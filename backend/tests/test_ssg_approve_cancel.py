"""SSG 취소승인 자동화 회귀 테스트.

배경 (2026-06-09 사용자 보고):
  다른 마켓(스마트스토어/쿠팡/롯데ON/11번가/eBay/롯데홈쇼핑)은 "취소승인" 누르면
  자동으로 처리되는데, SSG 만 "ssg 취소승인 미지원" 알림이 떴다.

  진앞: order.py 의 approve_cancel 라우터에 SSG 분기 누락. 다른 마켓은
  if/elif 체인에 들어있지만 SSG 만 빠져 마지막 else: 의 "미지원" 에러로 떨어짐.

  SSG 셀러 API 자체는 이미 구현되어 있음:
    - SSGClient.approve_cancel(ord_no, ord_item_seq)
    - POST /api/claim/v2/cancel/request/approve?ordNo=...&ordItemSeq=...
    - 주문동기화 시 ord_prd_seq 컬럼에 ordItemSeq 가 저장됨 (ssg.py:2400)

본 테스트는 다음 정적 계약을 보장:
  - approve_cancel 라우터에 SSG 분기 존재
  - SSGClient.approve_cancel 호출 시 ordNo·ordItemSeq 매핑 정확
  - PR #376 패턴 유지 — status='cancelled' 함께 update (빨간 배지 잔존 회귀 차단)
  - 인증·필드 가드 (api_key 누락 / ord_prd_seq 누락)
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


ORDER_PY = Path(__file__).resolve().parents[1] / (
    "backend/api/v1/routers/samba/order.py"
)


def _approve_cancel_body() -> str:
    """order.py 에서 approve_cancel 함수 본문 텍스트를 잘라 반환."""
    src = ORDER_PY.read_text(encoding="utf-8")
    start = src.find("async def approve_cancel(")
    assert start != -1, "approve_cancel 함수를 못 찾음"
    next_def = src.find("\nasync def ", start + 1)
    end = next_def if next_def != -1 else len(src)
    return src[start:end]


def _ssg_branch_body() -> str:
    """approve_cancel 의 SSG 분기 본문만 추출.

    'elif account.market_type == "ssg":' 부터 다음 'elif'/'else:' 직전까지.
    """
    body = _approve_cancel_body()
    marker = 'elif account.market_type == "ssg":'
    idx = body.find(marker)
    assert idx != -1, "SSG 분기 누락 — '미지원' 회귀"
    rest = body[idx + 1:]
    next_elif = rest.find("\n    elif ")
    next_else = rest.find("\n    else:")
    candidates = [c for c in (next_elif, next_else) if c != -1]
    end = (idx + 1 + min(candidates)) if candidates else len(body)
    return body[idx:end]


class TestSsgBranchRegistered:
    """라우터에 SSG 분기 존재 정적 검증."""

    def test_ssg_branch_exists(self) -> None:
        body = _approve_cancel_body()
        assert 'elif account.market_type == "ssg":' in body, (
            "approve_cancel 라우터에 SSG 분기 없음 — 'ssg 취소승인 미지원' 회귀"
        )

    def test_ssg_branch_before_else(self) -> None:
        """SSG 분기는 마지막 else (미지원) 직전에 위치해야 함."""
        body = _approve_cancel_body()
        ssg_idx = body.find('elif account.market_type == "ssg":')
        else_idx = body.rfind('else:')
        assert ssg_idx != -1 and else_idx != -1
        assert ssg_idx < else_idx, "SSG 분기가 else 보다 뒤에 있음 — 도달 불가"


class TestSsgClientWiring:
    """SSG 분기 — SSGClient.approve_cancel 호출 매핑."""

    def setup_method(self) -> None:
        self.branch = _ssg_branch_body()

    def test_imports_ssg_client(self) -> None:
        assert "SSGClient" in self.branch, "SSGClient import 누락"
        assert "SSGApiError" in self.branch, "SSGApiError import 누락 — 에러 잡기 어려움"

    def test_calls_approve_cancel(self) -> None:
        assert "client.approve_cancel(" in self.branch, "approve_cancel 호출 누락"

    def test_uses_order_number_as_ordno(self) -> None:
        # ordNo = order.order_number 첫 번째 인자
        assert "order.order_number" in self.branch, (
            "ordNo 가 order.order_number 매핑되어야 함"
        )

    def test_uses_ord_prd_seq_as_ord_item_seq(self) -> None:
        # ordItemSeq = order.ord_prd_seq 두 번째 인자 (ssg.py:2400 매핑 그대로)
        assert "order.ord_prd_seq" in self.branch, (
            "ordItemSeq 매핑 누락 — order.ord_prd_seq 를 두 번째 인자로"
        )


class TestSsgBranchGuards:
    """인증·필드 가드 — 잘못된 호출 시 사고 차단."""

    def setup_method(self) -> None:
        self.branch = _ssg_branch_body()

    def test_api_key_guard(self) -> None:
        assert 'SSG API 키 없음' in self.branch or 'api_key 없음' in self.branch.lower(), (
            "API 키 누락 시 400 가드 필수"
        )

    def test_ord_prd_seq_guard(self) -> None:
        # ord_prd_seq 미수집 시 400 — 동기화 안내. 누락 시 None 으로 호출돼 SSG API 가 실패.
        assert "ord_prd_seq" in self.branch and ("미수집" in self.branch or "없음" in self.branch), (
            "ord_prd_seq 미수집 가드 누락"
        )


class TestStatusCancelledConsistency:
    """PR #376 일관성 — status='cancelled' 도 같이 update.

    누락 시 OrdersTable.isCancelRequested 가 true 로 남아 빨간 배지·승인 버튼이
    안 사라지는 UX 사고 발생.
    """

    def test_updates_both_status_and_shipping_status(self) -> None:
        branch = _ssg_branch_body()
        assert '"status": "cancelled"' in branch or "'status': 'cancelled'" in branch, (
            "status='cancelled' update 누락 — 빨간 '취소요청' 배지 잔존 회귀"
        )
        assert "취소완료" in branch, "shipping_status='취소완료' update 누락"


class TestMissingMarketResultsInUnsupported:
    """마지막 else 의 '미지원' 메시지는 모르는 market_type 에만 떨어져야 함.

    SSG 가 그 메시지로 떨어지면 안 됨 (이게 원래 사고 진앞).
    """

    def test_unsupported_message_remains_as_fallback(self) -> None:
        body = _approve_cancel_body()
        # 미지원 메시지 자체는 보호장치로 유지 (모르는 마켓 들어왔을 때 안전)
        assert "취소승인 미지원" in body
