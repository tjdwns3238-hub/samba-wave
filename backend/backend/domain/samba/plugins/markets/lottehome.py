"""롯데홈쇼핑 마켓 플러그인.

기존 dispatcher._handle_lottehome + _transform_for_lottehome 로직을 플러그인 구조로 추출.
인증 로드는 base._load_auth 가 처리하므로 execute 에서는 creds dict 사용.
"""

from __future__ import annotations

import math as _math
import re
from typing import Any

from backend.domain.samba.plugins.market_base import MarketPlugin
from backend.utils import add_lazy_loading
from backend.utils.logger import logger

# goods_no → {opt_name: item_no} 캐시 (프로세스 내 영구 유지, 재시작 시 1회만 API 조회)
_item_no_cache: dict[str, dict[str, str]] = {}


def _sanitize_image_url(url: str) -> str:
    """롯데홈쇼핑 등록용 이미지 URL 정규화.

    [1036] 확장자 오류 / [1038] 용량 초과 동시 해결:
    - LotteON CDN: `.jpg/dims/optimize/...` 변환 path 컷
    - 무신사 msscdn: `.webp` → `.jpg`, `_big/_1100.jpg` 등 대형 사이즈 → `_500.jpg`
    - query string 제거 (롯데 path 확장자 파싱 실패 방지)
    """
    if not url:
        return ""
    s = str(url).strip()
    if s.startswith("//"):
        s = f"https:{s}"
    # query string 제거 — 롯데 path 확장자 파싱 안전
    s = s.split("?", 1)[0]
    # msscdn(무신사) 전용 정규화 — 용량/확장자 동시 해결
    if "msscdn.net" in s.lower():
        # 1) `_NNN.webp` → `_NNN.jpg` (사이즈 suffix 보존)
        s = re.sub(r"(_\d+)\.webp$", r"\1.jpg", s, flags=re.IGNORECASE)
        # 2) 그 외 임의 명명 규칙의 `.webp` → `.jpg` (msscdn은 동일 path의
        #    .jpg 변형을 항상 제공 — Puma 02616601 [1036] 재발 원인)
        s = re.sub(r"\.webp$", ".jpg", s, flags=re.IGNORECASE)
        s = re.sub(r"_big\.jpg$", "_500.jpg", s, flags=re.IGNORECASE)

        def _resize(m: re.Match) -> str:
            n = int(m.group(1))
            return "_500.jpg" if n > 500 else m.group(0)

        s = re.sub(r"_(\d+)\.jpg$", _resize, s, flags=re.IGNORECASE)
        s = s.replace("/thumbnails/images/goods_img/", "/images/goods_img/")
    # LotteON CDN 등 `.jpg/dims/...` 변환 path 컷
    s = re.sub(
        r"(\.(?:jpg|jpeg|png|gif|webp))/.*$",
        r"\1",
        s,
        flags=re.IGNORECASE,
    )
    return s


async def _get_setting(session, key: str) -> Any:
    """samba_settings 테이블에서 설정값 조회 후 즉시 커밋 — idle in transaction 방지."""
    from backend.domain.samba.forbidden.model import SambaSettings
    from sqlmodel import select

    stmt = select(SambaSettings).where(SambaSettings.key == key)
    result = await session.execute(stmt)
    row = result.scalars().first()
    val = row.value if row else None
    try:
        await session.commit()
    except Exception:
        pass
    return val


async def _find_reusable_goods_no(session, product: dict[str, Any], account) -> str:
    """중복방지(issue #365): 같은 style_code 가 같은 계정에 이미 등록한 goods_no 반환.

    같은 상품(동일 제조사 style_code)이 여러 collected_product 로 중복 존재할 때,
    각 cp 가 따로 register_goods → 새 goods_no 발급 → 구·신 번호 공존 →
    주문(구 번호)과 DB(신 번호) 불일치로 미등록(원가0/오토튠누락)이 발생한다.
    신규등록 직전 같은 style_code 의 기존 goods_no(단일)를 찾아 재사용해 발급을 차단한다.
    실측: 1계정에서 같은 style_code 가 2~3 goods_no 로 중복등록 1,005건.

    단일 후보만 재사용(다중이면 이미 중복 → 모호하므로 신규 진행, 사후 매칭으로 처리).
    style_code 는 색상별 SKU 코드(II1259-200 등)라 같은 값=같은 SKU=재사용 안전.
    """
    import json as _json

    from sqlalchemy import text as _t

    _style = str(product.get("style_code") or "").strip()
    _self_id = str(product.get("id") or "")
    if not _style and _self_id:
        _r = await session.execute(
            _t("SELECT style_code FROM samba_collected_product WHERE id = :i"),
            {"i": _self_id},
        )
        _style = str(_r.scalar() or "").strip()
    if not _style or not account:
        return ""
    _acc_id = str(account.id)
    try:
        rows = (
            await session.execute(
                _t(
                    "SELECT DISTINCT market_product_nos->>:k AS gno "
                    "FROM samba_collected_product "
                    "WHERE registered_accounts @> CAST(:a AS jsonb) "
                    "AND style_code = :s "
                    "AND jsonb_exists(market_product_nos, :k) "
                    "AND id <> :self"
                ),
                {
                    "k": _acc_id,
                    "a": _json.dumps([_acc_id]),
                    "s": _style,
                    "self": _self_id,
                },
            )
        ).fetchall()
        gnos = {str(r[0]) for r in rows if r[0]}
        if len(gnos) == 1:
            return next(iter(gnos))
        if len(gnos) > 1:
            logger.warning(
                f"[롯데홈쇼핑][중복방지] style={_style} goods_no 다중 {gnos} — "
                f"모호하여 신규 진행(사후 매칭 처리)"
            )
    except Exception as e:
        logger.warning(f"[롯데홈쇼핑][중복방지] 기존 goods_no 조회 실패(무시): {e}")
    return ""


