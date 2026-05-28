"""SambaWave CS 문의 service."""

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.domain.samba.cs_inquiry.model import SambaCSInquiry
from backend.domain.samba.cs_inquiry.repository import SambaCSInquiryRepository
from backend.utils.logger import logger


def _parse_date(raw: Any) -> Optional[datetime]:
    """마켓 API KST 날짜 문자열을 UTC datetime으로 파싱."""
    if not raw:
        return None
    from backend.utils import kst_str_to_utc

    return kst_str_to_utc(str(raw))


# CS 답변 기본 템플릿 (contact/service.py SMS 템플릿과 동일 패턴)
CS_REPLY_TEMPLATES: Dict[str, Dict[str, str]] = {
    "shipping_info": {
        "name": "배송안내",
        "content": "안녕하세요 고객님 발주 이후 통상 2~4일 정도 소요됩니다 가급적 빠르게 출고 될 수 있도록 하겠습니다. 조금만 시간 양해 부탁드립니다",
    },
    "out_of_stock": {
        "name": "품절안내",
        "content": "안녕하세요 고객님, 해당 상품은 현재 품절 상태입니다. 불편을 드려 죄송합니다. 빠른 시일 내 재입고 될 수 있도록 하겠습니다.",
    },
    "cancel_done": {
        "name": "취소완료",
        "content": "안녕하세요 고객님 해당주문건 취소완료되었습니다.",
    },
    "size_inquiry": {
        "name": "사이즈문의",
        "content": "안녕하세요 고객님, 혹시 어떤색상의 사이즈 문의 주시는 건지 알려주시면 답변 드리도록하겠습니다.",
    },
    "exchange_return": {
        "name": "교환/반품안내",
        "content": "안녕하세요 고객님, 교환/반품 접수되었습니다. 상품검수가 완료되면 반품승인 부탁드립니다. 처리 완료 후 안내드리겠습니다.",
    },
    "delivery_delay": {
        "name": "배송지연안내",
        "content": "안녕하세요 고객님, 현재 물량이 많아 배송이 다소 지연되고 있습니다. 빠르게 처리될 수 있도록 하겠습니다. 양해 부탁드립니다.",
    },
    "product_info": {
        "name": "상품정보안내",
        "content": "안녕하세요 고객님, 문의주신 상품 관련 확인 후 답변 드리겠습니다. 잠시만 기다려주세요.",
    },
    "thank_you": {
        "name": "감사인사",
        "content": "안녕하세요 고객님, 구매해주셔서 감사합니다. 상품에 문제가 있으시면 언제든 문의 부탁드립니다.",
    },
}


