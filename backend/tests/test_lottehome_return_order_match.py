"""롯데홈쇼핑 반품 주문 수집/반영 회귀 테스트 (issue #393).

배경 (대리점 계정 + 반품 OrdDtlSn≠OrgOrdDtlSn 환경):
  반품접수된 주문이 삼바 주문관리에 반영 안 됨. 원인 4겹.

  ① order_number/shipment_id 불일치:
     반품 API(searchReturnList)는 OrdDtlSn 에 새 클레임 라인번호를 발급 →
     원주문(OrgOrdDtlSn 기준)과 order_number 가 어긋나 upsert 매칭 실패.
     fix = 반품 파싱 시 prefer_org_dtl_sn=True 로 OrgOrdDtlSn 우선 통일.
     (취소 경로는 OrgOrdDtlSn 검증이 안 됐으므로 현행 OrdDtlSn 우선 유지.)

  ② update 경로 status 매핑에 반품 케이스 부재:
     '반품요청'→return_requested, '회수확정/반품완료'→return_completed 추가.

  ③ samba_order.shipment_id 인덱스 부재 → 미매칭 중복체크 Seq Scan →
     per-account 300초 타임아웃. CONCURRENTLY 인덱스 추가.

  ④ 신규 insert 경로 _normalize_synced_order_status 가 lottehome 반품
     status 를 pending 으로 덮음 → 미매칭 반품이 pending 으로 저장.
     lottehome 클레임 상태도 보존하도록 예외 확장.

테스트:
  ①은 순수함수 동작 검증(ast 격리 — order.py 는 순환 import 라 직접 import 불가).
  ②③④는 소스/마이그레이션 정적 계약 가드.
"""

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ORDER_PY = BACKEND_ROOT / "backend/api/v1/routers/samba/order.py"
MODEL_PY = BACKEND_ROOT / "backend/domain/samba/order/model.py"
VERSIONS_DIR = BACKEND_ROOT / "alembic/versions"


def _load_parse_funcs() -> dict:
    """order.py 에서 _parse_lottehome_order(_multi) 함수만 격리 컴파일.

    order.py 전체는 순환 import 라 import 불가 → AST 로 대상 함수만 떼어
    최소 네임스페이스(logger/re stub)에서 exec.
    """
    src = ORDER_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {"_parse_lottehome_order", "_parse_lottehome_order_multi"}
    funcs = [
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted
    ]
    mod = ast.Module(body=funcs, type_ignores=[])

    class _LoggerStub:
        def warning(self, *a, **k) -> None:
            pass

        def info(self, *a, **k) -> None:
            pass

    import re as _re

    ns: dict = {"logger": _LoggerStub(), "re": _re}
    exec(compile(mod, "<isolated>", "exec"), ns)
    return ns


class TestReturnOrgOrdDtlSnUnification:
    """① 반품 파싱 시 OrgOrdDtlSn 우선 통일 — 원주문 매칭."""

    def setup_method(self) -> None:
        self.parse = _load_parse_funcs()["_parse_lottehome_order"]

    def test_new_order_unaffected(self) -> None:
        """신규주문(OrdDtlSn 없음, OrgOrdDtlSn 만) — prefer 여부와 무관하게 동일."""
        item = {
            "OrdNo": "20260606J08079",
            "ProdInfo": {
                "OrgOrdDtlSn": "1125688779",
                "ProdCode": "X",
                "ProdName": "t",
            },
        }
        r_def = self.parse(dict(item), "acc", "lbl")
        r_pref = self.parse(dict(item), "acc", "lbl", prefer_org_dtl_sn=True)
        assert (
            r_def["order_number"]
            == r_pref["order_number"]
            == "20260606J08079:1125688779"
        ), "신규수집 회귀 — prefer 가 신규주문 번호를 바꾸면 안 됨"
        assert r_def["shipment_id"] == "1125688779"

    def test_return_prefers_org_for_match(self) -> None:
        """반품: OrdDtlSn(새 클레임) + OrgOrdDtlSn(원번호) → prefer 시 원번호 사용."""
        item = {
            "OrdNo": "20260606J08079",
            "ProdInfo": {
                "OrdDtlSn": "1125836467",  # 반품 새 클레임 라인번호
                "OrgOrdDtlSn": "1125688779",  # 원주문 라인번호
                "ProdCode": "X",
                "ProdName": "t",
            },
        }
        r_def = self.parse(dict(item), "acc", "lbl")
        r_pref = self.parse(dict(item), "acc", "lbl", prefer_org_dtl_sn=True)
        # 기존(default) 동작: OrdDtlSn(새 클레임) → 원주문과 어긋남
        assert r_def["order_number"] == "20260606J08079:1125836467"
        # fix: OrgOrdDtlSn(원번호) → 원주문과 매칭
        assert r_pref["order_number"] == "20260606J08079:1125688779"
        assert r_pref["shipment_id"] == "1125688779"
        assert r_pref["ext_order_number"] == "20260606J08079:1125688779"

    def test_return_fallback_when_org_missing(self) -> None:
        """반품인데 OrgOrdDtlSn 누락 → OrdDtlSn 폴백(기존 동작, 안전)."""
        item = {
            "OrdNo": "AA",
            "ProdInfo": {"OrdDtlSn": "999", "ProdCode": "X", "ProdName": "t"},
        }
        r = self.parse(dict(item), "acc", "lbl", prefer_org_dtl_sn=True)
        assert r["order_number"] == "AA:999"


