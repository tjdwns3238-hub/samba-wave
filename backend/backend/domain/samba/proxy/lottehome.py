"""롯데홈쇼핑(롯데아이몰) OpenAPI 클라이언트 - httpx 기반.

proxy-server.mjs의 롯데홈쇼핑 관련 로직을 Python으로 포팅.
EUC-KR 인코딩 요청 / XML 응답 처리를 지원한다.

주요 특징:
- EUC-KR 인코딩 요청 (UTF-8 → EUC-KR 자동 변환)
- XML 응답 파싱 (defusedxml 사용)
- 인증키 자동 관리 (24시간 유효, 캐시 지원)
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from xml.etree import ElementTree as ET

import httpx

from backend.core.config import settings
from backend.utils.logger import logger

# 모듈 레벨 인증키 캐시 — 같은 계정/환경이면 인증 API 재호출 방지
# key: "user_id:env" → (cert_key, expires_at)
_cert_cache: dict[str, tuple[str, datetime]] = {}
# 동시 인증 방지 Lock
_auth_locks: dict[str, asyncio.Lock] = {}


async def _persist_cert_to_db(
    user_id: str, env: str, cert_key: str, expires_iso: str
) -> None:
    """인증키를 DB에 저장 — 재시작 후에도 유지."""
    try:
        from backend.db.orm import get_write_session
        from backend.domain.samba.forbidden.model import SambaSettings
        from sqlmodel import select as _sel

        db_key = f"lottehome_cert_{user_id}_{env}"
        async with get_write_session() as session:
            row = (
                (
                    await session.execute(
                        _sel(SambaSettings).where(SambaSettings.key == db_key)
                    )
                )
                .scalars()
                .first()
            )
            val = {"cert_key": cert_key, "expires_at": expires_iso}
            if row:
                row.value = val
            else:
                session.add(SambaSettings(key=db_key, value=val))
            await session.commit()
    except Exception as e:
        logger.warning(f"[롯데홈쇼핑] 인증키 DB 저장 실패: {e}")


async def _persist_cert_to_lottehome_credentials(
    cert_key: str, expires_iso: str
) -> None:
    """재발급된 인증키를 lottehome_credentials에도 저장 — 서버 재시작 후 즉시 복구."""
    try:
        from backend.db.orm import get_write_session
        from backend.domain.samba.forbidden.model import SambaSettings
        from sqlmodel import select as _sel

        async with get_write_session() as session:
            row = (
                (
                    await session.execute(
                        _sel(SambaSettings).where(
                            SambaSettings.key == "lottehome_credentials"
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row and isinstance(row.value, dict):
                row.value = {
                    **row.value,
                    "certKey": cert_key,
                    "certExpiresAt": expires_iso,
                }
                await session.commit()
    except Exception as e:
        logger.warning(f"[롯데홈쇼핑] lottehome_credentials 인증키 업데이트 실패: {e}")


async def _load_cert_from_db(user_id: str, env: str) -> tuple[str, datetime] | None:
    """DB에서 인증키 로드 — 재시작 후 캐시 복구용."""
    try:
        from backend.db.orm import get_read_session
        from backend.domain.samba.forbidden.model import SambaSettings
        from sqlmodel import select as _sel

        async with get_read_session() as session:
            # 1순위: 새 형식 키 (lottehome_cert_{user_id}_{env})
            db_key = f"lottehome_cert_{user_id}_{env}"
            row = (
                (
                    await session.execute(
                        _sel(SambaSettings).where(SambaSettings.key == db_key)
                    )
                )
                .scalars()
                .first()
            )
            if row and isinstance(row.value, dict):
                val = row.value
                cert_key = val.get("cert_key", "")
                expires_iso = val.get("expires_at", "")
                if cert_key and expires_iso:
                    expires_at = datetime.fromisoformat(expires_iso)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    return cert_key, expires_at
            # 2순위: 설정 페이지 인증 테스트가 저장한 lottehome_credentials
            row2 = (
                (
                    await session.execute(
                        _sel(SambaSettings).where(
                            SambaSettings.key == "lottehome_credentials"
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row2 and isinstance(row2.value, dict):
                val2 = row2.value
                cert_key = val2.get("certKey", "")
                expires_iso = val2.get("certExpiresAt", "")
                if cert_key and expires_iso:
                    expires_at = datetime.fromisoformat(expires_iso)
                    if expires_at.tzinfo is None:
                        expires_at = expires_at.replace(tzinfo=timezone.utc)
                    return cert_key, expires_at
            return None
    except Exception as e:
        logger.warning(f"[롯데홈쇼핑] 인증키 DB 로드 실패: {e}")
        return None


class LotteHomeClient:
    """롯데홈쇼핑(롯데아이몰) OpenAPI 클라이언트."""

    TEST_BASE = "http://openapitst.lotteimall.com/openapi/"
    PROD_BASE = "https://openapi.lotteimall.com/openapi/"

    def __init__(
        self,
        user_id: str,
        password: str,
        agnc_no: str = "",
        env: str = "test",
        hp_no: str = "",
        cert_key: str = "",
        cert_expires_at_iso: str = "",
    ) -> None:
        self.user_id = user_id
        self.password = password
        self.agnc_no = agnc_no
        self.env = env
        self.hp_no = hp_no

        # 인증 캐시 (메모리)
        self._cert_key: str = ""
        self._cert_expires_at: Optional[datetime] = None

        # DB에서 전달된 cert key 사전 주입 (서버 리로드 후에도 유효)
        if cert_key and cert_expires_at_iso:
            try:
                expires_at = datetime.fromisoformat(cert_expires_at_iso)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                self._cert_key = cert_key
                self._cert_expires_at = expires_at
                _cert_cache[f"{user_id}:{env}"] = (cert_key, expires_at)
                logger.debug(
                    f"[롯데홈쇼핑] DB에서 인증키 주입 (key={cert_key[:8]}..., env={env})"
                )
            except Exception as e:
                logger.warning(f"[롯데홈쇼핑] DB 인증키 주입 실패: {e}")

    @property
    def base_url(self) -> str:
        return self.PROD_BASE if self.env == "prod" else self.TEST_BASE

    # ------------------------------------------------------------------
    # EUC-KR encoding helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_euc_kr(val: str) -> str:
        """UTF-8 문자열을 EUC-KR 퍼센트 인코딩으로 변환."""
        try:
            encoded = val.encode("euc-kr")
        except (UnicodeEncodeError, LookupError):
            encoded = val.encode("utf-8")
        return "".join(f"%{b:02X}" for b in encoded)

    @staticmethod
    def _build_query(params: dict[str, Any]) -> str:
        """파라미터를 EUC-KR 인코딩된 쿼리스트링으로 변환."""
        parts = []
        for k, v in params.items():
            if v is None or v == "":
                continue
            from urllib.parse import quote

            parts.append(f"{quote(k)}={LotteHomeClient._encode_euc_kr(str(v))}")
        return "&".join(parts)

    # ------------------------------------------------------------------
    # XML parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_xml_to_dict(element: ET.Element) -> Any:
        """XML Element를 dict/str로 재귀 변환."""
        children = list(element)
        if not children:
            return (element.text or "").strip()

        result: dict[str, Any] = {}
        # 속성 포함
        for attr_name, attr_val in element.attrib.items():
            result[f"@_{attr_name}"] = attr_val

        for child in children:
            tag = child.tag
            value = LotteHomeClient._parse_xml_to_dict(child)
            if tag in result:
                existing = result[tag]
                if isinstance(existing, list):
                    existing.append(value)
                else:
                    result[tag] = [existing, value]
            else:
                result[tag] = value
        return result

    @staticmethod
    def _parse_lotte_response(xml_str: str) -> dict[str, Any]:
        """롯데홈쇼핑 XML 응답 파싱 (성공/에러 분기)."""
        # 비정상 응답 방어 — 인증만료/장애 시 거대 HTML(수십MB)이 와서
        # ET.fromstring이 "not well-formed (invalid token): column 24861604" 같은
        # cryptic 에러를 내고 정체를 못 드러내던 사고 방지. 응답 정체를 로그에 노출.
        _stripped = (xml_str or "").lstrip()
        if not _stripped.startswith("<"):
            raise LotteApiError(
                code="NOT_XML",
                message=f"비XML 응답({len(xml_str):,}자) 앞부분: {_stripped[:500]!r}",
            )
        try:
            root_el = ET.fromstring(xml_str)
        except ET.ParseError as _e:
            raise LotteApiError(
                code="XML_PARSE",
                message=(
                    f"XML 파싱 실패({len(xml_str):,}자): {str(_e)[:200]} / "
                    f"앞부분: {_stripped[:300]!r}"
                ),
            )
        parsed = LotteHomeClient._parse_xml_to_dict(root_el)

        # 실제 루트는 대문자 Response
        root = parsed if isinstance(parsed, dict) else {}

        # 에러 블록 확인
        errors = root.get("Errors") or root.get("errors")
        if errors:
            error_block = errors.get("Error") if isinstance(errors, dict) else errors
            if isinstance(error_block, dict):
                code = error_block.get("Code", error_block.get("code", ""))
                msg = error_block.get("Message") or error_block.get(
                    "message", "알 수 없는 오류"
                )
                if str(code) != "0":
                    raise LotteApiError(code=str(code), message=str(msg))

        return {"success": True, "data": root, "rawXml": xml_str}

    @staticmethod
    def _find_cert_key(obj: Any, depth: int = 0) -> Optional[str]:
        """응답 객체에서 인증키 필드를 재귀 탐색."""
        if not isinstance(obj, dict) or depth > 5:
            return None

        cert_key_names = [
            "certification_key",
            "certkey",
            "cert_key",
            "strcertkey",
            "certificationkey",
            "authkey",
            "auth_key",
            "token",
            "strtoken",
            "sessionkey",
            "session_key",
            "subscriptionid",
        ]

        for k, v in obj.items():
            if k.lower() in cert_key_names and v and not isinstance(v, dict):
                return str(v)

        for v in obj.values():
            if isinstance(v, dict):
                found = LotteHomeClient._find_cert_key(v, depth + 1)
                if found:
                    return found
        return None

    # ------------------------------------------------------------------
    # Low-level API caller
    # ------------------------------------------------------------------

    async def _call_api(
        self,
        endpoint: str,
        method: str = "POST",
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """롯데홈쇼핑 API 공통 호출 (EUC-KR 인코딩)."""
        params = params or {}
        url = self.base_url + endpoint
        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=euc-kr",
            "Accept": "text/xml; charset=euc-kr",
            "Accept-Charset": "euc-kr",
        }

        timeout = httpx.Timeout(settings.http_timeout_default, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                qs = self._build_query(params)
                if qs:
                    url = f"{url}?{qs}"
                resp = await client.get(url, headers=headers)
            else:
                body = self._build_query(params)
                resp = await client.post(url, content=body, headers=headers)

            # EUC-KR 응답을 UTF-8로 변환
            raw_bytes = resp.content
            try:
                xml_str = raw_bytes.decode("euc-kr")
            except (UnicodeDecodeError, LookupError):
                xml_str = raw_bytes.decode("utf-8", errors="replace")

            # XML 선언의 encoding="EUC-KR"을 제거 (이미 UTF-8로 디코딩됨)
            xml_str = re.sub(r"<\?xml[^?]*\?>", "", xml_str).strip()
            return self._parse_lotte_response(xml_str)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def _call_api_auto_retry(
        self,
        endpoint: str,
        method: str = "POST",
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """인증키 오류([0001]/[5001] + 인증 메시지) 시 자동 재인증 후 1회 재시도.

        주의: 0001 은 롯데홈쇼핑이 다양한 "데이터 없음" 에 광범위하게 사용한다.
        예) 마스터 데이터 미존재(brnd_no/disp_no 등)도 0001 로 떨어진다.
        메시지에 "인증" 키워드가 있을 때만 재인증으로 분류한다.
        """
        try:
            return await self._call_api(endpoint, method, params)
        except LotteApiError as e:
            msg = (e.lotte_msg or "").lower()
            is_auth = e.code == "5001" or (
                e.code == "0001"
                and any(k in (e.lotte_msg or "") for k in ("인증", "토큰", "키"))
            )
            if is_auth:
                logger.info(
                    f"[롯데홈쇼핑] 인증키 무효 감지 → 강제 재인증 후 재시도 (endpoint={endpoint}, code={e.code}, msg={e.lotte_msg})"
                )
                _cert_cache.pop(f"{self.user_id}:{self.env}", None)
                self._cert_key = ""
                self._cert_expires_at = None
                new_key = await self._ensure_auth(force=True)
                if params and "subscriptionId" in params:
                    params = {**params, "subscriptionId": new_key}
                return await self._call_api(endpoint, method, params)
            # 0001 이지만 인증 외 사유면 그대로 raise → 호출자에게 정확한 원인 노출
            if e.code == "0001":
                logger.warning(
                    f"[롯데홈쇼핑] 0001(데이터 미존재) — endpoint={endpoint}, msg={e.lotte_msg}"
                )
            _ = msg  # suppress unused
            raise

    async def _ensure_auth(self, force: bool = False) -> str:
        """인증키 자동 관리.
        force=True: 캐시/DB 무시하고 무조건 createCertification.lotte 새 발급.
        """
        now = datetime.now(tz=timezone.utc)
        refresh_before = timedelta(minutes=30)
        cache_key = f"{self.user_id}:{self.env}"

        if not force:
            # 모듈 캐시 유효하면 바로 반환
            cached = _cert_cache.get(cache_key)
            if cached:
                cert_key, expires_at = cached
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if (expires_at - now) > refresh_before:
                    self._cert_key = cert_key
                    self._cert_expires_at = expires_at
                    return cert_key

        # 동시 인증 방지
        if cache_key not in _auth_locks:
            _auth_locks[cache_key] = asyncio.Lock()
        async with _auth_locks[cache_key]:
            # Lock 획득 후 재확인 — force=True 여도 동시 호출이 이미 갱신했으면 재사용
            cached = _cert_cache.get(cache_key)
            if cached:
                cert_key, expires_at = cached
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if (expires_at - now) > refresh_before:
                    self._cert_key = cert_key
                    self._cert_expires_at = expires_at
                    return cert_key

            if not force:
                # 메모리 캐시 미스 → DB에서 복구 시도 (재시작 후 재인증 방지)
                db_cached = await _load_cert_from_db(self.user_id, self.env)
                if db_cached:
                    cert_key, expires_at = db_cached
                    if (expires_at - now) > refresh_before:
                        self._cert_key = cert_key
                        self._cert_expires_at = expires_at
                        _cert_cache[cache_key] = (cert_key, expires_at)
                        logger.info(
                            f"[롯데홈쇼핑] DB에서 인증키 복구 (만료: {expires_at.isoformat()})"
                        )
                        return cert_key

            params: dict[str, Any] = {
                "strUserId": self.user_id,
                "strPassWd": self.password,
            }
            if self.agnc_no:
                params["strAgncNo"] = self.agnc_no
            if self.hp_no:
                params["strHpNo"] = self.hp_no

            result = await self._call_api("createCertification.lotte", "POST", params)
            data = result.get("data", {})

            cert_key = self._find_cert_key(data)
            if not cert_key:
                raise LotteApiError(
                    code="AUTH_FAILED",
                    message=f"인증키를 응답에서 찾을 수 없습니다. 응답 구조: {data}",
                )

            expires_at = now + timedelta(hours=23, minutes=55)
            expires_iso = expires_at.isoformat()
            self._cert_key = cert_key
            self._cert_expires_at = expires_at
            _cert_cache[cache_key] = (cert_key, expires_at)
            # DB 저장 (비동기 — 등록 흐름 지연 없음)
            asyncio.create_task(
                _persist_cert_to_db(self.user_id, self.env, cert_key, expires_iso)
            )
            asyncio.create_task(
                _persist_cert_to_lottehome_credentials(cert_key, expires_iso)
            )

            logger.info(
                f"[롯데홈쇼핑] 인증키 발급 완료 (만료: {expires_at.isoformat()})"
            )
            return self._cert_key

    async def authenticate(self) -> dict[str, Any]:
        """인증키 발급 (명시적 호출)."""
        cert_key = await self._ensure_auth()
        remaining_minutes = 0
        if self._cert_expires_at:
            remaining_minutes = int(
                (self._cert_expires_at - datetime.now(tz=timezone.utc)).total_seconds()
                / 60
            )
        return {
            "success": True,
            "message": (
                f"인증 성공 (잔여: {remaining_minutes // 60}시간 {remaining_minutes % 60}분)"
            ),
            "certKey": cert_key,
            "expiresAt": (
                self._cert_expires_at.isoformat() if self._cert_expires_at else ""
            ),
            "remaining": remaining_minutes,
        }

    def get_auth_status(self) -> dict[str, Any]:
        """캐시된 인증 상태 확인."""
        if not self._cert_key or not self._cert_expires_at:
            return {"authenticated": False, "message": "인증 정보 없음"}

        remaining = int(
            (self._cert_expires_at - datetime.now(tz=timezone.utc)).total_seconds() / 60
        )
        if remaining <= 0:
            self._cert_key = ""
            self._cert_expires_at = None
            return {"authenticated": False, "message": "인증키 만료됨"}

        return {
            "authenticated": True,
            "userId": self.user_id,
            "env": self.env,
            "expiresAt": self._cert_expires_at.isoformat(),
            "remaining": remaining,
            "message": f"인증 유효 (잔여: {remaining // 60}시간 {remaining % 60}분)",
        }

    def clear_auth(self) -> dict[str, Any]:
        """인증 캐시 초기화."""
        self._cert_key = ""
        self._cert_expires_at = None
        return {"success": True, "message": "인증 캐시가 초기화되었습니다."}

    # ------------------------------------------------------------------
    # 기초정보 조회
    # ------------------------------------------------------------------

    async def search_brands(self, brand_name: str = "") -> dict[str, Any]:
        """브랜드 목록 조회. POST 방식으로 한글 EUC-KR 인코딩 정상 처리."""
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {"subscriptionId": cert_key}
        if brand_name:
            params["brnd_nm"] = brand_name
        return await self._call_api_auto_retry(
            "searchBrandListOpenApi.lotte",
            "POST",
            params,
        )

    async def search_categories(
        self, disp_tp_cd: str = "", md_gsgr_no: str = ""
    ) -> dict[str, Any]:
        """전시카테고리 목록 조회. disp_tp_cd 필수 — 미지정 시 10/20 각각 호출 후 병합."""
        cert_key = await self._ensure_auth()
        if disp_tp_cd:
            return await self._call_api_auto_retry(
                "searchDispCatListOpenApi.lotte",
                "GET",
                {
                    "subscriptionId": cert_key,
                    "disp_tp_cd": disp_tp_cd,
                    "md_gsgr_no": md_gsgr_no,
                },
            )
        # disp_tp_cd 미지정: 10(필수)과 20(추가) 각각 호출 후 CategoryInfo 병합
        results: list[dict] = []
        for tp in ("10", "20"):
            try:
                res = await self._call_api_auto_retry(
                    "searchDispCatListOpenApi.lotte",
                    "GET",
                    {
                        "subscriptionId": cert_key,
                        "disp_tp_cd": tp,
                        "md_gsgr_no": md_gsgr_no,
                    },
                )
                results.append(res)
            except LotteApiError:
                pass
        if not results:
            raise LotteApiError(code="0005", message="전시카테고리 조회 실패")
        merged: dict[str, Any] = results[0].get("data") or {}
        if len(results) > 1:
            second = results[1].get("data") or {}

            def _get_cat_list(d: dict) -> list:
                res_block = d.get("Result", d)
                cat_list = res_block.get("CategoryInfoList", {})
                cats = (
                    cat_list.get("CategoryInfo", [])
                    if isinstance(cat_list, dict)
                    else cat_list
                )
                return cats if isinstance(cats, list) else ([cats] if cats else [])

            cats = _get_cat_list(merged) + _get_cat_list(second)
            merged_result = dict(merged.get("Result", merged))
            merged_result["CategoryInfoList"] = {"CategoryInfo": cats}
            merged = {"Result": merged_result}
        return {"success": True, "data": merged}

    async def search_md_list(self, md_nm: str = "", md_id: str = "") -> dict[str, Any]:
        """매입담당자(MD) 목록 조회."""
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {"subscriptionId": cert_key}
        if md_nm:
            params["md_nm"] = md_nm
        if md_id:
            params["md_id"] = md_id
        return await self._call_api_auto_retry(
            "searchMDListOpenApi.lotte",
            "GET",
            params,
        )

    async def search_md_groups(self, md_id: str = "") -> dict[str, Any]:
        """MD관리상품군 조회. md_id 필수."""
        cert_key = await self._ensure_auth()
        if not md_id:
            md_result = await self.search_md_list()
            md_data = md_result.get("data", {})
            logger.info(
                f"[롯데홈쇼핑] MD목록 응답 keys: {list(md_data.keys()) if isinstance(md_data, dict) else type(md_data).__name__}"
            )
            md_list = md_data.get("Result", md_data)
            info_list = (
                md_list.get("MDInfoList", {}) if isinstance(md_list, dict) else {}
            )
            md_info = info_list.get("MDInfo", {}) if isinstance(info_list, dict) else {}
            logger.info(
                f"[롯데홈쇼핑] MDInfo 추출 결과: type={type(md_info).__name__}, value={str(md_info)[:200]}"
            )
            if isinstance(md_info, list) and md_info:
                md_id = md_info[0].get("MDCode", "")
            elif isinstance(md_info, dict):
                md_id = md_info.get("MDCode", "")
            if not md_id:
                logger.warning(
                    f"[롯데홈쇼핑] MD코드를 찾지 못함. md_data={str(md_data)[:300]}"
                )
                return {"success": False, "message": "배정된 MD가 없습니다"}
            logger.info(f"[롯데홈쇼핑] MD코드 자동 조회: {md_id}")
            cert_key = self._cert_key  # search_md_list 이후 갱신된 cert 사용
        return await self._call_api_auto_retry(
            "searchMDGsgrListOpenApi.lotte",
            "GET",
            {"subscriptionId": cert_key, "md_id": md_id},
        )

    async def search_standard_categories(self, disp_no: str = "") -> dict[str, Any]:
        """전시카테고리에 매핑된 표준카테고리 목록 조회 (loadStdCatsByDispNo.lotte)."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "loadStdCatsByDispNo.lotte",
            "GET",
            {"subscriptionId": cert_key, "disp_no": disp_no},
        )

    async def search_delivery_policies(self) -> dict[str, Any]:
        """배송비정책 목록 조회."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "searchDlvPolcInfoListOpenApi.lotte",
            "GET",
            {"subscriptionId": cert_key},
        )

    async def register_delivery_policy(
        self, policy_data: dict[str, Any]
    ) -> dict[str, Any]:
        """배송비정책 등록."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "registApiDlvPolcInfo.lotte",
            "POST",
            {"subscriptionId": cert_key, **policy_data},
        )

    async def search_return_places(self) -> dict[str, Any]:
        """출고지/반품배송지 목록 조회. dlvp_tp_cd: 10=출고지, 20=반품지 — 각각 호출 후 병합."""
        shipping_places: list[dict[str, Any]] = []
        return_places: list[dict[str, Any]] = []
        for tp, target in (("10", shipping_places), ("20", return_places)):
            try:
                cert_key = await self._ensure_auth()
                res = await self._call_api_auto_retry(
                    "searchReturnListOpenApi.lotte",
                    "GET",
                    {"subscriptionId": cert_key, "dlvp_tp_cd": tp},
                )
                data = res.get("data", {})
                result = data.get("Result", data)
                items_wrap = result.get("ReturnInfoList", {})
                logger.info(
                    f"[롯데홈] 배송지 tp={tp} result_keys={list(result.keys()) if isinstance(result, dict) else type(result)}, items_wrap={str(items_wrap)[:400]}"
                )
                info = (
                    items_wrap.get("ReturnInfo", [])
                    if isinstance(items_wrap, dict)
                    else []
                )
                if isinstance(info, dict):
                    info = [info]
                for item in info if isinstance(info, list) else []:
                    target.append(
                        {
                            "code": item.get("ReturnCode", ""),
                            "name": item.get("ReturnName", ""),
                            "address": item.get("ReturnAddress", ""),
                        }
                    )
            except LotteApiError as e:
                logger.warning(f"[롯데홈] 배송지 tp={tp} 오류: {e}")
                continue
        return {
            "success": True,
            "data": {
                "shipping_places": shipping_places,
                "return_places": return_places,
            },
        }

    async def register_delivery_place(
        self, place_data: dict[str, Any]
    ) -> dict[str, Any]:
        """출고지/반품배송지 등록."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "registDlvpOpenApi.lotte",
            "POST",
            {"subscriptionId": cert_key, **place_data},
        )

    async def search_goods_article_codes(self, artc_cd: str = "") -> dict[str, Any]:
        """품목별 항목코드정보 조회."""
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {"subscriptionId": cert_key}
        if artc_cd:
            params["artc_cd"] = artc_cd
        return await self._call_api_auto_retry(
            "searchGoodsArtcItemCdListOpenApi.lotte",
            "GET",
            params,
        )

    # ------------------------------------------------------------------
    # 상품 CRUD
    # ------------------------------------------------------------------

    async def register_goods(self, goods_data: dict[str, Any]) -> dict[str, Any]:
        """신규상품등록."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "registApiGoodsInfo.lotte",
            "POST",
            {"subscriptionId": cert_key, **goods_data},
        )

    async def update_new_goods(
        self, goods_req_no: str, goods_data: dict[str, Any]
    ) -> dict[str, Any]:
        """신규상품수정 (승인 전)."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "upateApiNewGoodsInfo.lotte",
            "POST",
            {"subscriptionId": cert_key, "goods_req_no": goods_req_no, **goods_data},
        )

    async def update_display_goods(
        self, goods_no: str, goods_data: dict[str, Any]
    ) -> dict[str, Any]:
        """전시상품수정 (승인 후)."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "upateApiDisplayGoodsInfo.lotte",
            "POST",
            {"subscriptionId": cert_key, "goods_no": goods_no, **goods_data},
        )

    async def update_sale_status(
        self, goods_no: str, sale_stat_cd: str = "20"
    ) -> dict[str, Any]:
        """판매상태 변경. sale_stat_cd: 10=판매진행, 20=품절, 30=영구중단."""
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {
            "subscriptionId": cert_key,
            "goods_no": goods_no,
            "sale_stat_cd": sale_stat_cd,
        }
        if self.agnc_no:
            params["agncNo"] = self.agnc_no
        return await self._call_api_auto_retry(
            "updateGoodsSaleStat.lotte",
            "POST",
            params,
        )

    # ------------------------------------------------------------------
    # 재고
    # ------------------------------------------------------------------

    async def update_stock(
        self, goods_no: str, item_no: str, inv_qty: int
    ) -> dict[str, Any]:
        """재고수정."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "registStock.lotte",
            "POST",
            {
                "subscriptionId": cert_key,
                "goods_no": goods_no,
                "item_no": item_no,
                "inv_qty": inv_qty,
            },
        )

    async def search_goods_view(self, goods_no: str) -> dict[str, Any]:
        """전시상품 상세 조회 (승인 상태 확인용).

        다른 메서드와 동일하게 _call_api_auto_retry 경유 — 인증키 만료 시 자동 재발급.
        """
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "searchGoodsViewListOpenApi.lotte",
            "GET",
            {"subscriptionId": cert_key, "goods_no": goods_no},
        )

    async def update_price(
        self, goods_no: str, sale_price: int, margin_rate: int = 0
    ) -> dict[str, Any]:
        """판매가 수정 (updateGoodsSalePrcOpenApi)."""
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {
            "subscriptionId": cert_key,
            "strGoodsNo": goods_no,
            "strReqSalePrc": str(sale_price),
        }
        if margin_rate > 0:
            params["mrgnRt"] = str(margin_rate)
        return await self._call_api_auto_retry(
            "updateGoodsSalePrcOpenApi.lotte", "POST", params
        )

    async def search_stock(self, goods_no: str = "") -> dict[str, Any]:
        """재고 목록 조회."""
        cert_key = await self._ensure_auth()
        return await self._call_api_auto_retry(
            "searchStockList.lotte",
            "GET",
            {"subscriptionId": cert_key, "goods_no": goods_no},
        )

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------

    async def search_new_orders(
        self, start_date: str, end_date: str, sel_option: str = "01"
    ) -> list[dict[str, Any]]:
        """신규주문조회 (searchNewOrdLstOpenApi.lotte).

        sel_option:
            01 = 미발주(신규)
            02 = 발주확인(출하지시)
            03 = 발송약정
        """
        cert_key = await self._ensure_auth()
        result = await self._call_api_auto_retry(
            "searchNewOrdLstOpenApi.lotte",
            "GET",
            {
                "subscriptionId": cert_key,
                "start_date": start_date,
                "end_date": end_date,
                "SelOption": sel_option,
            },
        )
        data = result.get("data", {})
        result_data = data.get("Result", data)
        # 응답 키: OrderInfo (단건이면 dict, 다건이면 list)
        orders = result_data.get("OrderInfo", result_data.get("OrdList", []))
        if isinstance(orders, dict):
            orders = [orders]
        return orders if isinstance(orders, list) else []

    async def search_deliver_list(
        self,
        start_date: str,
        end_date: str,
        ord_dtl_stat_cd: str = "17",
        date_gubun: str = "rlor_dtime",
    ) -> list[dict[str, Any]]:
        """배송조회 (searchDeliverList.lotte) — 출고확정/배송완료 등 상태 주문 조회."""
        cert_key = await self._ensure_auth()
        result = await self._call_api_auto_retry(
            "searchDeliverList.lotte",
            "GET",
            {
                "subscriptionId": cert_key,
                "date_gubun": date_gubun,
                "start_date": start_date,
                "end_date": end_date,
                "ord_dtl_stat_cd": ord_dtl_stat_cd,
            },
        )
        data = result.get("data", {})
        result_data = data.get("Result", data)
        orders = result_data.get("OrderInfo", result_data.get("OrdList", []))
        if isinstance(orders, dict):
            orders = [orders]
        return orders if isinstance(orders, list) else []

    async def search_cancel_orders(
        self, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        """주문취소조회 (searchCnclList.lotte)."""
        cert_key = await self._ensure_auth()
        result = await self._call_api_auto_retry(
            "searchCnclList.lotte",
            "GET",
            {
                "subscriptionId": cert_key,
                "start_date": start_date,
                "end_date": end_date,
            },
        )
        data = result.get("data", {})
        result_data = data.get("Result", data)
        orders = result_data.get("OrderInfo", result_data.get("OrdList", []))
        if isinstance(orders, dict):
            orders = [orders]
        return orders if isinstance(orders, list) else []

    async def search_return_orders(
        self, start_date: str, end_date: str, ord_dtl_stat_cd: str = "20"
    ) -> list[dict[str, Any]]:
        """반품조회 (searchReturnList.lotte).

        ord_dtl_stat_cd:
            20 = 반품진행
            21 = 회수확정
        """
        cert_key = await self._ensure_auth()
        result = await self._call_api_auto_retry(
            "searchReturnList.lotte",
            "GET",
            {
                "subscriptionId": cert_key,
                "start_date": start_date,
                "end_date": end_date,
                "ord_dtl_stat_cd": ord_dtl_stat_cd,
            },
        )
        data = result.get("data", {})
        result_data = data.get("Result", data)
        orders = result_data.get("OrderInfo", result_data.get("OrdList", []))
        if isinstance(orders, dict):
            orders = [orders]
        return orders if isinstance(orders, list) else []

    # ------------------------------------------------------------------
    # 배송처리 / 반품처리 / 배송비조회 / CS
    # 스펙: 롯데아이몰 OpenAPI 연동표준안 (2024-05-22 기준)
    # ------------------------------------------------------------------

    async def register_deliver(
        self,
        ord_no: str,
        ord_dtl_sn: str,
        proc_gubun: str,
        hdc_cd: str = "",
        inv_no: str = "",
        snd_contr_dtime: str = "",
        dlv_fin_dtime: str = "",
    ) -> dict[str, Any]:
        """배송처리 (registDeliver.lotte).

        proc_gubun:
            sfin = 출고확정 (hdc_cd + inv_no 필수)
            dfin = 배송완료 (dlv_fin_dtime YYYYMMDD 필수)
            contr = 발송약정 (snd_contr_dtime YYYYMMDD 필수)
            imps = 발송불가
        """
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {
            "subscriptionId": cert_key,
            "ord_no": ord_no,
            "ord_dtl_sn": ord_dtl_sn,
            "proc_gubun": proc_gubun,
        }
        if hdc_cd:
            params["hdc_cd"] = hdc_cd
        if inv_no:
            params["inv_no"] = inv_no
        if snd_contr_dtime:
            params["snd_contr_dtime"] = snd_contr_dtime
        if dlv_fin_dtime:
            params["dlv_fin_dtime"] = dlv_fin_dtime

        result = await self._call_api_auto_retry("registDeliver.lotte", "GET", params)
        data = result.get("data", {}) or {}
        # 성공: <Result>1</Result>
        ok_val = data.get("Result")
        return {
            "ok": str(ok_val).strip() == "1",
            "result": ok_val,
            "raw": data,
        }

    async def send_invoice(
        self,
        ord_no: str,
        ord_dtl_sn: str,
        courier_code: str,
        tracking_number: str,
    ) -> dict[str, Any]:
        """송장 전송 (배송처리 sfin = 출고확정)."""
        return await self.register_deliver(
            ord_no=ord_no,
            ord_dtl_sn=ord_dtl_sn,
            proc_gubun="sfin",
            hdc_cd=courier_code,
            inv_no=tracking_number,
        )

    async def process_return(
        self,
        ord_no: str,
        ord_dtl_sn: str,
        courier_code: str = "",
        tracking_number: str = "",
    ) -> dict[str, Any]:
        """반품처리 — 회수확정 (registDeliver.lotte, proc_gubun=rfin).

        반품완료 처리 시 hdc_cd + inv_no 필수.
        """
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {
            "subscriptionId": cert_key,
            "ord_no": ord_no,
            "ord_dtl_sn": ord_dtl_sn,
            "proc_gubun": "rfin",
        }
        if courier_code:
            params["hdc_cd"] = courier_code
        if tracking_number:
            params["inv_no"] = tracking_number

        result = await self._call_api_auto_retry("registDeliver.lotte", "GET", params)
        data = result.get("data", {}) or {}
        ok_val = data.get("Result")
        return {
            "ok": str(ok_val).strip() == "1",
            "result": ok_val,
            "raw": data,
        }

    async def search_delivery_fee(self, dlv_unit_sn: str) -> list[dict[str, Any]]:
        """배송비조회 (searchDeliverPriceList.lotte).

        Gubun 코드 일부:
            19 = 배송비
            33 = 반품비
            34 = 추가배송비
            32 = 초도배송비
            62 = 도서산간 추가배송비
        OccurYn: 10=발생, 11=취소
        """
        cert_key = await self._ensure_auth()
        result = await self._call_api_auto_retry(
            "searchDeliverPriceList.lotte",
            "GET",
            {
                "subscriptionId": cert_key,
                "dlv_unit_sn": dlv_unit_sn,
            },
        )
        data = result.get("data", {}) or {}
        result_data = data.get("Result", data)
        infos = result_data.get("PriceInfo", [])
        if isinstance(infos, dict):
            infos = [infos]
        return infos if isinstance(infos, list) else []

    async def search_cs_voc(
        self,
        req_start_dtime: str,
        req_end_dtime: str,
        proc_stat_cd: str = "",
        mvot_tp_cd: str = "",
    ) -> list[dict[str, Any]]:
        """CS문의/메모(VOC) 조회 (searchCSCounselMemoListOpenApi.lotte).

        proc_stat_cd: 빈값=전체, 01=미처리, 02=완료
        mvot_tp_cd:   빈값=전체, 05=답변필요, 06=알림
        """
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {
            "subscriptionId": cert_key,
            "req_start_dtime": req_start_dtime,
            "req_end_dtime": req_end_dtime,
        }
        if proc_stat_cd:
            params["proc_stat_cd"] = proc_stat_cd
        if mvot_tp_cd:
            params["mvot_tp_cd"] = mvot_tp_cd

        result = await self._call_api_auto_retry(
            "searchCSCounselMemoListOpenApi.lotte", "GET", params
        )
        data = result.get("data", {}) or {}
        result_data = data.get("Result", data)
        infos = result_data.get("CSQuestInfo", [])
        if isinstance(infos, dict):
            infos = [infos]
        return infos if isinstance(infos, list) else []

    async def register_cs_voc_answer(
        self,
        ccn_no: str,
        mvot_req_sn: str,
        cnsl_proc_cont: str,
    ) -> dict[str, Any]:
        """CS문의/메모(VOC) 답변 등록 (updateCounselMemoOpenApi.lotte).

        Result: 1=성공, 2=실패, 3=이미 처리된 답변
        """
        cert_key = await self._ensure_auth()
        result = await self._call_api_auto_retry(
            "updateCounselMemoOpenApi.lotte",
            "POST",
            {
                "subscriptionId": cert_key,
                "ccn_no": ccn_no,
                "mvot_req_sn": mvot_req_sn,
                "cnsl_proc_cont": cnsl_proc_cont,
            },
        )
        data = result.get("data", {}) or {}
        ok_val = data.get("Result")
        return {
            "ok": str(ok_val).strip() == "1",
            "already_done": str(ok_val).strip() == "3",
            "result": ok_val,
            "raw": data,
        }

    async def search_qna_list(
        self,
        req_start_dtime: str,
        req_end_dtime: str,
        c_val: str = "",
        proc_fin_yn: str = "",
    ) -> list[dict[str, Any]]:
        """상품 Q&A (핫라인) 조회 (searchQnAListOpenApi.lotte).

        c_val:       빈값=전체, 2=상품정보Q&A, 17=고객핫라인
        proc_fin_yn: 빈값=전체, 99=미처리, 02=처리완료
        """
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {
            "subscriptionId": cert_key,
            "req_start_dtime": req_start_dtime,
            "req_end_dtime": req_end_dtime,
        }
        if c_val:
            params["c_val"] = c_val
        if proc_fin_yn:
            params["proc_fin_yn"] = proc_fin_yn

        result = await self._call_api_auto_retry(
            "searchQnAListOpenApi.lotte", "GET", params
        )
        data = result.get("data", {}) or {}
        result_data = data.get("Result", data)
        infos = result_data.get("GoodsQuestInfo", [])
        if isinstance(infos, dict):
            infos = [infos]
        return infos if isinstance(infos, list) else []

    async def register_qna_answer(
        self,
        inq_no: str,
        inq_ans_cont: str,
        ans_cont_type: str = "1",
        ans_disp_yn: str = "Y",
        memo: str = "",
    ) -> dict[str, Any]:
        """상품 Q&A 답변 등록 (updateQnaAnswerOpenApi.lotte).

        ans_cont_type: 1=유형1, 2=유형2 (머리말/맺음말)
        ans_disp_yn:   Y=전시, N=미전시
        Result: 1=성공, 2=실패
        """
        cert_key = await self._ensure_auth()
        params: dict[str, Any] = {
            "subscriptionId": cert_key,
            "inq_no": inq_no,
            "ans_cont_type": ans_cont_type,
            "inq_ans_cont": inq_ans_cont,
            "ans_disp_yn": ans_disp_yn,
        }
        if memo:
            params["memo"] = memo

        result = await self._call_api_auto_retry(
            "updateQnaAnswerOpenApi.lotte", "POST", params
        )
        data = result.get("data", {}) or {}
        ok_val = data.get("Result")
        return {
            "ok": str(ok_val).strip() == "1",
            "result": ok_val,
            "raw": data,
        }


# ----------------------------------------------------------------------
# 택배사 코드 매핑 (스펙: 70.배송처리 별첨, 2024-05-22)
# 한글 택배사명 → 롯데홈쇼핑 hdc_cd
# ----------------------------------------------------------------------
LOTTEHOME_COURIER_CODES: dict[str, str] = {
    "롯데택배": "11",
    "롯데글로벌로지스": "11",
    "CJ대한통운": "12",
    "대한통운": "12",
    "씨제이대한통운": "12",
    "CJGLS": "16",
    "CJ GLS": "16",
    "한진택배": "15",
    "한진": "15",
    "로젠택배": "24",
    "로젠": "24",
    "우체국택배": "31",
    "우체국": "31",
    "천일택배": "17",
    "일양택배": "18",
    "KG로지스": "21",
    "경동택배": "50",
    "DHL": "63",
    "EMS": "64",
    "FedEx": "65",
    "TNTExpress": "68",
    "UPS": "69",
    "굿모닝택배": "70",
    "합동택배": "76",
    "기타": "99",
    "기타택배": "19",
}


def lottehome_courier_code(name: str) -> str:
    """한글 택배사명 → 롯데홈쇼핑 hdc_cd. 매칭 실패 시 '99'(기타)."""
    if not name:
        return "99"
    n = (name or "").replace(" ", "").strip()
    for k, v in LOTTEHOME_COURIER_CODES.items():
        if k.replace(" ", "").strip() == n:
            return v
    # 부분 매칭 폴백
    for k, v in LOTTEHOME_COURIER_CODES.items():
        ks = k.replace(" ", "").strip()
        if ks and (ks in n or n in ks):
            return v
    return "99"


class LotteApiError(Exception):
    """롯데홈쇼핑 API 오류."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.lotte_msg = message
        super().__init__(f"[{code}] {message}")