def _transform_for_lottehome(
    product: dict[str, Any],
    category_id: str,
    creds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """수집 상품 → 롯데홈쇼핑 API 형식 변환.

    API 문서: registApiGoodsInfo.lotte 파라미터 기준.
    """
    creds = creds or {}
    logger.info(
        f"[롯데홈쇼핑 변환] 정책=ec_goods_artc_cd:{creds.get('ec_goods_artc_cd')}, product.material:{product.get('material')}, product.sizeInfo:{product.get('sizeInfo')}, product.quality_guarantee:{product.get('quality_guarantee')}"
    )
    images = [_sanitize_image_url(u) for u in (product.get("images") or [])]
    images = [u for u in images if u]
    sale_price = int(product.get("sale_price", 0) or 0)
    # 추가수수료율 역산 (0 < rate < 100 가드 — 100 이상이면 0 나눗셈/음수 방지)
    extra_fee_rate = float(creds.get("extraFeeRate") or 0)
    if 0 < extra_fee_rate < 100 and sale_price > 0:
        sale_price = _math.ceil(sale_price / (1 - extra_fee_rate / 100))
    # 판매가 끝자리 0 필수 (API 에러 1062)
    if sale_price % 10 != 0:
        sale_price = (sale_price // 10 + 1) * 10

    # 마진율 (정수, 1~99) — product 우선, 없으면 정책/credentials 기본값 사용
    margin_rate = int(
        product.get("margin_rate", 0) or creds.get("margin_rate", 0) or 20
    )
    if margin_rate <= 0:
        margin_rate = 20

    # category_id가 숫자면 사용, 아니면 버림 (creds 기본값 사용)
    try:
        real_category_id = str(int(category_id)) if category_id else ""
    except (ValueError, TypeError):
        real_category_id = ""

    # MD상품군번호 — category_id(숫자) 우선, 없으면 creds에서 기본값
    md_gsgr_no = real_category_id or creds.get("md_gsgr_no", "")

    # 품목코드 — 기본 102(구두/신발), 빈 문자열이면 기본값 사용
    ec_goods_artc_cd = creds.get("ec_goods_artc_cd", "") or "102"

    _brand_mappings = creds.get("brandMappings", [])
    _product_brand = (product.get("brand") or "").strip().lower()
    _product_brand_ns = _product_brand.replace(" ", "")  # 공백 제거 버전
    _product_name = (product.get("name") or "").strip().lower()
    _product_name_ns = _product_name.replace(" ", "")
    _matched_brnd_no = None
    # 1순위: 정확 매칭 (공백 정규화 포함)
    for _m in _brand_mappings:
        _nm = (_m.get("brnd_nm") or "").strip().lower()
        _nm_ns = _nm.replace(" ", "")
        if _nm and (_nm == _product_brand or _nm_ns == _product_brand_ns):
            _matched_brnd_no = _m["brnd_no"]
            break
    # 2순위: brand 포함 매칭 (공백 정규화 포함, _product_brand 비어있으면 스킵)
    if not _matched_brnd_no and _product_brand:
        for _m in _brand_mappings:
            _nm = (_m.get("brnd_nm") or "").strip().lower()
            _nm_ns = _nm.replace(" ", "")
            if _nm and (
                _nm in _product_brand
                or _product_brand in _nm
                or _nm_ns in _product_brand_ns
                or _product_brand_ns in _nm_ns
            ):
                _matched_brnd_no = _m["brnd_no"]
                break
    # 3순위: 상품명에 브랜드명 포함 (공백 정규화 포함)
    if not _matched_brnd_no:
        for _m in _brand_mappings:
            _nm = (_m.get("brnd_nm") or "").strip().lower()
            _nm_ns = _nm.replace(" ", "")
            if _nm and (_nm in _product_name or _nm_ns in _product_name_ns):
                _matched_brnd_no = _m["brnd_no"]
                break
    if _brand_mappings and not product.get("brand_code") and not _matched_brnd_no:
        raise ValueError(
            f"브랜드 매핑 없음: '{product.get('brand', '')}' / 상품명: '{product.get('name', '')}' — 정책에서 해당 브랜드를 추가해주세요."
        )

    data: dict[str, Any] = {
        # 필수
        "brnd_no": product.get("brand_code")
        or _matched_brnd_no
        or creds.get("brnd_no", "010565"),
        "goods_nm": product.get("name", ""),
        "md_gsgr_no": md_gsgr_no,
        "pur_shp_cd": "3",  # 위탁판매
        "sale_shp_cd": "10",  # 정상
        "sale_prc": str(sale_price),
        "mrgn_rt": str(margin_rate),
        "tdf_sct_cd": "1",  # 과세
        "disp_no": real_category_id or creds.get("disp_no", ""),
        "inv_mgmt_yn": "Y",
        "item_mgmt_yn": "N",  # 옵션 있으면 아래에서 "Y"로 변경
        "inv_qty": "999",  # 옵션 있으면 제거
        "dlv_proc_tp_cd": "1",  # 업체배송
        "gift_pkg_yn": "N",
        "exch_rtgs_sct_cd": "20",  # 교환/반품 가능
        "dlv_mean_cd": "10",  # 택배
        "dlv_goods_sct_cd": "01",  # 일반상품
        "dlv_dday": "2",  # 배송기일 2일
        "byr_age_lmt_cd": "0",  # 나이제한 없음
        "dlv_polc_no": creds.get("dlv_polc_no", ""),
        "corp_dlvp_sn": creds.get("corp_dlvp_sn", ""),  # 반품지
        "corp_rls_pl_sn": creds.get("corp_rls_pl_sn", ""),  # 출고지
        "orpl_nm": product.get("origin", "") or "해외",
        "mfcp_nm": product.get("manufacturer", "")
        or product.get("brand", "")
        or "상세페이지 참조",
        "img_url": images[0] if images else "",
        "dtl_info_fcont": add_lazy_loading(
            product.get("detail_html", "") or f"<p>{product.get('name', '')}</p>"
        ),
        "sum_pkg_psb_yn": "N",
        "ec_goods_artc_cd": ec_goods_artc_cd,
        "cdl_yn": "Y",  # 업체직송
        "cdl_goods_std": "30",  # 중형
        "prl_imp_yn": "N",
        "price_site_yn": "Y",
    }

    # 옵션 처리 (단품관리) — 사이즈 순서대로 정렬
    options = product.get("options") or []
    if options:
        opt_group_name = product.get("option_group_name") or "옵션"
        item_parts = []
        max_stock = int(product.get("_max_stock") or 0)
        logger.info(
            f"[롯데홈쇼핑 옵션] options 개수={len(options)}, max_stock={max_stock}"
        )

        # 사이즈 순서대로 정렬 (영어/숫자/한글 모두 지원)
        def get_size_order_key(opt_name):
            opt_name = str(opt_name).strip()

            # 영어 사이즈
            EN_SIZE = ["XS", "S", "M", "L", "XL", "2XL", "3XL", "4XL", "5XL"]
            if opt_name in EN_SIZE:
                return (0, EN_SIZE.index(opt_name), opt_name)

            # 숫자 사이즈 (230, 235, 240, ...)
            try:
                num = int(opt_name)
                return (1, num, opt_name)
            except ValueError:
                pass

            # 접두어/사이즈 형식 (A/M, A/XL, B/2XL 등) — "/" 뒤 사이즈 기준 정렬
            if "/" in opt_name:
                size_part = opt_name.split("/")[-1].strip()
                return get_size_order_key(size_part)

            # 혼합 사이즈 (S-M, M-L, ...) — 첫 번째 부분으로 정렬
            if "-" in opt_name:
                first_part = opt_name.split("-")[0].strip()
                return get_size_order_key(first_part)

            # 한글 사이즈 (소, 중, 대, ...)
            KR_SIZE = [
                "XXS",
                "XS",
                "S",
                "M",
                "L",
                "XL",
                "XXL",
                "3XL",
                "4XL",
                "5XL",
                "소",
                "중",
                "대",
                "특",
            ]
            if opt_name in KR_SIZE:
                return (2, KR_SIZE.index(opt_name), opt_name)

            # 알 수 없는 사이즈는 맨 뒤
            return (999, 0, opt_name)

        # 옵션을 사이즈 순서대로 정렬
        sorted_options = sorted(
            options,
            key=lambda o: get_size_order_key(
                str(o.get("name") or o.get("size") or o.get("value") or "").strip()
            ),
        )

        for opt in sorted_options:
            opt_name = str(
                opt.get("name")
                or opt.get("value")
                or opt.get("size")
                or opt.get("optionName")
                or ""
            ).strip()
            if not opt_name:
                continue
            is_sold_out = bool(opt.get("isSoldOut") or opt.get("sold_out"))
            raw_stock = opt.get("stock")
            stock_val = max(0, int(raw_stock)) if raw_stock is not None else None
            # 품절이거나 재고 0이면 옵션 제외
            if is_sold_out or (stock_val is not None and stock_val == 0):
                logger.info(
                    f"[롯데홈쇼핑 옵션 제외] {opt_name}: isSoldOut={is_sold_out}, stock={stock_val}"
                )
                continue
            if stock_val is not None:
                stock = str(min(stock_val, max_stock) if max_stock > 0 else stock_val)
            else:
                stock = str(max_stock) if max_stock > 0 else "999"
            managed_code = str(
                opt.get("managedCode")
                or opt.get("managed_code")
                or opt.get("itemCode")
                or opt_name
            ).strip()
            logger.info(f"[롯데홈쇼핑 옵션 등록] {opt_name}: stock={stock}")
            item_parts.append(f"{opt_name},{stock},{managed_code}")
        if item_parts:
            data["item_mgmt_yn"] = "Y"
            data["opt_nm"] = opt_group_name
            data["item_list"] = ":".join(item_parts)
            data.pop("inv_qty", None)

    # 부가이미지 (최대 5장)
    for i, img in enumerate(images[1:6], start=1):
        data[f"img_url{i}"] = img

    # 추가배송비정책 (도서산간/제주)
    ismr_dlv_polc_no = creds.get("ismr_dlv_polc_no", "")
    if ismr_dlv_polc_no:
        data["ismr_dlv_polc_no"] = str(ismr_dlv_polc_no)

    # 품목별 항목정보 (구두/신발 102 기본값)
    if ec_goods_artc_cd == "102":
        color = (
            product.get("color", "") or creds.get("item_color", "") or "상세페이지 참조"
        )
        material = (
            product.get("material", "")
            or creds.get("item_material", "")
            or "상세페이지 참조"
        )
        size = (
            product.get("sizeInfo", "")
            or creds.get("item_size", "")
            or "상세페이지 참조"
        )
        washing = (
            product.get("care_instructions", "")
            or creds.get("item_washing", "")
            or "상세페이지 참조"
        )
        import_yn = creds.get("item_import", "N")
        quality = (
            product.get("quality_guarantee", "")
            or creds.get("item_quality", "")
            or "관련 법 및 소비자 분쟁 해결 기준을 따름"
        )
        as_info = creds.get("item_as", "") or "상세페이지 참조"

        data["10030"] = color
        data["10078"] = material
        data["10102"] = washing
        data["10104"] = size
        data["10041_RD"] = import_yn
        data["10041"] = import_yn
        data["10116_RD"] = creds.get("item_quality_rd", "1")
        data["10116"] = quality
        data["10001"] = as_info
    elif ec_goods_artc_cd == "101":  # 의류
        material = (
            product.get("material", "")
            or creds.get("item_material", "")
            or "상세페이지 참조"
        )
        color = (
            product.get("color", "") or creds.get("item_color", "") or "상세페이지 참조"
        )
        size = (
            product.get("sizeInfo", "")
            or creds.get("item_size", "")
            or "상세페이지 참조"
        )
        washing = (
            product.get("care_instructions", "")
            or creds.get("item_washing", "")
            or "상세페이지 참조"
        )
        import_yn = creds.get("item_import", "N")
        mfg_date = creds.get("item_mfg_date", "") or "상세페이지 참조"
        quality = (
            product.get("quality_guarantee", "")
            or creds.get("item_quality", "")
            or "관련 법 및 소비자 분쟁 해결 기준을 따름"
        )
        as_info = creds.get("item_as", "") or "상세페이지 참조"

        logger.info(
            f"[의류 품목정보] material={material}, color={color}, size={size}, quality={quality}"
        )

        data["10030"] = color
        data["10078"] = material
        data["10035"] = washing
        data["10104"] = size
        data["10041_RD"] = import_yn
        data["10041"] = import_yn
        data["10073"] = mfg_date
        data["10116_RD"] = creds.get("item_quality_rd", "1")
        data["10116"] = quality
        data["10001"] = as_info

    logger.info(
        f"[롯데홈쇼핑 최종 데이터] 10078(재질)={data.get('10078')}, 10104(사이즈)={data.get('10104')}, 10116(품질)={data.get('10116')}, ismr_dlv_polc_no={data.get('ismr_dlv_polc_no')}"
    )
    return data


class LotteHomePlugin(MarketPlugin):
    market_type = "lottehome"
    policy_key = "롯데홈쇼핑"
    required_fields = ["name", "sale_price"]

    def _validate_category(self, category_id: str) -> str:
        # 롯데홈쇼핑은 category_id 없어도 정책의 disp_no 사용하므로 통과
        return category_id or "lottehome_policy"

    def transform(self, product: dict, category_id: str, **kwargs) -> dict:
        """상품 데이터 → 롯데홈쇼핑 API 포맷 변환."""
        creds = kwargs.get("creds", {})
        return _transform_for_lottehome(product, category_id, creds)

    async def execute(
        self,
        session,
        product: dict,
        creds: dict,
        category_id: str,
        account,
        existing_no: str,
    ) -> dict[str, Any]:
        """롯데홈쇼핑 상품 등록 — 전체 로직."""
        from backend.domain.samba.proxy.lottehome import LotteHomeClient

        # 정책(maxStock 등) 주입
        product = await self._apply_market_settings(session, product, account)
        logger.info(f"[롯데홈쇼핑 DEBUG] product={product}, category_id={category_id}")

        # account.additional_fields 우선, creds(base._load_auth) 보완
        auth_creds: dict[str, Any] = dict(creds)
        if account:
            extra = getattr(account, "additional_fields", None) or {}
            if extra.get("userId") or extra.get("password") or extra.get("agncNo"):
                auth_creds = {**auth_creds, **extra}
            elif getattr(account, "seller_id", None):
                auth_creds.setdefault("userId", account.seller_id)
                auth_creds.setdefault("password", extra.get("password", ""))
                auth_creds.setdefault("agncNo", extra.get("agncNo", account.seller_id))
                auth_creds.setdefault("env", extra.get("env", "test"))

        # credentials 로드 (인증 정보)
        creds_setting = await _get_setting(session, "lottehome_credentials")
        if creds_setting and isinstance(creds_setting, dict):
            auth_creds = {**auth_creds, **creds_setting}

        # store_lottehome(설정 페이지) → lottehome_policy(정책 페이지) 순으로 로드,
        # 뒤에 로드한 값이 우선(정책 페이지가 최종 override). 빈 값은 무시.
        # (2026-05-25) resolver 위임 + account.tenant_id 자동 추출.
        from backend.domain.samba.account.resolver import resolve_market_creds

        _tid = getattr(account, "tenant_id", None) if account else None
        store_lh = await resolve_market_creds(
            session, _tid, market_type="lottehome", store_key="store_lottehome"
        )
        store_lh = store_lh if isinstance(store_lh, dict) else {}
        policy = await _get_setting(session, "lottehome_policy")
        policy = policy if isinstance(policy, dict) else {}

        # account.additional_fields 의 camelCase 값도 폴백 소스로 사용
        # (배송정책/출고지/반품지 등은 계정별 설정인데 store_lh 보다 우선 고려)
        account_extra: dict[str, Any] = {}
        if account:
            ae = getattr(account, "additional_fields", None) or {}
            if isinstance(ae, dict):
                account_extra = ae

        def _pick(*keys: str) -> str:
            """policy → account.additional_fields → store_lh 순으로 첫 non-empty 값 반환."""
            for src in (policy, account_extra, store_lh):
                for k in keys:
                    v = src.get(k, "")
                    if v:
                        return str(v)
            return ""

        _field_map = {
            "md_gsgr_no": ("mdGsgrNo",),
            "disp_no": ("dispNo",),
            "dlv_polc_no": ("dlvPolcNo",),
            "ismr_dlv_polc_no": ("addDlvPolcNo",),
            "corp_rls_pl_sn": ("corpRlsPlSn",),
            "corp_dlvp_sn": ("corpDlvpSn",),
            "brnd_no": ("brndNo",),
            "margin_rate": ("marginRate",),
            "extraFeeRate": ("extraFeeRate",),
            "ec_goods_artc_cd": ("ecGoodsArtcCd",),
            "item_material": ("itemMaterial",),
            "item_color": ("itemColor",),
            "item_size": ("itemSize",),
            "item_import": ("itemImport",),
            "item_import_note": ("itemImportNote",),
            "item_washing": ("itemWashing",),
            "item_mfg_date": ("itemMfgDate",),
            "item_quality": ("itemQuality",),
            "item_quality_note": ("itemQualityNote",),
            "item_quality_rd": ("itemQualityRd",),
            "item_as": ("itemAs",),
        }
        for dest, src_keys in _field_map.items():
            val = _pick(*src_keys)
            if val:
                auth_creds[dest] = val
        auth_creds.setdefault("item_quality_rd", "1")
        brand_mappings = (
            policy.get("brandMappings") or store_lh.get("brandMappings") or []
        )
        if brand_mappings:
            auth_creds["brandMappings"] = brand_mappings
        if policy or store_lh:
            logger.info(
                f"[롯데홈쇼핑 정책 로드] ec_goods_artc_cd={auth_creds.get('ec_goods_artc_cd')}, md_gsgr_no={auth_creds.get('md_gsgr_no')}, disp_no={auth_creds.get('disp_no')}, dlv_polc_no={auth_creds.get('dlv_polc_no')}, add_dlv_polc_no={auth_creds.get('add_dlv_polc_no')}, item_material={auth_creds.get('item_material')}, item_size={auth_creds.get('item_size')}, item_quality={auth_creds.get('item_quality')}"
            )
        if not auth_creds:
            return {"success": False, "message": "롯데홈쇼핑 설정이 없습니다."}

        user_id = auth_creds.get("userId", "") or (
            getattr(account, "seller_id", "") if account else ""
        )
        password = auth_creds.get("password", "")
        agnc_no = auth_creds.get("agncNo", "")
        env = auth_creds.get("env", "test")

        if not user_id or not password:
            return {
                "success": False,
                "message": "롯데홈쇼핑 userId/password가 없습니다.",
            }

        client = LotteHomeClient(user_id, password, agnc_no, env)

        # 오토튠 가격/재고만 업데이트 — 전용 API 사용 (전체 재등록 불필요)
        if existing_no and product.get("_skip_image_upload"):
            results = {"success": True, "updated": []}
            sale_price = int(product.get("sale_price", 0) or 0)
            extra_fee_rate = float(auth_creds.get("extraFeeRate") or 0)
            if 0 < extra_fee_rate < 100 and sale_price > 0:
                sale_price = _math.ceil(sale_price / (1 - extra_fee_rate / 100))
            if sale_price % 10 != 0:
                sale_price = (sale_price // 10 + 1) * 10

            # 가격 업데이트
            if sale_price > 0:
                try:
                    margin_rate = (
                        int(
                            product.get("margin_rate", 0)
                            or auth_creds.get("margin_rate", 0)
                            or 0
                        )
                        or 20
                    )
                    price_result = await client.update_price(
                        existing_no, sale_price, margin_rate
                    )
                    # <Result> 코드 기반 분기 (#351): 1=즉시승인, 3=MD승인 대기, 2=실패
                    if price_result.get("ok"):
                        if price_result.get("approval") == "md_pending":
                            # MD승인 대기 — 접수됐으나 미반영. 재전송 폭주 방지 위해
                            # 성공으로 두되 'price_pending' 으로 구분(반영완료 아님).
                            results["updated"].append("price_pending")
                            logger.info(
                                f"[롯데홈쇼핑] 가격 MD승인 대기: {existing_no} → "
                                f"{sale_price}원 (승인 후 반영)"
                            )
                        else:
                            results["updated"].append("price")
                            logger.info(
                                f"[롯데홈쇼핑] 가격 업데이트 완료(즉시승인): {existing_no} → {sale_price}원"
                            )
                    else:
                        # Result=2(실패)/불명 — 더 이상 false success 로 묻지 않음
                        _pmsg = price_result.get("message") or "Result 불명"
                        results["success"] = False
                        results["message"] = f"롯데홈쇼핑 가격 수정 실패: {_pmsg}"
                        logger.warning(
                            f"[롯데홈쇼핑] 가격 업데이트 실패: {existing_no} → {_pmsg}"
                        )
                except Exception as e:
                    logger.error(f"[롯데홈쇼핑] 가격 업데이트 오류: {e}")

            # 재고 업데이트 (옵션별)
            source_options = product.get("options") or []
            if source_options:
                try:
                    # 캐시 우선 — 없을 때만 searchGoodsViewListOpenApi 호출
                    # (searchStockList는 33MB 응답으로 타임아웃 발생 → goods view로 대체)
                    # 멤버십 체크(in)로 음성캐시 동작 보장 — 빈 dict 는 falsy 라
                    # truthiness 로 판단하면 옵션 매칭 0건 상품을 매 사이클 재호출하게 됨.
                    if existing_no in _item_no_cache:
                        item_no_map: dict[str, str] = _item_no_cache[existing_no]
                        logger.debug(
                            f"[롯데홈쇼핑] item_no_map 캐시 히트: {existing_no} ({len(item_no_map)}개 옵션)"
                        )
                    else:
                        item_no_map = {}
                        goods_view = await client.search_goods_view(existing_no)
                        # 응답 구조 방어 — 형제 호출처(qa_poller/collector_autotune)와 동일하게
                        # Result/GoodsInfo 래퍼가 없는 응답도 fallback 으로 흡수(엄격 체이닝 시
                        # 래퍼 없으면 {} → 재고 스킵되어 원버그 재현).
                        data = goods_view.get("data", {})
                        result_data = (
                            data.get("Result", data) if isinstance(data, dict) else {}
                        )
                        goods_info = (
                            result_data.get("GoodsInfo", result_data)
                            if isinstance(result_data, dict)
                            else {}
                        )
                        if not isinstance(goods_info, dict):
                            goods_info = {}
                        item_info_list = goods_info.get("ItemInfo", [])
                        if isinstance(item_info_list, dict):
                            item_info_list = [item_info_list]

                        lotte_opt_names: set[str] = set()
                        for it in (
                            item_info_list if isinstance(item_info_list, list) else []
                        ):
                            corp_item_no = str(it.get("CorpItemNo", "")).strip()
                            item_no = str(it.get("ItemNo", "")).strip()
                            if corp_item_no and item_no:
                                item_no_map[corp_item_no] = item_no
                                lotte_opt_names.add(corp_item_no)

                        # 빈 결과도 음성캐시로 저장 → 다음 사이클 재호출 방지
                        # (search_goods_view 매 사이클 폭주 차단, IP 차단 예방).
                        # 옵션이 나중에 등록되면 프로세스 재시작(배포) 시 갱신됨.
                        _item_no_cache[existing_no] = item_no_map
                        if item_no_map:
                            logger.info(
                                f"[롯데홈쇼핑] item_no_map 캐시 저장: {existing_no} → {lotte_opt_names}"
                            )
                        else:
                            logger.info(
                                f"[롯데홈쇼핑] item_no_map 빈 결과 음성캐시: {existing_no} "
                                f"(옵션 매칭 0건, 재호출 방지)"
                            )

                    if not item_no_map:
                        logger.warning(
                            f"[롯데홈쇼핑] item_no_map 비어있음 — 재고 업데이트 건너뜀 ({existing_no})"
                        )
                    else:
                        # 소싱사이트 옵션명 → stock 맵핑
                        source_opt_map: dict[str, int] = {}
                        for opt in source_options:
                            opt_name = str(
                                opt.get("name")
                                or opt.get("value")
                                or opt.get("size")
                                or ""
                            ).strip()
                            if not opt_name:
                                continue
                            is_sold_out = bool(
                                opt.get("isSoldOut") or opt.get("sold_out")
                            )
                            raw_stock = opt.get("stock")
                            stock_val = (
                                0
                                if is_sold_out
                                else (
                                    max(0, int(raw_stock))
                                    if raw_stock is not None
                                    else 0
                                )
                            )
                            source_opt_map[opt_name] = stock_val

                        # 롯데에 있는 옵션 → 소싱사이트 재고로 업데이트
                        # 롯데에 있지만 소싱사이트에 없는 옵션 → 재고 0 (품절)
                        # registStock: inv_qty=0이면 ItemSaleStatCd=20(품절) 자동 설정.
                        # inv_qty>0은 InvQty만 업데이트되며 ItemSaleStatCd는 변경되지 않음.
                        max_stock = int(product.get("_max_stock") or 0)
                        stock_updated = False
                        for lotte_opt, item_no in item_no_map.items():
                            stock_val = source_opt_map.get(
                                lotte_opt, 0
                            )  # 소싱에 없으면 0
                            if max_stock > 0:
                                stock_val = min(stock_val, max_stock)
                            logger.info(
                                f"[롯데홈쇼핑] 재고 전송: {lotte_opt} item_no={item_no} stock={stock_val}"
                            )
                            try:
                                await client.update_stock(
                                    existing_no, item_no, stock_val
                                )
                                stock_updated = True
                            except Exception as _se:
                                logger.error(
                                    f"[롯데홈쇼핑] 재고 업데이트 오류: {lotte_opt} → {_se}"
                                )

                        if stock_updated:
                            results["updated"].append("stock")
                            logger.info(
                                f"[롯데홈쇼핑] 재고 업데이트 완료: {existing_no}"
                            )

                        # 소싱사이트 신규 옵션은 자동 추가 안 함 — 롯데 기등록 옵션만 관리

                except Exception as e:
                    logger.error(f"[롯데홈쇼핑] 재고 업데이트 오류: {e}")

            return results

        # 반품지/출고지/배송정책 자동 조회 (auth_creds에 없으면)
        if (
            not auth_creds.get("corp_dlvp_sn")
            or not auth_creds.get("corp_rls_pl_sn")
            or not auth_creds.get("dlv_polc_no")
        ):
            try:
                # 출고지/반품배송지 조회
                places = await client.search_return_places()
                place_data = places.get("data", {})
                place_result = place_data.get("Result", place_data)
                place_list = place_result.get(
                    "DlvPlcList", place_result.get("DlvpList", {})
                )
                items = place_list.get("DlvPlcInfo", place_list.get("DlvpInfo", []))
                if isinstance(items, dict):
                    items = [items]
                for item in items if isinstance(items, list) else []:
                    tp = item.get("dlvp_tp_cd", "")
                    sn = item.get("corp_dlvp_sn", "")
                    if tp in ("10", "30") and not auth_creds.get("corp_dlvp_sn") and sn:
                        auth_creds["corp_dlvp_sn"] = sn  # 반품지
                        logger.info(f"[롯데홈쇼핑] 반품지 자동 조회: {sn}")
                    if (
                        tp in ("40", "50")
                        and not auth_creds.get("corp_rls_pl_sn")
                        and sn
                    ):
                        auth_creds["corp_rls_pl_sn"] = sn  # 출고지
                        logger.info(f"[롯데홈쇼핑] 출고지 자동 조회: {sn}")
                # 배송정책 조회
                policies = None
                if not auth_creds.get("dlv_polc_no") or not auth_creds.get(
                    "add_dlv_polc_no"
                ):
                    policies = await client.search_delivery_policies()
                    pol_data = policies.get("data", {})
                    pol_result = pol_data.get("Result", pol_data)
                    pol_list = pol_result.get(
                        "DlvPolcList", pol_result.get("DlvPolcInfo", {})
                    )
                    pol_items = (
                        pol_list.get("DlvPolcInfo", [])
                        if isinstance(pol_list, dict)
                        else pol_list
                    )
                    if isinstance(pol_items, dict):
                        pol_items = [pol_items]
                    if isinstance(pol_items, list) and pol_items:
                        if not auth_creds.get("dlv_polc_no"):
                            auth_creds["dlv_polc_no"] = pol_items[0].get(
                                "dlv_polc_no", ""
                            )
                            logger.info(
                                f"[롯데홈쇼핑] 배송정책 자동 조회: {auth_creds['dlv_polc_no']}"
                            )
                        if not auth_creds.get("add_dlv_polc_no") and len(pol_items) > 1:
                            auth_creds["add_dlv_polc_no"] = pol_items[1].get(
                                "dlv_polc_no", ""
                            ) or pol_items[1].get("add_dlv_polc_no", "")
                            logger.info(
                                f"[롯데홈쇼핑] 추가배송비 자동 조회: {auth_creds['add_dlv_polc_no']}"
                            )
            except Exception as e:
                logger.warning(f"[롯데홈쇼핑] 배송지/정책 자동 조회 실패: {e}")

        # 이미지 정규화 — 롯데홈쇼핑 [1036] 확장자 / [1038] 용량 동시 대응
        # 1) mirror_external_to_r2: msscdn/lotteon CDN 등 차단/위장-확장자 이미지를
        #    R2로 선미러 + PIL JPEG 강제 변환 → 매직바이트 ≠ ContentType 거절 차단
        #    (`.webp` URL을 sanitize 가 `.jpg` 로 rename 만 해도 msscdn 이 webp 본문을
        #    그대로 응답 → 롯데홈 매직바이트 검증 실패 → 1036 재발. 11번가와 동일 패턴.)
        # 2) mirror_oversized_to_r2: 그 외 도매업체 CDN(yswholesale 등) 900KB 초과분
        #    리사이즈 — 1038 방어.
        try:
            from backend.domain.samba.image.service import ImageTransformService

            _img_svc = ImageTransformService(session)
            _images = product.get("images") or []
            _detail_images = product.get("detail_images") or []
            _detail_html = product.get("detail_html") or ""
            if _images or _detail_images or _detail_html:
                product = dict(product)  # 원본 dict 변형 방지
                _pid = product.get("id")
                if _images:
                    _imgs1, _ = await _img_svc.mirror_with_persistence(_pid, _images)
                    product["images"], _, _ = await _img_svc.mirror_oversized_to_r2(
                        _imgs1
                    )
                if _detail_images:
                    _dimgs1, _ = await _img_svc.mirror_with_persistence(
                        _pid, _detail_images
                    )
                    (
                        product["detail_images"],
                        _,
                        _,
                    ) = await _img_svc.mirror_oversized_to_r2(_dimgs1)
                if _detail_html:
                    _html1 = await _img_svc.mirror_urls_in_html(_detail_html)
                    product["detail_html"] = await _img_svc.mirror_oversized_in_html(
                        _html1
                    )
        except Exception as e:
            logger.warning(f"[롯데홈쇼핑] 이미지 정규화 단계 오류 — 원본 유지: {e}")

        goods_data = _transform_for_lottehome(product, category_id, auth_creds)

        # 진단: 전송 직전 img_url 캡처 + AI 이미지 여부 표시 (transformed/ai_ 패턴)
        def _img_tag(url: str | None) -> str:
            if not url:
                return "-"
            tag = "AI" if ("/transformed/" in url or "/ai_" in url) else "ORIG"
            ext = url.rsplit(".", 1)[-1].split("?")[0][:6] if "." in url else "?"
            return f"[{tag}/.{ext}] {url}"

        logger.info(
            f"[롯데홈쇼핑 진단/REQ] img_url={_img_tag(goods_data.get('img_url'))}, "
            f"img_url1={_img_tag(goods_data.get('img_url1'))}, "
            f"img_url2={_img_tag(goods_data.get('img_url2'))}, "
            f"img_url3={_img_tag(goods_data.get('img_url3'))}, "
            f"img_url4={_img_tag(goods_data.get('img_url4'))}, "
            f"img_url5={_img_tag(goods_data.get('img_url5'))}"
        )
        # P2 중복방지(issue #365): 같은 style_code 가 이미 이 계정에 등록돼 있으면
        # register_goods(새 goods_no 발급)를 스킵하고 기존 goods_no 를 공유한다.
        # → 중복 goods_no 발급 차단(주문 미매칭 근원 제거). 실제 등록 API 호출 0건.
        _reuse_no = ""
        if not existing_no and account:
            _reuse_no = await _find_reusable_goods_no(session, product, account)
        if _reuse_no:
            goods_no = _reuse_no
            result = {"data": {"_reused_goods_no": _reuse_no}}
            logger.info(
                f"[롯데홈쇼핑][중복방지] style 동일 기존 goods_no={goods_no} 공유 "
                f"— 신규등록 스킵(중복 발급 차단)"
            )
        else:
            result = await client.register_goods(goods_data)
            # 진단: 응답 raw XML 전체 로그 — 이미지 거부 메시지 있는지 확인용
            logger.info(
                f"[롯데홈쇼핑 진단/RES] rawXml={result.get('rawXml', '')[:4000]}"
            )
            logger.info(f"[롯데홈쇼핑 진단/RES_DATA] data={result.get('data', {})}")

            # 상품번호 추출
            g_data = result.get("data", {})
            g_result = g_data.get("GoodsResults", g_data.get("Result", g_data))
            goods_no = ""
            if isinstance(g_result, dict):
                goods_no = g_result.get("goods_no", "") or g_result.get("Result", "")

        # DB에 등록 정보 저장 (registered_accounts, market_product_nos)
        if goods_no and account:
            try:
                from backend.domain.samba.collector.model import SambaCollectedProduct
                from sqlmodel import select

                # 현재 상품 조회
                stmt = select(SambaCollectedProduct).where(
                    SambaCollectedProduct.id == product.get("id")
                )
                result_db = await session.execute(stmt)
                collected = result_db.scalars().first()

                if collected:
                    # registered_accounts 업데이트
                    reg_accts = list(collected.registered_accounts or [])
                    if account.id not in reg_accts:
                        reg_accts.append(account.id)
                        collected.registered_accounts = reg_accts

                    # market_product_nos 업데이트
                    market_nos = dict(collected.market_product_nos or {})
                    market_nos[account.id] = goods_no
                    # MD 승인 대기 상태 표시
                    market_nos[f"{account.id}_qa"] = "pending"
                    collected.market_product_nos = market_nos

                    # 롯데에 실제로 등록한 상품명으로 업데이트
                    lotte_product_name = goods_data.get("goods_nm", "")
                    if lotte_product_name and collected.name != lotte_product_name:
                        collected.name = lotte_product_name

                    # 상품 상태 업데이트
                    if collected.status != "registered":
                        collected.status = "registered"

                    session.add(collected)
                    await session.commit()
                    logger.info(
                        f"[롯데홈쇼핑] DB 저장 완료: {collected.id} → "
                        f"registered_accounts={reg_accts}, "
                        f"market_product_nos={market_nos}"
                    )
            except Exception as e:
                logger.warning(f"[롯데홈쇼핑] DB 저장 실패: {e}")

        return {
            "success": True,
            "message": "롯데홈쇼핑 등록 성공",
            "data": result,
            "goodsNo": goods_no,
            "qa_pending": True,  # 등록 후 MD 승인 대기 상태
        }