class TestReturnCallSitePrefersOrg:
    """① 반품 조회 호출부가 prefer_org_dtl_sn=True 로 호출하는지(취소는 미적용)."""

    def setup_method(self) -> None:
        self.src = ORDER_PY.read_text(encoding="utf-8")

    def test_return_parse_call_uses_prefer(self) -> None:
        assert "prefer_org_dtl_sn=True" in self.src, (
            "반품 파싱이 prefer_org_dtl_sn=True 로 호출돼야 원주문 매칭"
        )


class TestUpdateStatusMappingReturn:
    """② update 경로 status 매핑에 반품 케이스 존재."""

    def setup_method(self) -> None:
        self.src = ORDER_PY.read_text(encoding="utf-8")

    def test_return_requested_branch(self) -> None:
        assert '_new_ss_final == "반품요청"' in self.src, (
            "update 경로 '반품요청'→return_requested 매핑 누락"
        )

    def test_return_completed_branch(self) -> None:
        # '회수확정','반품완료' → return_completed
        assert '"회수확정"' in self.src and '"반품완료"' in self.src, (
            "update 경로 회수확정/반품완료→return_completed 매핑 누락"
        )


class TestNormalizePreservesLottehomeReturn:
    """④ _normalize_synced_order_status 가 lottehome 클레임 상태 보존."""

    def setup_method(self) -> None:
        self.src = ORDER_PY.read_text(encoding="utf-8")

    def test_lottehome_preserve_branch(self) -> None:
        idx = self.src.find("def _normalize_synced_order_status(")
        assert idx != -1
        body = self.src[idx : idx + 1500]
        assert '"lottehome"' in body and "in preserved" in body, (
            "신규 insert 경로에서 lottehome 반품 status 가 pending 으로 덮임 — 보존 누락"
        )


class TestShipmentIdIndex:
    """③ shipment_id 인덱스 (model + 마이그레이션)."""

    def test_model_index_flag(self) -> None:
        src = MODEL_PY.read_text(encoding="utf-8")
        idx = src.find("shipment_id: Optional[str]")
        assert idx != -1
        assert "index=True" in src[idx : idx + 200], "model.shipment_id index=True 누락"

    def test_concurrently_migration_exists(self) -> None:
        hit = False
        for f in VERSIONS_DIR.glob("*.py"):
            t = f.read_text(encoding="utf-8")
            if (
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_samba_order_shipment_id"
                in t
            ):
                hit = True
                # CONCURRENTLY 는 수동 COMMIT 패턴(트랜잭션 밖) 이어야 함
                assert 'exec_driver_sql("COMMIT")' in t, (
                    "CONCURRENTLY 마이그레이션은 수동 COMMIT 선행 필요"
                )
        assert hit, "ix_samba_order_shipment_id CONCURRENTLY 마이그레이션 누락"
