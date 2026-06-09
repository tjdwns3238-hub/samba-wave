"""CS 자동화 내부 API (Tier 0~2).

Claude 클라우드 스케줄잡(30분 주기)이 호출하는 전용 엔드포인트.
samba_auth(JWT)를 우회하므로 X-Internal-Token 헤더로만 인증한다.
app_factory에서 samba_auth 없이 등록되며, 토큰 검증이 유일 방어선.

엔드포인트:
  GET  /internal/cs/pending-with-context  미답변 문의 + 의도분류 + 컨텍스트
  POST /internal/cs/draft                 답변 초안 저장 (Tier 1)
  POST /internal/cs/auto-send             저위험 의도 자동전송 (Tier 2, 게이트)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as sa_text
from sqlmodel.ext.asyncio.session import AsyncSession

from backend.core.config import settings
from backend.db.orm import get_read_session_dependency, get_write_session_dependency
from backend.domain.samba.cs_inquiry.classifier import AUTO_SEND_INTENTS, classify
from backend.domain.samba.cs_inquiry.context import gather_context
from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository
from backend.utils.logger import logger

router = APIRouter(prefix="/internal/cs", tags=["samba-cs-internal"])


async def _require_internal_token(
    x_internal_token: Optional[str] = Header(default=None),
) -> None:
    """X-Internal-Token 검증. 토큰 미설정(빈 값)이면 전체 차단."""
    expected = settings.cs_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="CS 내부 API 비활성(토큰 미설정)")
    if x_internal_token != expected:
        raise HTTPException(status_code=403, detail="유효하지 않은 내부 토큰")


# ==================== 응답/요청 모델 ====================


class PendingItem(BaseModel):
    inquiry_id: str
    intent: str
    auto_send_eligible: bool
    confidence: float
    context: Dict[str, Any]


class DraftSave(BaseModel):
    inquiry_id: str
    intent: str
    draft_reply: str
    confidence: float = 0.0
    source: str = "claude"  # claude/template/rule


class AutoSend(BaseModel):
    inquiry_id: str
    draft_reply: str
    confidence: float = 0.0
    dry_run: bool = True  # 기본 dry-run — 실제 전송은 명시적으로 false


# 자동전송 게이트 상수
_AUTO_SEND_MIN_CONFIDENCE = 0.8
_AUTO_SEND_DAILY_LIMIT = 50
_KILL_SWITCH_KEY = "cs_auto_send_enabled"  # samba_settings, 기본 OFF


# ==================== 엔드포인트 ====================


@router.get(
    "/pending-with-context",
    dependencies=[Depends(_require_internal_token)],
)
async def pending_with_context(
    limit: int = 30,
    session: AsyncSession = Depends(get_read_session_dependency),
) -> Dict[str, Any]:
    """미답변(pending)이고 아직 초안 없는 문의를 의도분류 + 컨텍스트와 함께 반환.

    draft_status='none' AND reply_status='pending' 인 건만 — 중복 작성 방지.
    """
    limit = max(1, min(limit, 100))
    rows = (
        await session.execute(
            sa_text(
                "SELECT id FROM samba_cs_inquiry "
                "WHERE is_hidden = false AND reply_status = 'pending' "
                "AND COALESCE(draft_status, 'none') = 'none' "
                "ORDER BY inquiry_date DESC NULLS LAST LIMIT :lim"
            ),
            {"lim": limit},
        )
    ).all()

    repo = SambaCSInquiryRepository(session)
    items: List[PendingItem] = []
    for (iid,) in rows:
        inq = await repo.get_async(iid)
        if not inq:
            continue
        cls = classify(inq.content, inq.inquiry_type, inq.market, inq.questioner)
        ctx = await gather_context(session, inq, cls.intent)
        items.append(
            PendingItem(
                inquiry_id=inq.id,
                intent=cls.intent,
                auto_send_eligible=cls.auto_send_eligible,
                confidence=cls.confidence,
                context=ctx,
            )
        )
    return {"items": [i.model_dump() for i in items], "total": len(items)}


@router.post("/draft", dependencies=[Depends(_require_internal_token)])
async def save_draft(
    body: DraftSave,
    session: AsyncSession = Depends(get_write_session_dependency),
) -> Dict[str, Any]:
    """답변 초안 저장 (Tier 1) — reply_status는 pending 유지, 고객 미전송."""
    repo = SambaCSInquiryRepository(session)
    inq = await repo.get_async(body.inquiry_id)
    if not inq:
        raise HTTPException(status_code=404, detail="문의 없음")
    if inq.reply_status == "replied":
        return {"ok": False, "reason": "이미 답변 완료"}

    await repo.update_async(
        body.inquiry_id,
        intent=body.intent,
        draft_reply=body.draft_reply,
        draft_status="suggested",
        draft_confidence=body.confidence,
        draft_source=body.source,
        drafted_at=datetime.now(UTC),
    )
    logger.info(
        f"[CS자동화] 초안 저장 {body.inquiry_id} intent={body.intent} "
        f"conf={body.confidence}"
    )
    return {"ok": True, "draft_status": "suggested"}


@router.post("/auto-send", dependencies=[Depends(_require_internal_token)])
async def auto_send(
    body: AutoSend,
    session: AsyncSession = Depends(get_write_session_dependency),
) -> Dict[str, Any]:
    """저위험 의도 자동전송 (Tier 2) — 다중 게이트.

    게이트(하나라도 불충족 시 draft만 저장하고 미전송):
      1) 킬스위치 ON (samba_settings cs_auto_send_enabled, 기본 OFF)
      2) 서버 재분류 결과 auto_send_eligible (클라 intent 불신)
      3) confidence >= 0.8
      4) 일일 자동전송 한도 미초과
      5) dry_run=false (기본 true — 실수로 전송 방지)
    실제 전송은 기존 reply 파이프라인(reply_cs_inquiry) 재사용 — 마켓별 로직 중복 없음.
    """
    from backend.api.v1.routers.samba.cs_inquiry import reply_cs_inquiry
    from backend.api.v1.routers.samba.proxy._helpers import _get_setting
    from backend.dtos.samba.cs_inquiry import CSInquiryReply

    repo = SambaCSInquiryRepository(session)
    inq = await repo.get_async(body.inquiry_id)
    if not inq:
        raise HTTPException(status_code=404, detail="문의 없음")
    if inq.reply_status == "replied":
        return {"ok": False, "sent": False, "reason": "이미 답변 완료"}

    # 게이트 2: 서버 재분류 (클라이언트가 보낸 intent 신뢰 안 함)
    cls = classify(inq.content, inq.inquiry_type, inq.market, inq.questioner)
    if cls.intent not in AUTO_SEND_INTENTS or not cls.auto_send_eligible:
        return await _fallback_draft(
            repo, body, cls.intent, "자동전송 비대상 의도 — 초안만 저장"
        )

    # 게이트 3: 신뢰도
    eff_conf = max(body.confidence, cls.confidence)
    if eff_conf < _AUTO_SEND_MIN_CONFIDENCE:
        return await _fallback_draft(
            repo, body, cls.intent, f"신뢰도 부족({eff_conf:.2f}) — 초안만 저장"
        )

    # 게이트 1: 킬스위치
    enabled = await _get_setting(session, _KILL_SWITCH_KEY)
    if str(enabled).lower() not in ("true", "1", "on", "yes"):
        return await _fallback_draft(
            repo, body, cls.intent, "킬스위치 OFF — 초안만 저장(미전송)"
        )

    # 게이트 4: 일일 한도
    sent_today = (
        await session.execute(
            sa_text(
                "SELECT count(*) FROM samba_cs_inquiry "
                "WHERE draft_status = 'auto_sent' "
                "AND drafted_at >= date_trunc('day', now())"
            )
        )
    ).scalar() or 0
    if sent_today >= _AUTO_SEND_DAILY_LIMIT:
        return await _fallback_draft(
            repo, body, cls.intent, f"일일 한도({_AUTO_SEND_DAILY_LIMIT}) 초과 — 초안만"
        )

    # 게이트 5: dry-run
    if body.dry_run:
        await repo.update_async(
            body.inquiry_id,
            intent=cls.intent,
            draft_reply=body.draft_reply,
            draft_status="suggested",
            draft_confidence=eff_conf,
            draft_source="claude",
            drafted_at=datetime.now(UTC),
        )
        logger.info(f"[CS자동화] dry-run 자동전송 {body.inquiry_id} — 미전송")
        return {"ok": True, "sent": False, "dry_run": True, "intent": cls.intent}

    # 실제 전송 — 기존 reply 파이프라인 재사용
    try:
        result = await reply_cs_inquiry(
            body.inquiry_id, CSInquiryReply(reply=body.draft_reply), session
        )
    except Exception as e:
        logger.error(f"[CS자동화] 자동전송 실패 {body.inquiry_id}: {e}")
        await _fallback_draft(repo, body, cls.intent, f"전송 오류: {e}")
        return {"ok": False, "sent": False, "reason": str(e)[:200]}

    await repo.update_async(
        body.inquiry_id,
        intent=cls.intent,
        draft_reply=body.draft_reply,
        draft_status="auto_sent",
        draft_confidence=eff_conf,
        draft_source="claude",
        drafted_at=datetime.now(UTC),
    )
    logger.info(
        f"[CS자동화] 자동전송 완료 {body.inquiry_id} intent={cls.intent} "
        f"market_sent={result.get('market_sent')}"
    )
    return {
        "ok": True,
        "sent": True,
        "intent": cls.intent,
        "market_sent": result.get("market_sent"),
    }


@router.post("/send-all-drafts", dependencies=[Depends(_require_internal_token)])
async def send_all_drafts(
    session: AsyncSession = Depends(get_write_session_dependency),
) -> Dict[str, Any]:
    """draft_status='suggested' 인 모든 대기 초안을 즉시 전송.

    킬스위치·confidence 게이트 없음 — 운영자가 명시 호출하는 일괄전송 전용.
    이미 answered 건, is_hidden 건은 자동 제외.
    """
    from backend.api.v1.routers.samba.cs_inquiry import reply_cs_inquiry
    from backend.dtos.samba.cs_inquiry import CSInquiryReply

    rows = (
        await session.execute(
            sa_text(
                "SELECT id, draft_reply FROM samba_cs_inquiry "
                "WHERE draft_status = 'suggested' AND reply_status = 'pending' "
                "AND is_hidden = false ORDER BY drafted_at ASC"
            )
        )
    ).all()

    ok_ids: List[str] = []
    fail_ids: List[str] = []

    for iid, draft_reply in rows:
        if not draft_reply:
            fail_ids.append(iid)
            continue
        try:
            result = await reply_cs_inquiry(
                iid, CSInquiryReply(reply=draft_reply), session
            )
            ok_ids.append(iid)
            logger.info(
                f"[CS일괄전송] OK {iid} market_sent={result.get('market_sent') if isinstance(result, dict) else '?'}"
            )
        except Exception as e:
            fail_ids.append(iid)
            logger.error(f"[CS일괄전송] FAIL {iid}: {e}")

    return {
        "ok": len(ok_ids),
        "fail": len(fail_ids),
        "ok_ids": ok_ids,
        "fail_ids": fail_ids,
    }


async def _fallback_draft(
    repo: SambaCSInquiryRepository,
    body: "AutoSend",
    intent: str,
    reason: str,
) -> Dict[str, Any]:
    """자동전송 게이트 불충족 시 초안만 저장(suggested)하고 사유 반환."""
    await repo.update_async(
        body.inquiry_id,
        intent=intent,
        draft_reply=body.draft_reply,
        draft_status="suggested",
        draft_confidence=body.confidence,
        draft_source="claude",
        drafted_at=datetime.now(UTC),
    )
    logger.info(f"[CS자동화] 자동전송 보류 {body.inquiry_id}: {reason}")
    return {"ok": True, "sent": False, "reason": reason, "draft_status": "suggested"}
