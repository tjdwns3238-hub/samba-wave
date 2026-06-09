"""쿠팡 취소·반품 sync — cancel_map fallback 회귀 테스트.

배경 (2026-06-09 사용자 보고):
  쿠팡 Wing "출고중지요청" 3건이 마켓에 들어와 있는데 삼바엔 빨간 '취소요청'
  배지가 안 뜨고 주문상태도 '상품준비중' 으로 표시되는 사고.
  (예: 휠라 3SM01951F 데일리샌들 / 주문번호 1010099522)

진앞:
  order.py 의 쿠팡 sync 흐름에서 cancel_map 매핑 키 등록 시,
  `cancelItems[].vendorItemId` 와 raw_orders 의 `orderItems[0].vendorItemId` 가
  옵션 차이로 어긋나면 `cancel_map[(oid, vid)]` 정확매칭 실패.

  매칭측은 `cancel_map.get((oid, None))` fallback 을 시도하지만, 등록측에서
  items 가 비어있을 때만 `(oid, None)` 키를 만들어 fallback 자체가 존재하지 않아
  cancel_info=None 으로 떨어지고, _parse_coupang_order 에서 일반 status_map 으로
  매핑돼 'cancel_requested' 가 안 됨.

수정:
  - 모든 receipt 등록 시 `(oid, None)` fallback 키 항상 같이 등록 (CANCEL 우선).
  - 매칭 실패 시 운영 추적용 logger.warning (회귀 빠른 감지).
  - 쿠팡 returnRequests status 리스트에 의도 주석 보강 (UC=출고중지요청미확인).
  - get_cancel_and_return_requests 가 receiptStatus 빈도 로깅 (새 status 코드 회귀 감지).

본 테스트:
  fix 의 정적 계약 회귀 가드 — 누군가 fallback 제거하거나 logging 빠뜨리는
  PR 머지를 차단.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ORDER_PY = BACKEND_ROOT / "backend/api/v1/routers/samba/order.py"
COUPANG_PY = BACKEND_ROOT / "backend/domain/samba/proxy/coupang.py"


class TestCancelMapFallbackRegistered:
    """fix A — cancel_map 등록 시 (oid, None) fallback 항상 등록."""

    def setup_method(self) -> None:
        self.src = ORDER_PY.read_text(encoding="utf-8")

    def test_fallback_register_call_exists(self) -> None:
        """(oid, None) fallback 등록 호출이 명시적으로 존재.

        '_register((oid, None))' 또는 동등한 fallback 패턴.
        """
        # 우리 fix 에서 _register 헬퍼로 분리하고 (oid, None) 도 호출
        assert "_register((oid, None))" in self.src, (
            "(oid, None) fallback 등록 누락 — vendorItemId mismatch 매칭 실패 회귀"
        )

    def test_register_helper_uses_cancel_priority(self) -> None:
        """_register 헬퍼 안에 CANCEL 우선 정책 유지."""
        # 헬퍼 정의 발견 후 그 안에 CANCEL 우선 분기 있는지
        idx = self.src.find("def _register(")
        assert idx != -1, "_register 헬퍼 정의 누락"
        # 헬퍼 끝까지 (다음 def 또는 비슷한 패턴까지) 본문
        end = self.src.find("for vid in vids", idx)
        body = self.src[idx:end] if end != -1 else self.src[idx : idx + 1500]
        assert '"CANCEL"' in body, "헬퍼 내 CANCEL 우선 정책 누락"


class TestUnmatchedCancelLoggingPresent:
    """매칭 실패 시 운영 추적용 logger.warning 존재."""

    def setup_method(self) -> None:
        self.src = ORDER_PY.read_text(encoding="utf-8")

    def test_unmatched_warning(self) -> None:
        # 매칭 실패 진단 메시지 — orderId 가 cancel_map 안 다른 키에는 있는데
        # 정확매칭+fallback 둘 다 실패한 케이스.
        assert "vendorItemId" in self.src and "매칭 실패" in self.src, (
            "매칭 실패 logger.warning 누락 — 운영 추적 불가"
        )


class TestCoupangSyncStatusListDocumented:
    """fix B — coupang.py 의 returnRequests status 리스트 의도 주석 보강."""

    def setup_method(self) -> None:
        self.src = COUPANG_PY.read_text(encoding="utf-8")

    def test_four_statuses_kept(self) -> None:
        """4개 status (RU, CC, PR, UC) 유지 — UC 가 출고중지요청 잡음."""
        assert '["RU", "CC", "PR", "UC"]' in self.src, (
            "쿠팡 status 리스트 변형 — UC 가 출고중지요청 잡으므로 유지 필요"
        )

    def test_release_stop_unchecked_intent_documented(self) -> None:
        """UC = RELEASE_STOP_UNCHECKED 의도가 주석에 명시되어 있어야."""
        assert "RELEASE_STOP_UNCHECKED" in self.src or "출고전 중지요청" in self.src, (
            "UC=출고중지요청미확인 의도 주석 누락 — 다음 개발자가 모르고 status 빼는 회귀"
        )


class TestReceiptStatusFrequencyLogging:
    """fix B — get_cancel_and_return_requests 가 receiptStatus 빈도 로깅."""

    def setup_method(self) -> None:
        self.src = COUPANG_PY.read_text(encoding="utf-8")

    def test_receipt_status_freq_logging(self) -> None:
        """쿠팡이 새 status 도입 시 우리 4개 리스트 누락을 감지하는 로깅 필요."""
        assert "receiptStatus" in self.src and "status_freq" in self.src, (
            "receiptStatus 빈도 로깅 누락 — 새 status 코드 도입 시 회귀 감지 불가"
        )
