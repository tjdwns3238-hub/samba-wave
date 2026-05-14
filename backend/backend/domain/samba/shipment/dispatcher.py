"""마켓 전송 디스패처 — 플러그인 기반.

모든 18개 마켓이 플러그인으로 등록되어 있으므로
레거시 인라인 핸들러는 제거되었다.
삭제/판매중지 핸들러는 아직 플러그인 미전환이므로 유지.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from sqlmodel.ext.asyncio.session import AsyncSession

from backend.domain.samba.forbidden.repository import SambaSettingsRepository
from backend.utils.logger import logger


# ═══════════════════════════════════════════════
# 공통 헬퍼
# ═══════════════════════════════════════════════


async def _get_setting(session: AsyncSession, key: str) -> Any:
    """samba_settings 테이블에서 설정값 조회 후 즉시 커밋 — idle in transaction 방지."""
    repo = SambaSettingsRepository(session)
    row = await repo.find_by_async(key=key)
    val = row.value if row else None
    try:
        await session.commit()
    except Exception:
        pass
    return val


# 마켓별 "이미 죽은 상품" 응답 공통 신호.
# 이 신호가 잡히면 우리 DB에서도 정리 완료(success=True)로 처리해
# registered_accounts ghost 매핑이 영구화되는 것을 막는다.
_DELETE_GHOST_SIGNALS = (
    "존재하지 않",
    "이미 삭제",
    "삭제된 상품",
    "판매종료 된 상품",
    "판매중지된 상품",
    "현재 판매 중인 상품이 아닙니다",
    "유효하지 않은 상품",
    "상품을 찾을 수 없",
    "8888",
    "9999",
    "HTTP 404",
)

# 마켓이 명시적으로 "삭제 거부" 한 응답 — ghost 자동정리로 처리하면 안 됨.
# 쿠팡 spec: 승인완료 상품은 DELETE 불가, 임시저장 상태에서만 가능.
# 이 응답을 ghost 로 잘못 처리하면 sambawave 측 registered_accounts 만 정리되어
# UI 상 "삭제됨" 으로 보이지만 마켓에는 살아남 = 사용자 보고 시나리오.
_DELETE_REJECT_SIGNALS = (
    "삭제가 불가능",
    "삭제는 '저장중', '임시저장'",
    "임시저장' 상태에서만",
    "삭제 권한이 없",
)


def _is_delete_ghost(err: str) -> bool:
    # 명시 거부 응답은 ghost 가 아님 — 우선 검사
    if any(sig in err for sig in _DELETE_REJECT_SIGNALS):
        return False
    return any(sig in err for sig in _DELETE_GHOST_SIGNALS)


async def _safe_delete(
    market_name: str,
    market_key: str,
    product: dict[str, Any],
    api_call: Callable[[str], Coroutine],
) -> dict[str, Any]:
    """마켓 삭제 공통 래퍼 — 상품번호 확인 + try/except 처리 + ghost 자동정리.

    Args:
      market_name: 로그/메시지용 마켓 이름 (예: "스마트스토어")
      market_key: market_product_no 딕셔너리 키 (예: "smartstore")
      product: 상품 딕셔너리
      api_call: product_no를 받아 삭제 API를 호출하는 코루틴
    """
    product_no = product.get("market_product_no", {}).get(market_key, "")
    logger.info(
        f"[{market_name}] 삭제 시도 — market_key={market_key}, product_no={product_no!r}"
    )
    if not product_no:
        return {"success": False, "message": f"{market_name} 상품번호 없음 (건너뜀)"}
    try:
        await api_call(product_no)
        return {"success": True, "message": f"{market_name} 삭제 완료"}
    except Exception as e:
        err = str(e)
        if _is_delete_ghost(err):
            # 마켓 측 정리 완료 → 우리도 success로 처리해 registered_accounts 정리.
            logger.warning(
                f"[{market_name}] 이미 종료/삭제된 상품 — 정리 완료로 처리: {err}"
            )
            return {
                "success": True,
                "message": f"{market_name} 이미 종료됨(자동정리): {err}",
                "ghost_cleanup": True,
            }
        logger.error(f"[{market_name}] 삭제 실패: {e}")
        return {"success": False, "message": f"삭제 실패: {e}", "error_detail": str(e)}


# ═══════════════════════════════════════════════
# 검증 / 디스패치 (플러그인 기반)
# ═══════════════════════════════════════════════


def validate_transform(market_type: str, product: dict) -> list[str]:
    """전송 전 필수필드 누락 검사 → 누락 필드명 리스트 반환."""
    from backend.domain.samba.plugins import MARKET_PLUGINS

    plugin = MARKET_PLUGINS.get(market_type)
    if not plugin:
        return [f"미지원 마켓: {market_type}"]
    return [f for f in plugin.required_fields if not product.get(f)]


async def dispatch_to_market(
    session: AsyncSession,
    market_type: str,
    product: dict[str, Any],
    category_id: str = "",
    account: Any = None,
    existing_product_no: str = "",
) -> dict[str, Any]:
    """마켓 타입에 따라 상품 등록/수정 API를 호출.

    Args:
      session: DB 세션
      market_type: 마켓 구분
      product: SambaCollectedProduct 딕셔너리
      category_id: 대상 마켓 카테고리 코드
      account: SambaMarketAccount 객체 (계정별 인증 정보)
      existing_product_no: 기존 마켓 상품번호 (있으면 수정, 없으면 신규등록)

    Returns:
      {"success": bool, "message": str, "data": Any}
    """
    from backend.domain.samba.plugins import MARKET_PLUGINS

    plugin = MARKET_PLUGINS.get(market_type)
    if not plugin:
        return {"success": False, "message": f"미지원 마켓: {market_type}"}

    # 필수필드 검증
    missing = [f for f in plugin.required_fields if not product.get(f)]
    if missing:
        return {
            "success": False,
            "error_type": "schema_changed",
            "message": f"필수필드 누락: {', '.join(missing)}",
        }

    from backend.domain.samba.proxy.elevenst import ElevenstRateLimitError

    try:
        return await plugin.handle(
            session,
            product,
            category_id,
            account=account,
            existing_no=existing_product_no,
        )
    except ElevenstRateLimitError:
        raise  # worker까지 전파
    except Exception as e:
        logger.error(f"[디스패처] {market_type} 전송 예외: {e}")
        return {"success": False, "message": str(e)}


# ═══════════════════════════════════════════════
# 마켓 목록
# ═══════════════════════════════════════════════


def get_supported_markets() -> list[str]:
    """플러그인 기반 지원 마켓 목록."""
    from backend.domain.samba.plugins import MARKET_PLUGINS

    return list(MARKET_PLUGINS.keys())


SUPPORTED_MARKETS = get_supported_markets()

# 미지원 마켓 (공개 API 없음 — 파트너 계약 또는 연동솔루션 필요)
UNSUPPORTED_MARKETS = ["gmarket", "auction", "homeand", "hmall"]


# ═══════════════════════════════════════════════
# 마켓 상품 삭제/판매중지
# ═══════════════════════════════════════════════


async def delete_from_market(
    session: AsyncSession,
    market_type: str,
    product: dict[str, Any],
    account: Any = None,
    market_delete: bool = False,
) -> dict[str, Any]:
    """마켓에서 상품 판매중지/삭제.

    품절 감지 시 호출되어 마켓에 등록된 상품을 내린다.
    각 마켓 API에 판매중지 메서드가 있으면 호출하고,
    없으면 재고 0 업데이트로 대체한다.

    market_delete=True: 수동 마켓삭제 (EMP 직접 삭제 후 DB 정리용) — PlayAuto는 API 생략
    market_delete=False: 오토튠/리프레시 품절 처리 — PlayAuto는 EMP API 호출 + soldout_fallback
    """
    try:
        handler = MARKET_DELETE_HANDLERS.get(market_type)
        if not handler:
            # 삭제 핸들러 미구현 마켓 — 로그만 남김
            logger.warning(f"[디스패처] {market_type} 삭제 핸들러 미구현, 건너뜀")
            return {
                "success": False,
                "message": f"{market_type} 삭제 핸들러 미구현 (건너뜀)",
            }
        if market_type == "playauto":
            return await handler(
                session, product, account=account, market_delete=market_delete
            )
        return await handler(session, product, account=account)
    except Exception as exc:
        # 세션 오염 도미노 차단 — 다음 상품이 같은 세션 재사용 시 줄줄이 greenlet_spawn 폭발 방지
        try:
            await session.rollback()
        except Exception as rb_err:
            logger.warning(f"[디스패처] rollback 실패: {rb_err}")
        logger.error(f"[디스패처] {market_type} 상품 삭제 실패: {exc}", exc_info=True)
        return {"success": False, "message": f"{market_type} 삭제 실패: {str(exc)}"}


async def _delete_smartstore(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """스마트스토어 상품 삭제."""
    from backend.domain.samba.proxy.smartstore import SmartStoreClient

    # 계정 객체에서 인증 정보 우선 사용
    client_id = ""
    client_secret = ""
    if account:
        extras = getattr(account, "additional_fields", None) or {}
        client_id = extras.get("clientId", "") or getattr(account, "api_key", "") or ""
        client_secret = (
            extras.get("clientSecret", "") or getattr(account, "api_secret", "") or ""
        )

    # 계정이 명시된 삭제에서는 다른 계정의 전역 설정으로 폴백하지 않는다.
    if (not client_id or not client_secret) and account is None:
        creds = await _get_setting(session, "store_smartstore")
        if creds and isinstance(creds, dict):
            client_id = client_id or creds.get("clientId", "")
            client_secret = client_secret or creds.get("clientSecret", "")

    if not client_id or not client_secret:
        return {"success": False, "message": "스마트스토어 인증 정보 없음"}

    from backend.domain.samba.proxy.smartstore import SmartStoreApiError

    client = SmartStoreClient(client_id, client_secret)
    logger.info(
        f"[스마트스토어] 삭제 시도 — clientId={client_id[:6]}*** account={getattr(account, 'seller_id', '?')}"
    )
    product_no = product.get("market_product_no", {}).get("smartstore", "")
    if not product_no:
        return {"success": False, "message": "스마트스토어 상품번호 없음 (건너뜀)"}

    async def _soldout_fallback(target_no: str, original_err: str) -> dict[str, Any]:
        """삭제 불가 시 전 옵션 재고 0 품절 폴백 (order.py 패턴 차용)."""
        logger.warning(
            f"[스마트스토어] 삭제 실패({original_err[:120]}) → 품절 폴백 시도: {target_no}"
        )
        try:
            existing = await client.get_product(target_no)
            origin = existing.get("originProduct", {})
            for k in ["productNo", "channelProducts", "regDate", "modifiedDate"]:
                origin.pop(k, None)
            origin["stockQuantity"] = 0
            opt_info = (origin.get("detailAttribute", {}).get("optionInfo")) or {}
            # SmartStore GET 응답은 optionCombinations 키 사용 (transform_product와 동일)
            combos = opt_info.get("optionCombinations") or opt_info.get(
                "combinations", []
            )
            zeroed = 0
            for combo in combos:
                combo["stockQuantity"] = 0
                # usable=False 금지: SmartStore는 최소 1개 옵션이 usable=True+price=0이어야 함
                # (진행중인 주문 있을 때 usable=False 전체 설정 시 400 에러)
                zeroed += 1
            put_data: dict[str, Any] = {"originProduct": origin}
            if "smartstoreChannelProduct" in existing:
                put_data["smartstoreChannelProduct"] = existing[
                    "smartstoreChannelProduct"
                ]
            await client.update_product(target_no, put_data)
            logger.info(
                f"[스마트스토어] 품절 폴백 완료: {target_no} (옵션 {zeroed}개 재고0)"
            )
            return {
                "success": True,
                "soldout_fallback": True,
                "message": f"품절 처리 완료 (옵션 {zeroed}개)",
            }
        except Exception as fb_err:
            logger.error(f"[스마트스토어] 품절 폴백도 실패: {fb_err}")
            return {
                "success": False,
                "message": f"삭제 실패: {original_err} / 품절 폴백 실패: {fb_err}",
            }

    try:
        del_result = await client.delete_product(product_no)
        if del_result.get("already_deleted"):
            # DELETE 404: 실제 삭제됐는지 vs 다른 계정 소유인지 GET으로 확인
            try:
                await client.get_product(product_no)
                logger.error(
                    f"[스마트스토어] 상품 {product_no} GET 200이지만 DELETE 404 — "
                    f"다른 계정 소유 가능성. clientId={client_id[:6]}***, "
                    f"account={getattr(account, 'seller_id', '?')}"
                )
                return await _soldout_fallback(
                    product_no, "DELETE 404 but GET 200 (권한 없음)"
                )
            except SmartStoreApiError as get_err:
                if "HTTP 404" in str(get_err):
                    logger.info(
                        f"[스마트스토어] 상품 {product_no} GET도 404 — 실제 삭제됨"
                    )
                else:
                    logger.error(
                        f"[스마트스토어] 상품 {product_no} GET 실패 ({get_err})"
                    )
                return {
                    "success": True,
                    "message": "스마트스토어 삭제 완료 (이미 삭제됨)",
                }
        return {"success": True, "message": "스마트스토어 삭제 완료"}
    except SmartStoreApiError as e:
        err_str = str(e)
        if "HTTP 404" in err_str:
            # 채널번호를 origin-products API에 잘못 호출한 경우도 404 → 역조회 먼저 시도
            style_code = product.get("style_code", "") or product.get("styleCode", "")
            if style_code:
                logger.warning(
                    f"[스마트스토어] 삭제 404 ({product_no}) → 채널번호 오호출 가능성, "
                    f"sellerManagementCode({style_code})로 origin 역조회 시도"
                )
                found = await client.find_by_management_code(style_code)
                if found:
                    origin_no = str(
                        found.get("originProductNo")
                        or found.get("originProduct", {}).get("id", "")
                        or ""
                    )
                    if origin_no and origin_no != product_no:
                        logger.info(
                            f"[스마트스토어] origin 역조회 성공: {product_no} → {origin_no}, 재시도"
                        )
                        try:
                            await client.delete_product(origin_no)
                            return {
                                "success": True,
                                "message": f"스마트스토어 삭제 완료 (origin={origin_no})",
                            }
                        except SmartStoreApiError as e2:
                            if "HTTP 404" in str(e2):
                                return {
                                    "success": True,
                                    "message": "스마트스토어 삭제 완료 (이미 삭제됨)",
                                }
                            return await _soldout_fallback(origin_no, str(e2))
            # DELETE 404: 실제 삭제됐는지 vs 다른 계정 소유(권한 없음)인지 GET으로 확인
            try:
                existing = await client.get_product(product_no)
                # GET 200 → 상품이 존재하지만 DELETE 불가 → 다른 계정 소유 가능성
                logger.error(
                    f"[스마트스토어] 상품 {product_no} GET 200이지만 DELETE 404 — "
                    f"다른 계정 소유 가능성. clientId={client_id[:6]}***, "
                    f"account={getattr(account, 'seller_id', '?')}"
                )
                return await _soldout_fallback(
                    product_no, "DELETE 404 but GET 200 (권한 없음)"
                )
            except SmartStoreApiError as get_err:
                if "HTTP 404" in str(get_err):
                    logger.info(
                        f"[스마트스토어] 상품 {product_no} GET도 404 — 실제 삭제됨"
                    )
                    return {
                        "success": True,
                        "message": "스마트스토어 삭제 완료 (이미 삭제됨)",
                    }
                logger.error(
                    f"[스마트스토어] 상품 {product_no} GET 실패 ({get_err}) — "
                    f"삭제 여부 불명확, 성공으로 처리"
                )
                return {
                    "success": True,
                    "message": "스마트스토어 삭제 완료 (이미 삭제됨)",
                }
        # 채널번호로 잘못 호출된 경우 — sellerManagementCode로 originProductNo 역조회
        style_code = product.get("style_code", "") or product.get("styleCode", "")
        if style_code:
            logger.warning(
                f"[스마트스토어] 삭제 실패({err_str[:80]}) → sellerManagementCode({style_code})로 origin 역조회 시도"
            )
            found = await client.find_by_management_code(style_code)
            if found:
                origin_no = str(
                    found.get("originProductNo")
                    or found.get("originProduct", {}).get("id", "")
                    or ""
                )
                if origin_no and origin_no != product_no:
                    logger.info(
                        f"[스마트스토어] origin 역조회 성공: {product_no} → {origin_no}, 재시도"
                    )
                    try:
                        await client.delete_product(origin_no)
                        return {
                            "success": True,
                            "message": f"스마트스토어 삭제 완료 (origin={origin_no})",
                        }
                    except SmartStoreApiError as e2:
                        if "HTTP 404" in str(e2):
                            return {
                                "success": True,
                                "message": "스마트스토어 삭제 완료 (이미 삭제됨)",
                            }
                        # origin 번호로도 삭제 실패 → 품절 폴백
                        return await _soldout_fallback(origin_no, str(e2))
        # 삭제 실패 시 에러 종류 무관하게 품절 폴백 시도
        return await _soldout_fallback(product_no, err_str)


async def _delete_coupang(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """쿠팡 상품 삭제."""
    from backend.domain.samba.proxy.coupang import CoupangClient

    access_key = ""
    secret_key = ""
    vendor_id = ""
    if account:
        extras = getattr(account, "additional_fields", None) or {}
        access_key = (
            extras.get("accessKey", "") or getattr(account, "api_key", "") or ""
        )
        secret_key = (
            extras.get("secretKey", "") or getattr(account, "api_secret", "") or ""
        )
        vendor_id = (
            extras.get("vendorId", "") or getattr(account, "seller_id", "") or ""
        )

    if (not access_key or not secret_key) and account is None:
        creds = await _get_setting(session, "store_coupang")
        if creds and isinstance(creds, dict):
            access_key = access_key or creds.get("accessKey", "")
            secret_key = secret_key or creds.get("secretKey", "")
            vendor_id = vendor_id or creds.get("vendorId", "")

    if not access_key or not secret_key:
        return {"success": False, "message": "쿠팡 인증 정보 없음"}

    client = CoupangClient(access_key, secret_key, vendor_id)

    # 쿠팡 spec: DELETE 는 '저장중'/'임시저장' 상태에서만 가능.
    # 승인완료/심사중 상품은 모든 vendor-items 를 sales/stop 후 잠시 대기하면
    # 쿠팡 측에서 자동으로 삭제 가능 상태로 동기화되어 DELETE 가능.
    # (2026-05-11 검증: 16198825322 케이스에서 stop+대기 후 DELETE 성공)
    product_no = product.get("market_product_no", {}).get("coupang", "")
    if not product_no:
        return {"success": False, "message": "쿠팡 상품번호 없음 (건너뜀)"}

    async def _stop_all_items() -> int:
        """모든 vendor-items 를 sales/stop. 성공 개수 반환."""
        try:
            gr = await client.get_product(str(product_no))
            inner = gr.get("data", gr) if isinstance(gr, dict) else {}
            items = inner.get("items") or []
        except Exception as e:
            logger.warning(f"[쿠팡 삭제] get_product 실패: {product_no} — {e}")
            return 0
        ok = 0
        for it in items:
            vid = it.get("vendorItemId") if isinstance(it, dict) else None
            if not vid:
                continue
            try:
                await client._call_api(
                    "PUT",
                    f"/v2/providers/seller_api/apis/api/v1/marketplace/vendor-items/{vid}/sales/stop",
                )
                ok += 1
            except Exception as e:
                err_s = str(e)
                # 이미 stop 상태면 무시
                if "이미" in err_s or "already" in err_s.lower() or "판매중지" in err_s:
                    ok += 1
                else:
                    logger.warning(
                        f"[쿠팡 삭제] vid={vid} sales/stop 실패: {err_s[:120]}"
                    )
        return ok

    # 1차: 즉시 DELETE 시도 (임시저장 상품 빠르게 처리)
    try:
        await client.delete_product(str(product_no))
        return {"success": True, "message": "쿠팡 삭제 완료"}
    except Exception as e:
        first_err = str(e)
        if "삭제가 불가능" not in first_err and "임시저장" not in first_err:
            # 다른 에러는 ghost 확인
            if _is_delete_ghost(first_err):
                return {
                    "success": True,
                    "message": f"쿠팡 이미 종료됨(자동정리): {first_err}",
                    "ghost_cleanup": True,
                }
            return {
                "success": False,
                "message": f"쿠팡 삭제 실패: {first_err}",
            }

    # 2차: 모든 옵션 stop + 짧은 대기 + DELETE 재시도 (최대 3회)
    logger.info(f"[쿠팡 삭제] 옵션 stop 후 재시도 진행: {product_no}")
    stop_ok = await _stop_all_items()
    logger.info(f"[쿠팡 삭제] sales/stop 완료: {stop_ok}개 — DELETE 재시도")

    import asyncio as _asyncio

    last_err = ""
    for wait_sec in (5, 15, 30):
        await _asyncio.sleep(wait_sec)
        try:
            await client.delete_product(str(product_no))
            return {
                "success": True,
                "message": f"쿠팡 삭제 완료 (옵션 {stop_ok}개 stop 후, 대기 {wait_sec}s)",
            }
        except Exception as e:
            last_err = str(e)
            logger.info(
                f"[쿠팡 삭제] DELETE 재시도 실패 (대기 {wait_sec}s): {last_err[:120]}"
            )
            # ghost 신호면 자동정리로 처리
            if _is_delete_ghost(last_err):
                return {
                    "success": True,
                    "message": f"쿠팡 이미 종료됨(자동정리): {last_err}",
                    "ghost_cleanup": True,
                }
            # 거부 외 에러면 즉시 실패
            if "삭제가 불가능" not in last_err and "임시저장" not in last_err:
                return {
                    "success": False,
                    "message": f"쿠팡 삭제 실패: {last_err}",
                }

    return {
        "success": False,
        "message": (
            f"쿠팡 삭제 실패 — 옵션 stop({stop_ok}개) 후 재시도 3회 모두 거부됨. "
            f"쿠팡 측 동기화 지연 가능, 잠시 후 자동 재시도 권장: {last_err[:200]}"
        ),
    }


async def _delete_lottehome(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """롯데홈쇼핑 상품 삭제 (영구중단)."""
    # MD 승인 대기 중인 상품은 goods_no가 없으므로 삭제 불가 — 차단
    if account:
        m_nos = product.get("market_product_nos", {}) or {}
        account_id = getattr(account, "id", None)
        if account_id and m_nos.get(f"{account_id}_qa") == "pending":
            return {
                "success": False,
                "message": "롯데홈쇼핑 MD 승인 대기 중인 상품입니다. 승인 완료 후 삭제해주세요.",
            }

    from backend.domain.samba.proxy.lottehome import LotteHomeClient

    creds: dict[str, Any] | None = None
    db_creds = await _get_setting(session, "lottehome_credentials") or {}
    if not isinstance(db_creds, dict):
        db_creds = {}
    if account:
        extra = getattr(account, "additional_fields", None) or {}
        if isinstance(extra, dict) and (
            extra.get("userId")
            or extra.get("password")
            or extra.get("agncNo")
            or extra.get("env")
        ):
            # account.additional_fields에 env 없으면 lottehome_credentials에서 보완
            creds = {**db_creds, **extra}
        else:
            # account 제공됐지만 lottehome 자격증명 없음 → 전역 설정 폴백 금지
            return {"success": False, "message": "롯데홈쇼핑 계정 자격증명 없음"}
    if not creds:
        creds = db_creds or await _get_setting(session, "store_lottehome")
    if not creds or not isinstance(creds, dict):
        return {"success": False, "message": "롯데홈쇼핑 설정 없음"}

    client = LotteHomeClient(
        creds.get("userId", ""),
        creds.get("password", ""),
        creds.get("agncNo", ""),
        creds.get("env", "test"),
    )
    # 30 = 영구중단
    return await _safe_delete(
        "롯데홈쇼핑",
        "lottehome",
        product,
        lambda pno: client.update_sale_status(pno, "30"),
    )


async def _delete_gsshop(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """GS샵 상품 삭제 (판매 종료)."""
    from datetime import datetime, timezone

    from backend.domain.samba.proxy.gsshop import GsShopClient

    creds: dict[str, Any] | None = None
    if account:
        extra = getattr(account, "additional_fields", None) or {}
        if isinstance(extra, dict) and (
            extra.get("supCd")
            or extra.get("aesKey")
            or extra.get("apiKeyProd")
            or extra.get("apiKeyDev")
            or extra.get("env")
        ):
            creds = extra
    else:
        creds = await _get_setting(session, "gsshop_credentials")
        if not creds or not isinstance(creds, dict):
            creds = await _get_setting(session, "store_gsshop")
    if not creds or not isinstance(creds, dict):
        return {"success": False, "message": "GS샵 설정 없음"}

    sup_cd = (
        creds.get("supCd", "") or creds.get("storeId", "") or creds.get("vendorId", "")
    )
    if not sup_cd and account:
        sup_cd = getattr(account, "seller_id", "") or ""
    client = GsShopClient(
        sup_cd,
        creds.get("aesKey", "")
        or creds.get("apiKeyProd", "")
        or creds.get("apiKeyDev", ""),
        creds.get("subSupCd", ""),
        "prod" if creds.get("apiKeyProd") else creds.get("env", "dev"),
    )
    # 판매 종료일을 현재로 설정하여 즉시 판매 종료
    past = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return await _safe_delete(
        "GS샵",
        "gsshop",
        product,
        lambda pno: client.update_sale_status(pno, past),
    )


async def _delete_11st(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """11번가 상품 삭제."""
    from backend.domain.samba.proxy.elevenst import ElevenstClient

    api_key = ""
    if account:
        extra = getattr(account, "additional_fields", None) or {}
        api_key = extra.get("apiKey", "") or getattr(account, "api_key", "") or ""
    # 계정이 명시된 삭제에서는 다른 계정의 전역 설정으로 폴백하지 않는다.
    # 11번가 계정 API Key는 additional_fields.apiKey에 저장되는 경로가 기본이므로,
    # 여기서 전역 store_11st를 섞으면 다계정 환경에서 오삭제가 날 수 있다.
    if not api_key and account is None:
        creds = await _get_setting(session, "store_11st")
        if creds and isinstance(creds, dict):
            api_key = creds.get("apiKey", "")
    if not api_key:
        return {"success": False, "message": "11번가 인증 정보 없음"}

    client = ElevenstClient(api_key)
    return await _safe_delete("11번가", "11st", product, client.delete_product)


async def _delete_lotteon(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """롯데ON 상품 판매중지 — 플러그인 delete() 위임."""
    from backend.domain.samba.plugins.markets.lotteon import LotteonPlugin

    product_no = product.get("market_product_no", {}).get("lotteon", "")
    if not product_no:
        return {"success": False, "message": "롯데ON 상품번호 없음 (건너뜀)"}
    plugin = LotteonPlugin()
    return await plugin.delete(session, product_no, account)


async def _delete_ssg(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """SSG(신세계몰) 상품 삭제."""
    from backend.domain.samba.proxy.ssg import SSGClient

    creds: dict[str, Any] | None = None
    if account:
        extra = getattr(account, "additional_fields", None) or {}
        if isinstance(extra, dict) and (
            extra.get("apiKey") or extra.get("storeId") or extra.get("mallId")
        ):
            creds = extra
    else:
        creds = await _get_setting(session, "store_ssg")
    if not creds or not isinstance(creds, dict):
        return {"success": False, "message": "SSG 설정 없음"}

    api_key = creds.get("apiKey", "") or getattr(account, "api_key", "") or ""
    if not api_key:
        return {"success": False, "message": "SSG 인증키 없음"}

    store_id = (
        creds.get("storeId", "")
        or creds.get("mallId", "")
        or getattr(account, "seller_id", "")
        or SSGClient.DEFAULT_SITE_NO
    )
    client = SSGClient(api_key, site_no=store_id)
    return await _safe_delete("SSG", "ssg", product, client.delete_product)


async def _delete_cafe24(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """카페24 상품 완전 삭제 — 플러그인 delete() 위임."""
    from backend.domain.samba.plugins.markets.cafe24 import Cafe24Plugin

    product_no = product.get("market_product_no", {}).get("cafe24", "")
    if not product_no:
        return {"success": True, "message": "카페24 상품번호 없음 (건너뜀)"}
    plugin = Cafe24Plugin()
    return await plugin.delete(session, product_no, account)


async def _delete_gmarket(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """G마켓 상품 판매중지 — 플러그인 delete() 위임."""
    from backend.domain.samba.plugins.markets.gmarket import GMarketMarketPlugin

    product_no = product.get("market_product_no", {}).get("gmarket", "")
    if not product_no:
        return {"success": False, "message": "G마켓 상품번호 없음 (건너뜀)"}
    plugin = GMarketMarketPlugin()
    try:
        result = await plugin.delete(session, product_no, account)
    except Exception as e:
        err = str(e)
        if _is_delete_ghost(err):
            logger.warning(f"[G마켓] 이미 종료/삭제된 상품 — 정리 완료로 처리: {err}")
            return {
                "success": True,
                "message": f"G마켓 이미 종료됨(자동정리): {err}",
                "ghost_cleanup": True,
            }
        logger.error(f"[G마켓] 판매중지 실패: {e}")
        return {"success": False, "message": f"G마켓 판매중지 실패: {e}"}
    if not result.get("success"):
        msg = result.get("message", "")
        if _is_delete_ghost(msg):
            logger.warning(f"[G마켓] 이미 종료/삭제 응답 — 정리 완료로 처리: {msg}")
            return {
                "success": True,
                "message": f"G마켓 이미 종료됨(자동정리): {msg}",
                "ghost_cleanup": True,
            }
    return result


async def _delete_auction(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
) -> dict[str, Any]:
    """옥션 상품 판매중지 — 플러그인 delete() 위임."""
    from backend.domain.samba.plugins.markets.auction import AuctionPlugin

    product_no = product.get("market_product_no", {}).get("auction", "")
    if not product_no:
        return {"success": False, "message": "옥션 상품번호 없음 (건너뜀)"}
    plugin = AuctionPlugin()
    try:
        result = await plugin.delete(session, product_no, account)
    except Exception as e:
        err = str(e)
        if _is_delete_ghost(err):
            logger.warning(f"[옥션] 이미 종료/삭제된 상품 — 정리 완료로 처리: {err}")
            return {
                "success": True,
                "message": f"옥션 이미 종료됨(자동정리): {err}",
                "ghost_cleanup": True,
            }
        logger.error(f"[옥션] 판매중지 실패: {e}")
        return {"success": False, "message": f"옥션 판매중지 실패: {e}"}
    if not result.get("success"):
        msg = result.get("message", "")
        if _is_delete_ghost(msg):
            logger.warning(f"[옥션] 이미 종료/삭제 응답 — 정리 완료로 처리: {msg}")
            return {
                "success": True,
                "message": f"옥션 이미 종료됨(자동정리): {msg}",
                "ghost_cleanup": True,
            }
    return result


async def _delete_playauto(
    session: AsyncSession,
    product: dict[str, Any],
    account: Any = None,
    market_delete: bool = False,
) -> dict[str, Any]:
    """플레이오토 삭제.

    플레이오토는 상품 삭제 API가 없으므로 재고0+취소대기 전환이 곧 "마켓삭제"와 동치.
    따라서 market_delete 값과 무관하게 DB 정리(market_product_nos/registered_accounts)도 수행되도록
    soldout_fallback 플래그를 반환하지 않는다.

    market_delete=True (수동 마켓삭제): 재고 0 → 취소대기 처리 후 DB 정리
    market_delete=False (오토튠/리프레시 품절): 재고 0 → 취소대기 처리 후 DB 정리
    """
    from backend.domain.samba.plugins.markets.playauto import PlayAutoPlugin

    product_no = product.get("market_product_no", {}).get("playauto", "")

    if market_delete:
        if not product_no:
            return {
                "success": False,
                "message": "플레이오토 MasterCode(상품번호)를 찾을 수 없습니다. EMP에서 직접 확인해주세요.",
            }
        plugin = PlayAutoPlugin()
        result = await plugin.delete(session, product_no, account)
        if not result.get("success"):
            logger.warning(
                f"[플레이오토 마켓삭제] API 실패 (DB 정리 진행): {result.get('message', '')}"
            )
        return {"success": True, "message": "플레이오토: 취소대기 처리 후 DB 제거"}

    if not product_no:
        # 상품번호가 없으면 API 호출 불가 — DB 정리만 수행하도록 success=True 반환
        return {
            "success": True,
            "message": "플레이오토: 상품번호 없음, DB 정리만 수행",
        }

    plugin = PlayAutoPlugin()
    result = await plugin.delete(session, product_no, account)
    if not result.get("success"):
        # EMP soldout API는 판매중/수정대기/종료대기 상태만 허용 —
        # 이미 취소대기·종료·미전송 마스터는 실패하지만 재고 0은 1단계에서 시도됐고
        # 플레이오토는 상품 삭제 API가 없어 DB 정리가 곧 "마켓삭제"와 동치이므로
        # API 실패해도 DB 정리가 진행되도록 success=True 반환.
        logger.warning(
            f"[플레이오토 품절] API 실패 (DB 정리 진행): {result.get('message', '')}"
        )
    return {
        "success": True,
        "message": result.get("message", "플레이오토: 취소대기 시도 후 DB 제거"),
    }


# 마켓별 삭제 핸들러 매핑
MARKET_DELETE_HANDLERS: dict[str, Any] = {
    "smartstore": _delete_smartstore,
    "coupang": _delete_coupang,
    "11st": _delete_11st,
    "lotteon": _delete_lotteon,
    "ssg": _delete_ssg,
    "lottehome": _delete_lottehome,
    "gsshop": _delete_gsshop,
    "cafe24": _delete_cafe24,
    "playauto": _delete_playauto,
    "gmarket": _delete_gmarket,
    "auction": _delete_auction,
}
