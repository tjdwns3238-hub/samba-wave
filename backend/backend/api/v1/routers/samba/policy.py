"""SambaWave Policy API router."""

import logging
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.policy.model import (
    SambaDetailTemplate,
    SambaNameRule,
    SambaPolicy,
)
from backend.domain.samba.policy.repository import SambaPolicyRepository
from backend.domain.samba.policy.service import (
    PolicyNameDuplicateError,
    SambaPolicyService,
)
from backend.domain.samba.tenant.middleware import get_optional_tenant_id
from backend.dtos.samba.policy import PolicyCreate, PolicyUpdate, PriceCalculateRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/policies", tags=["samba-policies"])


def _get_service(session: AsyncSession) -> SambaPolicyService:
    return SambaPolicyService(SambaPolicyRepository(session))


@router.get("", response_model=list[SambaPolicy])
async def list_policies(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    # tenant_id가 있으면 해당 테넌트 + 기존(NULL) 정책 모두 조회
    if tenant_id:
        from sqlalchemy import or_

        stmt = (
            select(SambaPolicy)
            .where(
                or_(
                    SambaPolicy.tenant_id == tenant_id,
                    SambaPolicy.tenant_id == None,  # noqa: E711
                )
            )
            .offset(skip)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()
    svc = _get_service(session)
    return await svc.list_policies(skip=skip, limit=limit)


# ── Detail Templates ──────────────────────────────────────────────────────────
# 정적 경로를 /{policy_id} 파라미터 경로보다 먼저 등록해야 경로 충돌 방지


@router.get("/detail-templates", response_model=list[SambaDetailTemplate])
async def list_detail_templates(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """상세페이지 템플릿 목록 조회."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaDetailTemplate)
    return await repo.list_async(skip=skip, limit=limit, order_by="-created_at")


@router.get("/detail-templates/{template_id}", response_model=SambaDetailTemplate)
async def get_detail_template(
    template_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """상세페이지 템플릿 단건 조회."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaDetailTemplate)
    tpl = await repo.get_async(template_id)
    if not tpl:
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    return tpl


@router.post("/detail-templates", response_model=SambaDetailTemplate, status_code=201)
async def create_detail_template(
    body: dict = Body(...),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상세페이지 템플릿 생성."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaDetailTemplate)
    return await repo.create_async(**body)


@router.put("/detail-templates/{template_id}", response_model=SambaDetailTemplate)
async def update_detail_template(
    template_id: str,
    body: dict = Body(...),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상세페이지 템플릿 수정."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaDetailTemplate)
    tpl = await repo.update_async(template_id, **body)
    if not tpl:
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    return tpl


@router.delete("/detail-templates/{template_id}")
async def delete_detail_template(
    template_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상세페이지 템플릿 삭제."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaDetailTemplate)
    deleted = await repo.delete_async(template_id)
    if not deleted:
        raise HTTPException(404, "템플릿을 찾을 수 없습니다")
    return {"ok": True}


# ── Name Rules ────────────────────────────────────────────────────────────────


@router.get("/name-rules", response_model=list[SambaNameRule])
async def list_name_rules(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """상품/옵션명 규칙 목록 조회."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaNameRule)
    return await repo.list_async(skip=skip, limit=limit, order_by="-created_at")


@router.get("/name-rules/{rule_id}", response_model=SambaNameRule)
async def get_name_rule(
    rule_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    """상품/옵션명 규칙 단건 조회."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaNameRule)
    rule = await repo.get_async(rule_id)
    if not rule:
        raise HTTPException(404, "규칙을 찾을 수 없습니다")
    return rule


@router.post("/name-rules", response_model=SambaNameRule, status_code=201)
async def create_name_rule(
    body: dict = Body(...),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상품/옵션명 규칙 생성."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaNameRule)
    return await repo.create_async(**body)


@router.put("/name-rules/{rule_id}", response_model=SambaNameRule)
async def update_name_rule(
    rule_id: str,
    body: dict = Body(...),
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상품/옵션명 규칙 수정."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaNameRule)
    rule = await repo.update_async(rule_id, **body)
    if not rule:
        raise HTTPException(404, "규칙을 찾을 수 없습니다")
    return rule


@router.delete("/name-rules/{rule_id}")
async def delete_name_rule(
    rule_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """상품/옵션명 규칙 삭제."""
    from backend.domain.shared.base_repository import BaseRepository

    repo = BaseRepository(session, SambaNameRule)
    deleted = await repo.delete_async(rule_id)
    if not deleted:
        raise HTTPException(404, "규칙을 찾을 수 없습니다")
    return {"ok": True}


# ── AI 정책 변경 (정적 경로 — /{policy_id} 보다 앞에 등록) ─────────────────────


class AiPolicyCommandRequest(BaseModel):
    """AI 정책 일괄 변경 요청 — 자연어 명령."""

    command: str


@router.post("/ai-change")
async def ai_change_policy(
    body: AiPolicyCommandRequest,
    session: AsyncSession = Depends(get_write_session_dependency),
):
    """자연어 명령으로 관련 마켓의 모든 정책을 일괄 변경."""
    from backend.domain.samba.ai.gemma_client import _get_gemma_api_key

    try:
        api_key = await _get_gemma_api_key(session)
    except ValueError:
        raise HTTPException(400, "Gemini/Gemma API Key가 설정되지 않았습니다")

    import json

    svc = _get_service(session)
    all_policies = await svc.list_policies(skip=0, limit=200)

    policies_summary = []
    for p in all_policies:
        mp = p.market_policies or {}
        pr = p.pricing or {}
        policies_summary.append(
            {
                "id": p.id,
                "name": p.name,
                "pricing": {
                    "marginRate": pr.get("marginRate", 15),
                    "shippingCost": pr.get("shippingCost", 0),
                    "extraCharge": pr.get("extraCharge", 0),
                    "minMarginAmount": pr.get("minMarginAmount", 0),
                },
                "market_policies": {
                    mk: {
                        "marginRate": mv.get("marginRate", 0)
                        if isinstance(mv, dict)
                        else 0,
                        "feeRate": mv.get("feeRate", 0) if isinstance(mv, dict) else 0,
                        "shippingCost": mv.get("shippingCost", 0)
                        if isinstance(mv, dict)
                        else 0,
                    }
                    for mk, mv in (mp.items() if isinstance(mp, dict) else [])
                },
            }
        )

    prompt = f"""위탁판매 솔루션의 가격정책 관리자입니다.
사용자의 명령을 분석하여 해당 마켓이 설정된 모든 정책을 일괄 변경해주세요.

[사용자 명령]
"{body.command}"

[현재 전체 정책 목록]
{json.dumps(policies_summary, ensure_ascii=False, indent=2)}

[마켓 키 매핑]
smartstore=스마트스토어, coupang=쿠팡, gmarket=G마켓/지마켓, auction=옥션,
11st=11번가, ssg=SSG/신세계, lotteon=롯데ON, lottehome=롯데홈쇼핑,
gsshop=GS샵, homeand=홈앤쇼핑, hmall=HMALL/현대, kream=KREAM/크림

[정책 구조]
- pricing: 공통 가격정책 (marginRate, shippingCost, extraCharge, minMarginAmount)
- market_policies.{{마켓키}}: 마켓별 개별정책 (marginRate, feeRate, shippingCost)
- market_policies의 marginRate가 0이면 공통 pricing의 marginRate를 사용

규칙:
1. 명령에서 언급된 마켓이 market_policies에 있는 정책만 변경 대상
2. "마진율 1% 올려" = 해당 마켓의 marginRate에 +1
3. "배송비 500원 내려" = 해당 마켓의 shippingCost에 -500
4. "수수료 2% 낮춰" = 해당 마켓의 feeRate에 -2
5. 마켓 미지정 시 공통 pricing을 변경
6. 변경할 정책이 없으면 빈 배열 반환

JSON만 응답:
{{
  "changes": [
    {{
      "policy_id": "정책ID",
      "policy_name": "정책명",
      "field": "변경 필드명",
      "market": "마켓키 또는 common",
      "before": 이전값,
      "after": 변경값
    }}
  ]
}}"""

    from backend.domain.samba.ai.gemma_client import generate_text, extract_json

    try:
        raw_text = await generate_text(api_key, prompt, max_tokens=2048)
        result = extract_json(raw_text)
        changes = result.get("changes", [])

        applied = 0
        for ch in changes:
            pid = ch.get("policy_id")
            policy = await svc.get_policy(pid)
            if not policy:
                continue

            market = ch.get("market", "common")
            field = ch.get("field", "")
            after = ch.get("after")

            if market == "common":
                updated_pricing = dict(policy.pricing or {})
                updated_pricing[field] = after
                await svc.update_policy(pid, {"pricing": updated_pricing})
            else:
                updated_mp = dict(policy.market_policies or {})
                if market in updated_mp and isinstance(updated_mp[market], dict):
                    updated_mp[market] = dict(updated_mp[market])
                    updated_mp[market][field] = after
                    await svc.update_policy(pid, {"market_policies": updated_mp})
            applied += 1

        return {"ok": True, "applied": applied, "changes": changes}
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"AI 응답 파싱 실패: {e}") from e
    except RuntimeError as e:
        raise HTTPException(400, f"Gemma API 오류: {e}") from e


# ── Policy CRUD (파라미터 경로는 정적 경로 뒤에 등록) ─────────────────────────


@router.get("/{policy_id}", response_model=SambaPolicy)
async def get_policy(
    policy_id: str,
    session: AsyncSession = Depends(get_read_session_dependency),
):
    svc = _get_service(session)
    policy = await svc.get_policy(policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="정책을 찾을 수 없습니다")
    return policy


@router.post("", response_model=SambaPolicy, status_code=201)
async def create_policy(
    body: PolicyCreate,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _get_service(session)
    data = body.model_dump(exclude_unset=True)
    # 테넌트 ID가 있으면 새 정책에 설정
    if tenant_id:
        data["tenant_id"] = tenant_id
    try:
        return await svc.create_policy(data)
    except PolicyNameDuplicateError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{policy_id}", response_model=SambaPolicy)
async def update_policy(
    policy_id: str,
    body: PolicyUpdate,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _get_service(session)
    # 테넌트 소유권 검증: tenant_id가 있으면 해당 테넌트 정책만 수정 가능
    if tenant_id:
        existing = await svc.get_policy(policy_id)
        if not existing:
            raise HTTPException(status_code=404, detail="정책을 찾을 수 없습니다")
        if existing.tenant_id != tenant_id:
            raise HTTPException(
                status_code=403, detail="해당 정책에 접근 권한이 없습니다"
            )
    try:
        policy = await svc.update_policy(
            policy_id, body.model_dump(exclude_unset=True)
        )
    except PolicyNameDuplicateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not policy:
        raise HTTPException(status_code=404, detail="정책을 찾을 수 없습니다")
    return policy


@router.delete("/{policy_id}")
async def delete_policy(
    policy_id: str,
    session: AsyncSession = Depends(get_write_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _get_service(session)
    # 테넌트 소유권 검증: tenant_id가 있으면 해당 테넌트 정책만 삭제 가능
    if tenant_id:
        existing = await svc.get_policy(policy_id)
        if not existing:
            raise HTTPException(status_code=404, detail="정책을 찾을 수 없습니다")
        if existing.tenant_id != tenant_id:
            raise HTTPException(
                status_code=403, detail="해당 정책에 접근 권한이 없습니다"
            )
    deleted = await svc.delete_policy(policy_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="정책을 찾을 수 없습니다")
    return {"ok": True}


@router.post("/{policy_id}/calculate-price")
async def calculate_price(
    policy_id: str,
    body: PriceCalculateRequest,
    session: AsyncSession = Depends(get_read_session_dependency),
    tenant_id: Optional[str] = Depends(get_optional_tenant_id),
):
    svc = _get_service(session)
    return await svc.get_price_preview(
        policy_id, body.cost, body.fee_rate, body.source_site, tenant_id
    )
