"""SambaWave Account service."""

from typing import Any, Dict, List, Optional

from backend.domain.samba.account.model import SambaMarketAccount
from backend.domain.samba.account.repository import SambaMarketAccountRepository

# Ported from js/modules/account.js supportedMarkets
SUPPORTED_MARKETS: List[Dict[str, Any]] = [
    # Domestic
    {
        "id": "auction",
        "name": "옥션",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    {
        "id": "gmarket",
        "name": "G마켓",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    {
        "id": "11st",
        "name": "11번가",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    {
        "id": "smartstore",
        "name": "스마트스토어",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret", "clientId"],
    },
    {
        "id": "coupang",
        "name": "쿠팡",
        "group": "국내",
        "api_fields": ["accessKey", "secretKey", "vendorCode"],
    },
    {
        "id": "gsshop",
        "name": "GS샵",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    {
        "id": "lotteon",
        "name": "롯데ON",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    {
        "id": "ssg",
        "name": "신세계몰",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret", "mallId"],
    },
    {
        "id": "lottehome",
        "name": "롯데홈쇼핑",
        "group": "국내",
        "api_fields": ["userId", "password", "agncNo"],
    },
    {
        "id": "homeand",
        "name": "홈앤쇼핑",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    {
        "id": "hmall",
        "name": "HMALL",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    {
        "id": "toss",
        "name": "토스",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    {
        "id": "ktalpha",
        "name": "KT알파쇼핑",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
    # Integration solution
    {
        "id": "playauto",
        "name": "플레이오토",
        "group": "연동솔루션",
        "api_fields": ["apiKey", "solutionCode", "userId", "password"],
    },
    # Overseas
    {
        "id": "ebay",
        "name": "eBay",
        "group": "해외",
        "api_fields": ["appId", "devId", "certId", "authToken"],
    },
    {
        "id": "lazada",
        "name": "Lazada",
        "group": "해외",
        "api_fields": ["appKey", "appSecret", "accessToken"],
    },
    {
        "id": "shopee",
        "name": "Shopee",
        "group": "해외",
        "api_fields": ["partnerId", "shopId", "accessToken"],
    },
    {
        "id": "qoo10",
        "name": "Qoo10",
        "group": "해외",
        "api_fields": ["apiKey", "userId"],
    },
    {
        "id": "quten",
        "name": "큐텐",
        "group": "해외",
        "api_fields": ["apiKey", "qUserId"],
    },
    # Resale
    {
        "id": "kream",
        "name": "KREAM",
        "group": "리셀",
        "api_fields": ["email", "password"],
    },
    {
        "id": "shopify",
        "name": "Shopify",
        "group": "해외",
        "api_fields": ["accessToken"],
    },
    {
        "id": "zoom",
        "name": "Zum(줌)",
        "group": "국내",
        "api_fields": ["apiKey", "apiSecret"],
    },
]


class SambaAccountService:
    def __init__(self, repo: SambaMarketAccountRepository):
        self.repo = repo

    async def list_accounts(
        self, skip: int = 0, limit: int = 500
    ) -> List[SambaMarketAccount]:
        return await self.repo.list_async(skip=skip, limit=limit, order_by="created_at")

    async def get_account(self, account_id: str) -> Optional[SambaMarketAccount]:
        return await self.repo.get_async(account_id)

    async def create_account(self, data: Dict[str, Any]) -> SambaMarketAccount:
        # Auto-populate market_name and account_label if not provided
        market_info = self.get_market_info(data.get("market_type", ""))
        if "market_name" not in data or not data["market_name"]:
            data["market_name"] = (
                market_info["name"] if market_info else data.get("market_type", "")
            )
        if "account_label" not in data or not data["account_label"]:
            business = data.get("business_name") or data.get("market_name", "")
            seller = data.get("seller_id", "")
            data["account_label"] = f"{business}-{seller}"
        return await self.repo.create_async(**data)

    async def update_account(
        self, account_id: str, data: Dict[str, Any]
    ) -> Optional[SambaMarketAccount]:
        from backend.utils.masking import (
            drop_masked_secret_fields,
            sanitize_top_level_secrets,
        )

        # 클라이언트가 GET 응답의 마스킹값(****XXXX)을 그대로 돌려보낼 때
        # 진짜 secret 값을 마스킹 문자열로 덮어쓰는 사고를 차단.
        # 마스킹값은 키 자체를 incoming에서 제거 → merge 단계에서 기존 DB 값이 살아남음
        data = sanitize_top_level_secrets(data)
        # additional_fields는 기존 값과 merge — 클라가 보내지 않은 키(OAuth 토큰 등) 보존
        # 전체 교체 방식이면 카페24 OAuth 직후 설정 저장 시 accessToken/refreshToken이 증발함
        if "additional_fields" in data and isinstance(data["additional_fields"], dict):
            cleaned_incoming = drop_masked_secret_fields(data["additional_fields"])
            existing = await self.repo.get_async(account_id)
            if existing:
                existing_af = existing.additional_fields or {}
                if isinstance(existing_af, dict):
                    data["additional_fields"] = {
                        **existing_af,
                        **cleaned_incoming,
                    }
                else:
                    data["additional_fields"] = cleaned_incoming
            else:
                data["additional_fields"] = cleaned_incoming
        return await self.repo.update_async(account_id, **data)

    async def delete_account(self, account_id: str) -> bool:
        return await self.repo.delete_async(account_id)

    async def get_active_accounts(self) -> List[SambaMarketAccount]:
        return await self.repo.filter_by_async(
            is_active=True, order_by="created_at", order_by_desc=True
        )

    async def get_accounts_by_market(
        self, market_type: str
    ) -> List[SambaMarketAccount]:
        return await self.repo.filter_by_async(
            market_type=market_type, order_by="created_at", order_by_desc=True
        )

    async def toggle_active(self, account_id: str) -> Optional[SambaMarketAccount]:
        account = await self.repo.get_async(account_id)
        if not account:
            return None
        return await self.repo.update_async(account_id, is_active=not account.is_active)

    @staticmethod
    def get_market_info(market_type: str) -> Optional[Dict[str, Any]]:
        for market in SUPPORTED_MARKETS:
            if market["id"] == market_type:
                return market
        return None

    async def set_default(
        self, account_id: str, tenant_id: Optional[str] = None
    ) -> Optional[SambaMarketAccount]:
        """기본 계정 지정 — 같은 (tenant_id, market_type) 의 다른 계정은 is_default=false 강제.

        라디오 동작 (market_type 당 1개만 true). store_* 단일 키 폴백을 대체하는
        진실의 출처 마킹.
        """
        from datetime import datetime, timezone
        from sqlalchemy import update as sa_update

        account = await self.repo.get_async(account_id)
        if not account:
            return None

        stmt = (
            sa_update(SambaMarketAccount)
            .where(SambaMarketAccount.market_type == account.market_type)
            .where(SambaMarketAccount.id != account_id)
        )
        if tenant_id is not None:
            stmt = stmt.where(SambaMarketAccount.tenant_id == tenant_id)
        else:
            stmt = stmt.where(SambaMarketAccount.tenant_id.is_(None))
        stmt = stmt.values(is_default=False, updated_at=datetime.now(timezone.utc))
        await self.repo.session.execute(stmt)
        return await self.repo.update_async(account_id, is_default=True)

    async def find_default_for(
        self, market_type: str, tenant_id: Optional[str]
    ) -> Optional[SambaMarketAccount]:
        """market_type+tenant 의 fallback 계정 조회 (is_default 우선, 없으면 최근 활성)."""
        return await self.repo.find_default(market_type, tenant_id)
