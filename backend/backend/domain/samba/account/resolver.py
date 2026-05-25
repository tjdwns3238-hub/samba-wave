"""마켓 자격증명 우선순위 해석 — 라우터/디스패처/워커 공통 (2026-05-25).

호출 흐름:
1. form_payload 가 있으면 → 폼 입력값 우선 (신규 등록 인증 테스트)
2. account_id 지정 시 → 그 계정 (tenant 일치 검증)
3. allow_default_fallback=True 인 경우에만 → find_default(market_type, tenant_id) 폴백
4. 그 외 빈 dict — samba_market_account 단일 진실 출처 (legacy store_* 제거됨, 2026-05-25)

전송/등록 hot 경로는 테트리스 매칭(order.channel_id) 또는 정책 연결 계정으로
account 가 이미 결정되어야 함. resolver 의 default 폴백이 그 결정을 덮어
다른 계정 자격증명으로 전송하는 회귀를 차단하기 위해 폴백은 명시 옵트인.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlmodel.ext.asyncio.session import AsyncSession

from backend.domain.samba.account.credentials import CRED_BUILDERS
from backend.domain.samba.account.model import SambaMarketAccount
from backend.domain.samba.account.repository import SambaMarketAccountRepository


async def resolve_market_creds(
    session: AsyncSession,
    tenant_id: Optional[str],
    market_type: str,
    store_key: str = "",
    form_payload: Optional[dict] = None,
    account_id: Optional[str] = None,
    allow_default_fallback: bool = False,
) -> dict[str, Any]:
    """범용 자격증명 우선순위 해석. credentials 빌더 키 명세 따른 dict 반환.

    market_type 은 CRED_BUILDERS 키 (소문자).
    store_key 는 backward-compat 시그니처 — 더 이상 참조되지 않지만 호출자 변경 최소화.
    allow_default_fallback=True 인 경우에만 is_default 계정 폴백 활성화
    (신규 등록 인증 테스트 등 account 미결정 컨텍스트 전용).
    """
    if form_payload:
        cleaned = {k: v for k, v in form_payload.items() if v}
        if cleaned:
            return cleaned

    if session is None:
        return {}

    try:
        repo = SambaMarketAccountRepository(session)
        account: Optional[SambaMarketAccount] = None
        if account_id:
            account = await repo.get_async(account_id)
            if account and tenant_id is not None and account.tenant_id != tenant_id:
                account = None
        if account is None and allow_default_fallback:
            account = await repo.find_default(market_type, tenant_id)
        if account is not None:
            builder = CRED_BUILDERS.get(market_type.lower())
            if builder is not None:
                return builder(account)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            f"[resolver] DB 조회 실패 ({market_type}): {exc}"
        )

    return {}
