"""CS 문의 의도 분류기 (룰 기반, Tier 0).

프로덕션 누적 162건 분석 기반. 데이터 양이 적어(월 82건) ML 부적합 →
키워드 룰 기반 결정적 분류. inquiry_type(마켓별 비일관)은 신뢰 백본이 아니라
보조 피처로만 사용.

핵심 산출: intent + auto_send_eligible(사람 판단 불필요 여부) + confidence.
  - auto_send_eligible=True  → 즉시 자동전송 후보 (현재 notice_ack 한정)
  - auto_send_eligible=False → 초안만 작성, 운영자 검토 후 전송
"""

from __future__ import annotations

from dataclasses import dataclass


# ==================== 표준 의도 ====================
# stock_check     재고/품절/재입고 — 원소싱처 검색 필요 (draft)
# delivery_eta    배송시점/지연/도착 문의 (draft)
# tracking        송장/배송조회 (draft)
# exchange_return 교환/반품/회수 (draft, 사람 판단)
# refund_status   환불 진행/완료 문의 (draft)
# order_change    주문 취소/변경/주소변경 요청 (draft)
# sizing          사이즈/치수/착용감 (draft)
# notice_ack      마켓 플랫폼 공지/안내 (auto-send: "확인했습니다")
# product_auth    정품 여부 문의 (auto-send: "정품 맞습니다, 안심하세요")
# general         기타 (draft)

# 사람 판단 불필요 = 즉시 자동전송 가능한 의도
AUTO_SEND_INTENTS = frozenset({"notice_ack", "product_auth"})


@dataclass
class Classification:
    intent: str
    auto_send_eligible: bool
    confidence: float  # 0.0 ~ 1.0
    matched: str  # 매칭 근거 (디버그/감사용)


# 마켓 플랫폼 자기소개 — 고객이 아니라 마켓이 보낸 공지의 신호
_PLATFORM_INTRO = (
    "11번가입니다",
    "쿠팡입니다",
    "스마트스토어입니다",
    "네이버",
    "위메프",
    "티몬",
    "지마켓",
    "옥션입니다",
    "판매자센터",
    "셀러",
)
# 공지/안내성 키워드 (고객 질문이 아님)
_NOTICE_KW = (
    "안내드립니다",
    "안내 드립니다",
    "공지",
    "이벤트",
    "참여 안내",
    "참여안내",
    "셀러톡",
    "미발송률",
    "정산",
    "프로모션",
    "캠페인",
    "신청 안내",
    "기획전",
    "안내메일",
    "안내 메일",
)

# 의도별 키워드 (우선순위 순서대로 평가)
_STOCK_KW = (
    "재고",
    "품절",
    "재입고",
    "입고",
    "다팔",
    "남았",
    "남아",
    "솔드아웃",
    "수량",
)
_SIZING_KW = (
    "사이즈",
    "치수",
    "둘레",
    "정사이즈",
    "착용감",
    "크기",
    "cm",
    "발볼",
    "키",
    "몸무게",
)
_TRACKING_KW = ("송장", "운송장", "택배사", "배송조회", "어디쯤", "배송추적")
_DELIVERY_KW = (
    "배송",
    "출고",
    "도착",
    "언제",
    "발송",
    "받을",
    "오나요",
    "지연",
    "며칠",
)
_EXCHANGE_KW = (
    "교환",
    "반품",
    "회수",
    "맞교환",
    "오배송",
    "불량",
    "파손",
    "다른 상품",
    "잘못",
)
_REFUND_KW = ("환불", "결제취소", "입금", "돈", "페이백")
_ORDER_CHANGE_KW = ("취소", "변경", "주소 변경", "주소변경", "옵션 변경", "수정")
_PRODUCT_AUTH_KW = ("정품", "가품", "짝퉁", "진품", "정품인가", "정품 맞", "진정품")


def _has(text: str, kws: tuple[str, ...]) -> str | None:
    for kw in kws:
        if kw in text:
            return kw
    return None


def classify(
    content: str,
    inquiry_type: str | None = None,
    market: str | None = None,
    questioner: str | None = None,
) -> Classification:
    """문의 1건을 표준 의도로 분류.

    추측 없이 키워드 매칭만 — 매칭 없으면 general(낮은 신뢰도)로 보수 분류.
    """
    text = (content or "").strip()
    low = text.lower()

    # 1) 마켓 공지/안내 (notice_ack) — 자동전송 후보. 가장 강한 신호 우선.
    #    플랫폼 자기소개 + 공지 키워드 동시 충족, 또는 inquiry_type=urgent_inquiry
    intro = _has(text, _PLATFORM_INTRO)
    notice = _has(text, _NOTICE_KW)
    if (intro and notice) or inquiry_type == "urgent_inquiry":
        # 단, 고객 질문 신호(물음표 + 재고/배송/환불 키워드)가 있으면 공지 아님
        is_question = ("?" in text or "나요" in text or "까요" in text) and (
            _has(text, _STOCK_KW)
            or _has(text, _DELIVERY_KW)
            or _has(text, _REFUND_KW)
            or _has(text, _EXCHANGE_KW)
        )
        if not is_question:
            return Classification(
                intent="notice_ack",
                auto_send_eligible=True,
                confidence=0.9 if (intro and notice) else 0.8,
                matched=f"platform_notice:{intro or inquiry_type}",
            )

    # 2) 고객 문의 — 의도별 키워드 (구체적 → 일반 순)
    for intent, kws, conf in (
        ("product_auth", _PRODUCT_AUTH_KW, 0.95),  # 정품 문의: 단순·답 확정 → 최우선
        ("exchange_return", _EXCHANGE_KW, 0.8),
        ("refund_status", _REFUND_KW, 0.75),
        ("sizing", _SIZING_KW, 0.8),
        ("stock_check", _STOCK_KW, 0.8),
        ("tracking", _TRACKING_KW, 0.8),
        ("order_change", _ORDER_CHANGE_KW, 0.7),
        ("delivery_eta", _DELIVERY_KW, 0.7),
    ):
        hit = _has(low if intent == "sizing" else text, kws)
        if hit:
            return Classification(
                intent=intent,
                auto_send_eligible=intent in AUTO_SEND_INTENTS,
                confidence=conf,
                matched=f"kw:{hit}",
            )

    # 3) 미매칭 — general (낮은 신뢰도, 무조건 사람 검토)
    return Classification(
        intent="general",
        auto_send_eligible=False,
        confidence=0.3,
        matched="none",
    )
