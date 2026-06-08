"""SambaWave Return service."""

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from backend.domain.samba.returns.model import SambaReturn
from backend.domain.samba.returns.repository import SambaReturnRepository
from backend.utils.logger import logger

# 반품 사유 (js/modules/returns.js 포팅)
RETURN_REASONS: Dict[str, List[str]] = {
    "return": [
        "단순 변심",
        "사이즈 불일치",
        "색상 차이",
        "상품 불량",
        "오배송",
        "상품 파손",
        "기타",
    ],
    "exchange": [
        "사이즈 교환",
        "색상 교환",
        "상품 불량 교환",
        "오배송 교환",
        "기타",
    ],
    "cancel": [
        "단순 변심",
        "배송 지연",
        "가격 변동",
        "중복 주문",
        "품절",
        "기타",
    ],
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _make_timeline_entry(status: str, message: str) -> Dict[str, str]:
    return {
        "date": _now_iso(),
        "status": status,
        "message": message,
    }


class SambaReturnService:
    def __init__(self, repo: SambaReturnRepository):
        self.repo = repo

    # ==================== CRUD ====================

    async def list_returns(
        self,
        skip: int = 0,
        limit: int = 50,
        order_id: Optional[str] = None,
        status: Optional[str] = None,
        type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[SambaReturn]:
        # 날짜 필터 또는 tenant_id가 있으면 list_filtered 사용
        if (start_date and end_date) or tenant_id:
            from backend.utils import kst_date_range_to_utc

            start_dt, end_dt = None, None
            if start_date and end_date:
                start_dt, end_dt = kst_date_range_to_utc(start_date, end_date)
            return await self.repo.list_filtered(
                skip=skip,
                limit=limit,
                order_id=order_id,
                status=status,
                type=type,
                start_dt=start_dt,
                end_dt=end_dt,
                tenant_id=tenant_id,
            )
        if order_id:
            return await self.repo.list_by_order(order_id)
        if status:
            return await self.repo.list_by_status(status)
        if type:
            return await self.repo.list_by_type(type)
        return await self.repo.list_async(
            skip=skip, limit=limit, order_by="-created_at"
        )

    async def get_return(self, return_id: str) -> Optional[SambaReturn]:
        return await self.repo.get_async(return_id)

    # ==================== Business Logic ====================

    async def create_return(self, data: Dict[str, Any]) -> SambaReturn:
        """반품/교환/취소 생성 + 초기 타임라인 엔트리."""
        return_type = data.get("type", "return")
        initial_timeline = [
            _make_timeline_entry("requested", f"{return_type} 요청이 접수되었습니다.")
        ]
        data["timeline"] = initial_timeline
        data["notes"] = data.get("notes") or []
        data["status"] = "requested"

        ret = await self.repo.create_async(**data)
        logger.info(
            f"Return {ret.id} created for order {ret.order_id} type={return_type}"
        )
        return ret

    async def approve_return(self, return_id: str) -> Optional[SambaReturn]:
        """반품 승인."""
        ret = await self.repo.get_async(return_id)
        if not ret:
            return None

        now = datetime.now(UTC)
        timeline = list(ret.timeline or [])
        timeline.append(_make_timeline_entry("approved", "요청이 승인되었습니다."))

        return await self.repo.update_async(
            return_id,
            status="approved",
            approval_date=now,
            timeline=timeline,
        )

    async def reject_return(
        self, return_id: str, reason: Optional[str] = None
    ) -> Optional[SambaReturn]:
        """반품 거절."""
        ret = await self.repo.get_async(return_id)
        if not ret:
            return None

        message = (
            f"요청이 거절되었습니다. 사유: {reason}"
            if reason
            else "요청이 거절되었습니다."
        )
        timeline = list(ret.timeline or [])
        timeline.append(_make_timeline_entry("rejected", message))

        return await self.repo.update_async(
            return_id,
            status="rejected",
            timeline=timeline,
        )

    async def complete_return(self, return_id: str) -> Optional[SambaReturn]:
        """반품 완료 처리 + 연결된 주문 상태 동기화."""
        ret = await self.repo.get_async(return_id)
        if not ret:
            return None

        now = datetime.now(UTC)
        timeline = list(ret.timeline or [])
        timeline.append(_make_timeline_entry("completed", "처리가 완료되었습니다."))

        updated = await self.repo.update_async(
            return_id,
            status="completed",
            completion_date=now,
            timeline=timeline,
        )

        # 연결된 주문의 status도 '반품완료(returned)'로 동기화
        await self._sync_order_status_returned(ret.order_id)

        return updated

    async def _sync_order_status_returned(self, order_id: Optional[str]) -> None:
        """반품 완료 시 연결된 주문 status를 'returned'로 갱신."""
        if not order_id:
            return

        from backend.db.orm import get_write_session
        from backend.domain.samba.order.model import SambaOrder

        try:
            async with get_write_session() as session:
                order = await session.get(SambaOrder, order_id)
                if not order:
                    logger.warning(f"[반품완료동기화] 주문 없음 order_id={order_id}")
                    return
                if order.status == "returned":
                    return
                order.status = "returned"
                order.return_status = "returned"
                session.add(order)
                await session.commit()
                logger.info(f"[반품완료동기화] 주문 {order_id} status=returned 반영 완료")
        except Exception as exc:
            logger.warning(f"[반품완료동기화] 실패 order_id={order_id}: {exc}")

    async def cancel_return(self, return_id: str) -> Optional[SambaReturn]:
        """반품 요청 취소."""
        ret = await self.repo.get_async(return_id)
        if not ret:
            return None

        timeline = list(ret.timeline or [])
        timeline.append(_make_timeline_entry("cancelled", "요청이 취소되었습니다."))

        return await self.repo.update_async(
            return_id,
            status="cancelled",
            timeline=timeline,
        )

    async def complete_lottehome_return(
        self,
        return_id: str,
        lh_client: Any,
    ) -> Dict[str, Any]:
        """롯데홈쇼핑 반품 회수확정 (registDeliver.lotte, proc_gubun=rfin).

        흐름:
          1) SambaReturn → 연결 SambaOrder 조회 (source==lottehome 검증)
          2) ext_order_number → ord_no:ord_dtl_sn 파싱
          3) lh_client.process_return 호출
          4) 성공 시 self.complete_return 호출하여 timeline + status=completed 갱신
        """
        from backend.db.orm import get_write_session
        from backend.domain.samba.order.model import SambaOrder
        from backend.domain.samba.proxy.lottehome import lottehome_courier_code

        ret = await self.repo.get_async(return_id)
        if not ret:
            return {"ok": False, "error": "반품 레코드 없음"}

        async with get_write_session() as session:
            order = await session.get(SambaOrder, ret.order_id)

        if not order:
            return {"ok": False, "error": "연결 주문 없음"}
        if (order.source or "").lower() != "lottehome":
            return {
                "ok": False,
                "error": f"롯데홈쇼핑 주문 아님 (source={order.source})",
            }

        raw = (order.ext_order_number or "").strip() or (
            order.shipment_id or ""
        ).strip()
        sep = ":" if ":" in raw else ("/" if "/" in raw else None)
        if not sep:
            return {
                "ok": False,
                "error": (
                    f"롯데홈쇼핑은 'ord_no:ord_dtl_sn' 형식이 필요합니다 (현재값={raw})"
                ),
            }
        parts = [p.strip() for p in raw.split(sep, 1)]
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return {"ok": False, "error": f"주문번호 파싱 실패: {raw}"}
        ord_no, ord_dtl_sn = parts[0], parts[1]

        courier_code = lottehome_courier_code(order.shipping_company or "")
        try:
            api_resp = await lh_client.process_return(
                ord_no=ord_no,
                ord_dtl_sn=ord_dtl_sn,
                courier_code=courier_code,
                tracking_number=order.tracking_number or "",
            )
        except Exception as exc:
            logger.warning(f"[롯데홈 반품] API 호출 실패 ({return_id}): {exc}")
            return {"ok": False, "error": f"API 호출 실패: {exc}"}

        if not api_resp.get("ok"):
            return {
                "ok": False,
                "error": f"롯데홈쇼핑 응답 result={api_resp.get('result')}",
                "raw": api_resp,
            }

        await self.complete_return(return_id)
        logger.info(f"[롯데홈 반품] 회수확정 완료: {return_id}")
        return {"ok": True, "raw": api_resp}

    async def add_note(self, return_id: str, note: str) -> Optional[SambaReturn]:
        """메모 추가."""
        ret = await self.repo.get_async(return_id)
        if not ret:
            return None

        notes = list(ret.notes or [])
        notes.append(
            {
                "date": _now_iso(),
                "message": note,
            }
        )

        return await self.repo.update_async(return_id, notes=notes)

    # ==================== Stats ====================

    async def get_return_stats(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """상태별/유형별 반품 통계 + 총 환불 금액."""
        if tenant_id:
            all_returns = await self.repo.list_filtered(
                tenant_id=tenant_id, limit=10000
            )
        else:
            all_returns = await self.repo.list_async()

        status_counts: Dict[str, int] = {}
        type_counts: Dict[str, int] = {}
        reason_counts: Dict[str, int] = {}
        total_refund: float = 0.0

        for ret in all_returns:
            status_counts[ret.status] = status_counts.get(ret.status, 0) + 1
            type_counts[ret.type] = type_counts.get(ret.type, 0) + 1
            reason_counts[ret.reason or "미분류"] = (
                reason_counts.get(ret.reason or "미분류", 0) + 1
            )
            if ret.requested_amount and ret.status in ("approved", "completed"):
                total_refund += ret.requested_amount

        return {
            "total": len(all_returns),
            "by_status": status_counts,
            "by_type": type_counts,
            "by_reason": reason_counts,
            "total_refund_amount": total_refund,
        }

    # ==================== Auto Approve ====================

    async def auto_approve_returns(self, within_days: int = 7) -> int:
        """요청 상태인 반품 중 N일 이내 요청건 자동승인. 반환: 승인 건수."""
        from datetime import datetime, timezone, timedelta

        all_returns = await self.repo.list_async()
        cutoff = datetime.now(timezone.utc) - timedelta(days=within_days)
        approved_count = 0

        for ret in all_returns:
            if ret.status != "requested":
                continue
            if ret.created_at < cutoff:
                continue

            # 자동승인 처리
            timeline = list(ret.timeline or [])
            timeline.append(
                _make_timeline_entry(
                    "approved",
                    f"자동승인 (요청 후 {within_days}일 이내)",
                )
            )
            await self.repo.update_async(ret.id, status="approved", timeline=timeline)
            approved_count += 1

        return approved_count

    # ==================== Reasons ====================

    @staticmethod
    def get_return_reasons() -> Dict[str, List[str]]:
        return RETURN_REASONS
