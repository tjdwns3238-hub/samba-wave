"""마켓 계정 자격증명 표준 접근자 (2026-05-25).

`samba_market_account` 행을 마켓별 표준 dict 로 변환한다.
기존 `store_*` samba_settings 단일 키 구조와 동일한 키 명명을 유지해 호출자 코드 변경 최소화.

설계 원칙:
- 각 마켓의 cred dict 키는 camelCase (기존 store_* JSON 호환)
- api_key/api_secret/seller_id 는 컬럼에서 추출
- 그 외 마켓별 필드는 additional_fields(JSON) 에서 추출
- OAuth 토큰은 oauth_* 별도 컬럼에서 추출
- account 가 None 이면 빈 dict 반환 → 호출자가 폴백/에러 처리

표준 키 명세는 backend/domain/samba/account/credentials.py 상단 주석 표 참조.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from backend.domain.samba.account.model import SambaMarketAccount


def _extras(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    if account is None:
        return {}
    extras = getattr(account, "additional_fields", None)
    return extras if isinstance(extras, dict) else {}


def lotteon_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """롯데ON — apiKey + 배송인프라(dvCstPolNo/owhpNo/rtrpNo)."""
    if account is None:
        return {}
    ext = _extras(account)
    return {
        "apiKey": account.api_key or "",
        "dvCstPolNo": ext.get("dvCstPolNo", ""),
        "owhpNo": ext.get("owhpNo", ""),
        "rtrpNo": ext.get("rtrpNo", ""),
    }


def elevenst_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """11번가 — apiKey 단일."""
    if account is None:
        return {}
    return {"apiKey": account.api_key or ""}


def coupang_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """쿠팡 — accessKey + secretKey + vendorId."""
    if account is None:
        return {}
    return {
        "accessKey": account.api_key or "",
        "secretKey": account.api_secret or "",
        "vendorId": account.seller_id or "",
    }


def ssg_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """SSG — clientId + clientSecret + sellerId + 부속 정책."""
    if account is None:
        return {}
    ext = _extras(account)
    return {
        "clientId": account.api_key or "",
        "clientSecret": account.api_secret or "",
        "sellerId": account.seller_id or "",
        "brandList": ext.get("brandList", []),
        "shippingPolicyId": ext.get("shippingPolicyId", ""),
        "outboundAddressId": ext.get("outboundAddressId", ""),
        "inboundAddressId": ext.get("inboundAddressId", ""),
    }


def smartstore_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """스마트스토어 — clientId + clientSecret."""
    if account is None:
        return {}
    return {
        "clientId": account.api_key or "",
        "clientSecret": account.api_secret or "",
    }


def gsshop_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """GSShop — supCd + aesKey + dev/prod 키 + subSupCd + env."""
    if account is None:
        return {}
    ext = _extras(account)
    return {
        "supCd": account.seller_id or "",
        "aesKey": account.api_key or "",
        "apiKeyDev": ext.get("apiKeyDev", ""),
        "apiKeyProd": ext.get("apiKeyProd", ""),
        "subSupCd": ext.get("subSupCd", ""),
        "env": ext.get("env", "dev"),
    }


def lottehome_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """롯데홈쇼핑 — userId + password + agncNo + env."""
    if account is None:
        return {}
    ext = _extras(account)
    return {
        "userId": account.api_key or "",
        "password": account.api_secret or "",
        "agncNo": account.seller_id or "",
        "env": ext.get("env", "prod"),
    }


def playauto_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """플레이오토 — apiKey + hostingId."""
    if account is None:
        return {}
    return {
        "apiKey": account.api_key or "",
        "hostingId": account.seller_id or "",
    }


def ebay_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """eBay — clientId + clientSecret + devId + OAuth + 정책 ID."""
    if account is None:
        return {}
    ext = _extras(account)
    return {
        "clientId": account.api_key or "",
        "clientSecret": account.api_secret or "",
        "devId": ext.get("devId", ""),
        "oauthToken": account.oauth_access_token or "",
        "refreshToken": account.oauth_refresh_token or "",
        "fulfillmentPolicyId": ext.get("fulfillmentPolicyId", ""),
        "paymentPolicyId": ext.get("paymentPolicyId", ""),
        "returnPolicyId": ext.get("returnPolicyId", ""),
    }


def cafe24_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """카페24 — mallId + OAuth(clientId/clientSecret/access/refresh)."""
    if account is None:
        return {}
    return {
        "mallId": account.seller_id or "",
        "clientId": account.api_key or "",
        "clientSecret": account.api_secret or "",
        "accessToken": account.oauth_access_token or "",
        "refreshToken": account.oauth_refresh_token or "",
    }


def amazon_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """Amazon — clientId + clientSecret + storeId + region + refreshToken."""
    if account is None:
        return {}
    ext = _extras(account)
    return {
        "clientId": account.api_key or "",
        "clientSecret": account.api_secret or "",
        "storeId": account.seller_id or "",
        "region": ext.get("region", "fe"),
        "refreshToken": account.oauth_refresh_token or "",
    }


def esm_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """옥션/G마켓(ESMPlus) — apiKey + sellerId."""
    if account is None:
        return {}
    return {
        "apiKey": account.api_key or "",
        "sellerId": account.seller_id or "",
    }


def kream_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """KREAM — token + cookie (additional_fields 에 저장)."""
    if account is None:
        return {}
    ext = _extras(account)
    return {
        "token": ext.get("token", ""),
        "cookie": ext.get("cookie", ""),
    }


def musinsa_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """무신사 — cookie (additional_fields 에 저장)."""
    if account is None:
        return {}
    ext = _extras(account)
    return {"cookie": ext.get("cookie", "")}


# 마켓 → 빌더 매핑 — 동적 디스패치용
CRED_BUILDERS = {
    "lotteon": lotteon_creds,
    "11st": elevenst_creds,
    "elevenst": elevenst_creds,
    "coupang": coupang_creds,
    "ssg": ssg_creds,
    "smartstore": smartstore_creds,
    "gsshop": gsshop_creds,
    "lottehome": lottehome_creds,
    "playauto": playauto_creds,
    "ebay": ebay_creds,
    "cafe24": cafe24_creds,
    "amazon": amazon_creds,
    "auction": esm_creds,
    "gmarket": esm_creds,
    "esm": esm_creds,
    "kream": kream_creds,
    "musinsa": musinsa_creds,
}


def build_creds(account: Optional["SambaMarketAccount"]) -> dict[str, Any]:
    """account.market_type 에 따라 적절한 빌더 호출. 매핑 없으면 빈 dict."""
    if account is None:
        return {}
    builder = CRED_BUILDERS.get((account.market_type or "").lower())
    if builder is None:
        return {}
    return builder(account)
