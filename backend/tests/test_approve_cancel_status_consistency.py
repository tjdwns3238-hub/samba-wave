"""approve_cancel 마켓별 분기의 status 업데이트 일관성 회귀 테스트.

배경 (2026-06-08 사용자 보고):
  스마트스토어 주문에서 "취소승인" 누른 뒤에도 빨간 '취소요청' 배지와
  '취소 승인/거부' 버튼이 사라지지 않고 그대로 떠 있는 사고.

  진앞: approve_cancel 스마트스토어/11번가 분기가 shipping_status='취소완료'
  로만 update 하고 status 는 'cancel_requested' 그대로 두던 비일관성.
  프론트(OrdersTable) 의 isCancelRequested = (status === 'cancel_requested')
  가 true 로 유지돼 배지가 안 사라짐.

  쿠팡/롯데ON/eBay 분기는 처음부터 status='cancelled' 도 같이 update —
  스마트스토어/11번가만 누락.

본 테스트:
  approve_cancel 의 모든 마켓 분기가 status='cancelled' 를 함께 update 하는지
  정적 검증. 누락된 PR 머지 회귀 차단용.
"""

from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


ORDER_PY = Path(__file__).resolve().parents[1] / (
    "backend/api/v1/routers/samba/order.py"
)


def _extract_approve_cancel_body() -> str:
    """order.py 에서 approve_cancel 함수 본문 텍스트를 잘라 반환.

    다음 함수 정의(`async def `) 가 나타나는 지점까지를 본문으로 본다.
    """
    src = ORDER_PY.read_text(encoding="utf-8")
    start = src.find("async def approve_cancel(")
    assert start != -1, "approve_cancel 함수를 못 찾음"
    # 다음 async def 까지가 본문
    next_def = src.find("\nasync def ", start + 1)
    end = next_def if next_def != -1 else len(src)
    return src[start:end]


class TestApproveCancelStatusConsistency:
    """approve_cancel 마켓별 분기의 status='cancelled' 업데이트 일관성."""

    def setup_method(self) -> None:
        self.body = _extract_approve_cancel_body()

    def _branch_body(self, market_marker: str) -> str:
        """특정 마켓 분기 본문 (다음 elif/else 또는 함수 끝까지)."""
        idx = self.body.find(market_marker)
        assert idx != -1, f"분기 미발견: {market_marker}"
        # 다음 elif/else 가 나타나는 지점까지를 분기 본문으로 본다
        next_elif = self.body.find("\n    elif ", idx + 1)
        next_else = self.body.find("\n    else:", idx + 1)
        candidates = [c for c in (next_elif, next_else) if c != -1]
        end = min(candidates) if candidates else len(self.body)
        return self.body[idx:end]

    def test_smartstore_branch_sets_status_cancelled(self) -> None:
        """스마트스토어 분기에서 status='cancelled' update 필수.

        2026-06-08 회귀: shipping_status 만 update 하면 isCancelRequested
        가 true 로 남아 빨간 배지가 안 사라짐.
        """
        branch = self._branch_body('if account.market_type == "smartstore":')
        assert '"status": "cancelled"' in branch or "'status': 'cancelled'" in branch, (
            "스마트스토어 approve_cancel 분기에 status='cancelled' update 누락 — "
            "빨간 '취소요청' 배지가 처리 후에도 안 사라지는 UX 버그 회귀"
        )

    def test_elevenst_branch_sets_status_cancelled(self) -> None:
        """11번가 분기도 같은 일관성."""
        branch = self._branch_body('elif account.market_type == "11st":')
        assert '"status": "cancelled"' in branch or "'status': 'cancelled'" in branch, (
            "11번가 approve_cancel 분기에 status='cancelled' update 누락"
        )

    def test_coupang_branch_sets_status_cancelled(self) -> None:
        """쿠팡 분기 — 회귀 방지용 보호 가드 (이미 정상이지만 누가 뺴면 막음)."""
        branch = self._branch_body('elif account.market_type == "coupang":')
        assert '"status": "cancelled"' in branch or "'status': 'cancelled'" in branch

    def test_lotteon_branch_sets_status_cancelled(self) -> None:
        """롯데ON 분기 — 회귀 방지용."""
        branch = self._branch_body('elif account.market_type == "lotteon":')
        assert '"status": "cancelled"' in branch or "'status': 'cancelled'" in branch

    def test_ebay_branch_sets_status_cancelled(self) -> None:
        """eBay 분기 — 회귀 방지용."""
        branch = self._branch_body('elif account.market_type == "ebay":')
        assert '"status": "cancelled"' in branch or "'status': 'cancelled'" in branch

    def test_all_market_branches_consistent(self) -> None:
        """전체 일관성 — approve_cancel 함수 내 모든 update_order 호출이
        shipping_status='취소완료' 와 함께 status='cancelled' 를 갖는지 검증.

        markets 분기에서 update_order(order_id, {...}) 호출 모두 추출 후
        '취소완료' 가 들어가는 dict 는 'cancelled' 도 함께 가져야 함.
        """
        # 본문 안의 update_order(...) 호출 dict 모두 추출
        # 정규식: update_order\(\s*order_id\s*,\s*\{ ... \} \s*\)
        pattern = re.compile(r"update_order\(\s*order_id\s*,\s*(\{[^}]*\})", re.DOTALL)
        matches = pattern.findall(self.body)
        assert matches, "approve_cancel 본문에서 update_order 호출 미발견"

        bad = []
        for dict_str in matches:
            has_shipping_complete = "취소완료" in dict_str
            has_status_cancelled = "cancelled" in dict_str
            if has_shipping_complete and not has_status_cancelled:
                bad.append(dict_str.strip())

        assert not bad, (
            "shipping_status='취소완료' 를 set 하면서 status='cancelled' 누락한 "
            f"update_order 호출 발견 ({len(bad)} 개): {bad}"
        )