class SambaCSInquiryService:
    def __init__(self, repo: SambaCSInquiryRepository):
        self.repo = repo

    # ==================== 목록/조회 ====================

    async def list_inquiries(
        self,
        skip: int = 0,
        limit: int = 30,
        market: Optional[str] = None,
        inquiry_type: Optional[str] = None,
        reply_status: Optional[str] = None,
        search: Optional[str] = None,
        sort_field: str = "inquiry_date",
        sort_desc: bool = True,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """필터링된 문의 목록 + 총 건수 반환."""
        # KST 날짜 문자열 → UTC datetime 변환
        start_dt = None
        end_dt = None
        if start_date and end_date:
            from backend.utils import kst_date_range_to_utc

            start_dt, end_dt = kst_date_range_to_utc(start_date, end_date)
        items = await self.repo.list_filtered(
            skip=skip,
            limit=limit,
            market=market,
            inquiry_type=inquiry_type,
            reply_status=reply_status,
            search=search,
            sort_field=sort_field,
            sort_desc=sort_desc,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        total = await self.repo.count_filtered(
            market=market,
            inquiry_type=inquiry_type,
            reply_status=reply_status,
            search=search,
            start_dt=start_dt,
            end_dt=end_dt,
        )
        return {"items": items, "total": total}

    async def get_inquiry(self, inquiry_id: str) -> Optional[SambaCSInquiry]:
        return await self.repo.get_async(inquiry_id)

    # ==================== 생성 ====================

    async def create_inquiry(self, data: Dict[str, Any]) -> SambaCSInquiry:
        return await self.repo.create_async(**data)

    # ==================== 답변 ====================

    async def reply_inquiry(
        self, inquiry_id: str, reply_content: str, mark_replied: bool = True
    ) -> Optional[SambaCSInquiry]:
        """문의에 답변 등록.
        mark_replied=False: 답변 내용만 저장, reply_status는 pending 유지 (마켓 전송 실패 시)
        """
        inquiry = await self.repo.get_async(inquiry_id)
        if not inquiry:
            return None

        update_fields: dict = {"reply": reply_content}
        if mark_replied:
            update_fields["reply_status"] = "replied"
            update_fields["replied_at"] = datetime.now(UTC)

        updated = await self.repo.update_async(inquiry_id, **update_fields)
        if mark_replied:
            logger.info(f"CS 문의 {inquiry_id} 답변 완료")
        else:
            logger.info(f"CS 문의 {inquiry_id} 답변 저장 (마켓 미전송 — pending 유지)")
        return updated or inquiry

    # ==================== 삭제 ====================

    async def delete_inquiry(self, inquiry_id: str) -> bool:
        """문의 삭제 (숨김 처리 — 동기화 시 중복 방지)."""
        updated = await self.repo.update_async(inquiry_id, is_hidden=True)
        return updated is not None

    async def delete_batch(self, ids: List[str]) -> int:
        """선택 삭제 (숨김 처리)."""
        count = 0
        for _id in ids:
            result = await self.repo.update_async(_id, is_hidden=True)
            if result:
                count += 1
        logger.info(f"CS 문의 {count}건 숨김 처리")
        return count

    # ==================== SSG CS 수집 ====================

    async def collect_from_ssg(
        self,
        ssg_client: Any,
        days_back: int = 7,
        account_id: Optional[str] = None,
        account_label: Optional[str] = None,
    ) -> Dict[str, int]:
        """SSG 쪽지/Q&A 수집 후 DB 저장."""
        now = datetime.now(UTC)
        start_dt = now - timedelta(days=days_back)
        start_date = start_dt.strftime("%Y%m%d")
        end_date = now.strftime("%Y%m%d")

        notes_collected = 0
        qna_collected = 0
        skipped = 0
        replied_marked = 0

        # ── 쪽지 수집 ──
        try:
            notes = await ssg_client.get_notes(start_date, end_date)
            active_note_ids = {
                str(n.get("boNtId", "")) for n in notes if n.get("boNtId")
            }

            # 기존 pending 쪽지 중 API에 없는 것 → replied 마킹
            existing_pending = await self.repo.find_pending_since(
                "SSG", "note", start_dt, account_id=account_id
            )
            for ep in existing_pending:
                if ep.external_id not in active_note_ids:
                    await self.repo.update_async(ep.id, reply_status="replied")
                    replied_marked += 1

            for note in notes:
                ext_id = str(note.get("boNtId", ""))
                if not ext_id:
                    continue
                existing = await self.repo.find_by_external_id("SSG", ext_id)
                if existing:
                    skipped += 1
                    continue

                raw_date = note.get("lstRegDts") or note.get("regDts")
                parsed_date = _parse_date(raw_date)

                # 상세 조회로 대화 스레드 가져오기
                content = note.get("ntCntt") or ""
                try:
                    detail = await ssg_client.get_note_detail_no_recv(ext_id)
                    talk_list = detail.get("talkList") or []
                    if isinstance(talk_list, dict):
                        talk_list = [talk_list]
                    if talk_list:
                        lines: list[str] = []
                        for talk in talk_list:
                            sender = talk.get("userNm") or talk.get("regpeId") or ""
                            is_me = talk.get("isMeYn") == "Y"
                            talk_content = talk.get("ntCntt") or ""
                            talk_date = (
                                talk.get("regDts") or talk.get("lstRegDts") or ""
                            )
                            prefix = "[업체]" if is_me else "[고객]"
                            date_str = f" ({talk_date})" if talk_date else ""
                            lines.append(f"{prefix} {sender}{date_str}\n{talk_content}")
                        content = "\n\n---\n\n".join(lines)
                except Exception as e_detail:
                    logger.debug(f"[SSG CS] 쪽지 상세 조회 실패 ({ext_id}): {e_detail}")

                await self.repo.create_async(
                    market="SSG",
                    inquiry_type="note",
                    external_id=ext_id,
                    external_sent=False,
                    account_id=account_id,
                    account_name=account_label,
                    market_order_id=note.get("ordNo"),
                    questioner=note.get("regpeId"),
                    product_name=note.get("itemNm"),
                    content=content,
                    reply_status="pending",
                    inquiry_date=parsed_date,
                )
                notes_collected += 1
        except Exception as e:
            logger.warning(f"[SSG CS] 쪽지 수집 실패: {e}")

        # ── Q&A 수집 ──
        try:
            qnas = await ssg_client.get_qna_list(start_date, end_date)
            active_qna_ids = {
                str(q.get("postngId", "")) for q in qnas if q.get("postngId")
            }

            existing_pending_qna = await self.repo.find_pending_since(
                "SSG", "qna", start_dt, account_id=account_id
            )
            for ep in existing_pending_qna:
                if ep.external_id not in active_qna_ids:
                    await self.repo.update_async(ep.id, reply_status="replied")
                    replied_marked += 1

            for qna in qnas:
                ext_id = str(qna.get("postngId", ""))
                if not ext_id:
                    continue
                existing = await self.repo.find_by_external_id("SSG", ext_id)
                if existing:
                    skipped += 1
                    continue

                raw_date = qna.get("regDts")
                parsed_date = _parse_date(raw_date)

                await self.repo.create_async(
                    market="SSG",
                    inquiry_type="qna",
                    external_id=ext_id,
                    external_sent=False,
                    account_id=account_id,
                    account_name=account_label,
                    market_order_id=qna.get("ordNo"),
                    questioner=qna.get("regpeId"),
                    product_name=qna.get("itemNm"),
                    content=qna.get("postngCntt") or qna.get("postngTitleNm") or "",
                    reply_status="pending",
                    inquiry_date=parsed_date,
                )
                qna_collected += 1
        except Exception as e:
            logger.warning(f"[SSG CS] Q&A 수집 실패: {e}")

        logger.info(
            f"[SSG CS] 수집 완료 — 쪽지 {notes_collected}건, Q&A {qna_collected}건, "
            f"스킵 {skipped}건, 답변완료 마킹 {replied_marked}건"
        )
        return {
            "notes_collected": notes_collected,
            "qna_collected": qna_collected,
            "skipped": skipped,
            "replied_marked": replied_marked,
        }

    async def send_reply_to_market(
        self,
        inquiry_id: str,
        ssg_client: Any,
    ) -> bool:
        """SSG에 답변 전송 후 external_sent=True 업데이트."""
        inquiry = await self.repo.get_async(inquiry_id)
        if not inquiry:
            logger.warning(f"[SSG CS] 문의 없음: {inquiry_id}")
            return False

        ext_id = inquiry.external_id
        reply = inquiry.reply
        if not ext_id or not reply:
            logger.warning(f"[SSG CS] external_id 또는 답변 없음: {inquiry_id}")
            return False

        try:
            if inquiry.inquiry_type == "note":
                await ssg_client.get_note_detail(ext_id)
                ok = await ssg_client.reply_note(ext_id, reply)
            else:
                ok = await ssg_client.reply_qna(ext_id, reply)
        except Exception as e:
            logger.warning(f"[SSG CS] 마켓 답변 전송 실패 ({inquiry_id}): {e}")
            return False

        if ok:
            await self.repo.update_async(inquiry_id, external_sent=True)
            logger.info(
                f"[SSG CS] 마켓 전송 완료: {inquiry_id} ({inquiry.inquiry_type})"
            )
        return ok

    # ==================== 롯데홈쇼핑 ====================

    async def collect_from_lottehome(
        self,
        lh_client: Any,
        days_back: int = 7,
        account_id: Optional[str] = None,
        account_label: Optional[str] = None,
    ) -> Dict[str, int]:
        """롯데홈쇼핑 CS문의/메모(VOC) 수집 후 DB 저장.

        external_id 형식: "ccn_no:mvot_req_sn" (답변 등록 시 둘 다 필수).
        """
        now = datetime.now(UTC)
        start_dt = now - timedelta(days=days_back)
        start_date = start_dt.strftime("%Y%m%d")
        end_date = now.strftime("%Y%m%d")

        collected = 0
        skipped = 0
        replied_marked = 0

        try:
            items = await lh_client.search_cs_voc(
                req_start_dtime=start_date,
                req_end_dtime=end_date,
                proc_stat_cd="",  # 전체
                mvot_tp_cd="",
            )
        except Exception as e:
            logger.warning(f"[롯데홈 CS] 수집 실패: {e}")
            return {"collected": 0, "skipped": 0, "replied_marked": 0}

        active_ids: set[str] = set()
        for it in items:
            ccn_no = str(it.get("CcnNo") or "").strip()
            mvot_req_sn = str(it.get("MvotReqSn") or "").strip()
            if not ccn_no or not mvot_req_sn:
                continue
            ext_id = f"{ccn_no}:{mvot_req_sn}"
            active_ids.add(ext_id)

            existing = await self.repo.find_by_external_id("롯데홈쇼핑", ext_id)
            if existing:
                # 마켓 측 처리상태가 '완료'면 replied 마킹
                if (
                    str(it.get("MvotProcStatNm", "")).strip() == "완료"
                    and existing.reply_status != "replied"
                ):
                    await self.repo.update_async(existing.id, reply_status="replied")
                    replied_marked += 1
                else:
                    skipped += 1
                continue

            raw_date = it.get("MvotReqDtime")
            parsed_date = _parse_date(raw_date)
            content = str(it.get("AnsSumrCont") or "")
            voc_type = str(it.get("VocNm") or "")
            inquiry_type = "note" if str(it.get("CcnMvotTpNm", "")) == "알림" else "qna"

            await self.repo.create_async(
                market="롯데홈쇼핑",
                inquiry_type=inquiry_type,
                external_id=ext_id,
                external_sent=False,
                account_id=account_id,
                account_name=account_label,
                market_order_id=str(it.get("OrdNo") or "") or None,
                questioner=str(it.get("MbrNm") or "") or None,
                product_name=str(it.get("GoodsNm") or "") or None,
                market_product_no=str(it.get("GoodsNo") or "") or None,
                content=f"[{voc_type}] {content}".strip() if voc_type else content,
                reply_status=(
                    "replied"
                    if str(it.get("MvotProcStatNm", "")).strip() == "완료"
                    else "pending"
                ),
                inquiry_date=parsed_date,
            )
            collected += 1

        # API 결과에 없는 기존 pending → 외부에서 처리됨으로 간주, replied 마킹
        existing_pending = await self.repo.find_pending_since(
            "롯데홈쇼핑", "qna", start_dt, account_id=account_id
        )
        for ep in existing_pending:
            if ep.external_id not in active_ids:
                await self.repo.update_async(ep.id, reply_status="replied")
                replied_marked += 1

        # ====== 상품 Q&A (searchQnAListOpenApi.lotte) 수집 ======
        # 별도 API — 상품 페이지 Q&A는 VOC와 분리되어 있음
        # external_id 형식: "QNA:{ReceiptNo}" (CS상담의 "ccn_no:mvot_req_sn"과 구별)
        qna_collected = 0
        qna_skipped = 0
        qna_replied_marked = 0
        try:
            qna_items = await lh_client.search_qna_list(
                req_start_dtime=start_date,
                req_end_dtime=end_date,
                c_val="",      # 전체 (2=상품정보Q&A, 17=핫라인)
                proc_fin_yn="",  # 전체
            )
        except Exception as e:
            logger.warning(f"[롯데홈 상품Q&A] 수집 실패: {e}")
            qna_items = []

        qna_active_ids: set[str] = set()
        for it in qna_items:
            receipt_no = str(it.get("ReceiptNo") or "").strip()
            if not receipt_no:
                continue
            ext_id = f"QNA:{receipt_no}"
            qna_active_ids.add(ext_id)

            existing = await self.repo.find_by_external_id("롯데홈쇼핑", ext_id)
            # Result: "02"=처리완료, 그 외=미처리
            is_done = str(it.get("Result", "")).strip() == "02"

            if existing:
                if is_done and existing.reply_status != "replied":
                    await self.repo.update_async(existing.id, reply_status="replied")
                    qna_replied_marked += 1
                else:
                    qna_skipped += 1
                continue

            # ReceiptDate: YYYYMMDDHHMMSS (14자) → datetime
            raw_date = str(it.get("ReceiptDate") or "").strip()
            parsed_date = _parse_date(raw_date)
            subject = str(it.get("Subject") or "")
            content = str(it.get("Content") or "")
            goods_nm = str(it.get("GoodsNm") or "")

            await self.repo.create_async(
                market="롯데홈쇼핑",
                inquiry_type="product_question",
                external_id=ext_id,
                external_sent=False,
                account_id=account_id,
                account_name=account_label,
                market_order_id=str(it.get("RcntOrdNo") or "") or None,
                questioner=str(it.get("QuestNm") or "") or None,
                product_name=goods_nm or None,
                market_product_no=str(it.get("GoodsNo") or "") or None,
                content=f"[{subject}] {content}".strip() if subject else content,
                reply_status="replied" if is_done else "pending",
                inquiry_date=parsed_date,
            )
            qna_collected += 1

        # 상품Q&A도 active_ids에 없는 기존 pending → replied 마킹
        existing_qna_pending = await self.repo.find_pending_since(
            "롯데홈쇼핑", "product_question", start_dt, account_id=account_id
        )
        for ep in existing_qna_pending:
            if ep.external_id not in qna_active_ids:
                await self.repo.update_async(ep.id, reply_status="replied")
                qna_replied_marked += 1

        logger.info(
            f"[롯데홈 CS] 수집 완료 — CS상담 {collected}건/스킵 {skipped}건/완료마킹 {replied_marked}건, "
            f"상품Q&A {qna_collected}건/스킵 {qna_skipped}건/완료마킹 {qna_replied_marked}건"
        )
        return {
            "collected": collected + qna_collected,
            "skipped": skipped + qna_skipped,
            "replied_marked": replied_marked + qna_replied_marked,
        }

    async def send_reply_to_lottehome(
        self,
        inquiry_id: str,
        lh_client: Any,
    ) -> bool:
        """롯데홈쇼핑 답변 전송.
        - CS상담(inquiry_type=qna/note, ext_id="ccn_no:mvot_req_sn") → updateCounselMemoOpenApi
        - 상품Q&A(inquiry_type=product_question, ext_id="QNA:ReceiptNo") → updateQnaAnswerOpenApi
        """
        inquiry = await self.repo.get_async(inquiry_id)
        if not inquiry:
            logger.warning(f"[롯데홈 CS] 문의 없음: {inquiry_id}")
            return False

        ext_id = inquiry.external_id or ""
        reply = inquiry.reply
        if not ext_id or not reply:
            logger.warning(f"[롯데홈 CS] external_id 또는 답변 없음: {inquiry_id}")
            return False

        # 상품Q&A 분기
        if ext_id.startswith("QNA:"):
            inq_no = ext_id[4:].strip()
            if not inq_no:
                logger.warning(f"[롯데홈 상품Q&A] ReceiptNo 없음: {ext_id}")
                return False
            try:
                api_resp = await lh_client.register_qna_answer(
                    inq_no=inq_no,
                    inq_ans_cont=reply,
                )
            except Exception as e:
                logger.warning(f"[롯데홈 상품Q&A] 답변 전송 실패 ({inquiry_id}): {e}")
                return False
            ok = bool(api_resp.get("ok"))
            if ok:
                await self.repo.update_async(
                    inquiry_id, external_sent=True, reply_status="replied"
                )
                logger.info(f"[롯데홈 상품Q&A] 답변 전송 완료: {inquiry_id}")
            else:
                logger.warning(
                    f"[롯데홈 상품Q&A] 답변 전송 실패: {inquiry_id} resp={api_resp}"
                )
            return ok

        # CS상담 분기 (기존)
        if ":" not in ext_id:
            logger.warning(
                f"[롯데홈 CS] external_id 형식 오류 (ccn_no:mvot_req_sn 필요): {ext_id}"
            )
            return False
        ccn_no, mvot_req_sn = ext_id.split(":", 1)

        try:
            api_resp = await lh_client.register_cs_voc_answer(
                ccn_no=ccn_no.strip(),
                mvot_req_sn=mvot_req_sn.strip(),
                cnsl_proc_cont=reply,
            )
        except Exception as e:
            logger.warning(f"[롯데홈 CS] 답변 전송 실패 ({inquiry_id}): {e}")
            return False

        ok = bool(api_resp.get("ok") or api_resp.get("already_done"))
        if ok:
            await self.repo.update_async(
                inquiry_id, external_sent=True, reply_status="replied"
            )
            logger.info(f"[롯데홈 CS] 답변 전송 완료: {inquiry_id}")
        else:
            logger.warning(f"[롯데홈 CS] 답변 전송 실패: {inquiry_id} resp={api_resp}")
        return ok

    # ==================== 통계 ====================

    async def get_stats(self) -> Dict[str, Any]:
        """문의 통계: 전체/미답변/답변완료/마켓별."""
        all_items = await self.repo.list_async()

        market_counts: Dict[str, int] = {}
        type_counts: Dict[str, int] = {}
        pending = 0
        replied = 0

        for item in all_items:
            market_counts[item.market] = market_counts.get(item.market, 0) + 1
            type_counts[item.inquiry_type] = type_counts.get(item.inquiry_type, 0) + 1
            if item.reply_status == "replied":
                replied += 1
            else:
                pending += 1

        return {
            "total": len(all_items),
            "pending": pending,
            "replied": replied,
            "by_market": market_counts,
            "by_type": type_counts,
        }
