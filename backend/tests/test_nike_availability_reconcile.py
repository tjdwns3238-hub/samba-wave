"""Nike size-level availability reconcile 단위 테스트.

배경: PDP `__NEXT_DATA__.selectedProduct.sizes[*].status` 는 listing
availability 메타데이터일 뿐 size-level stock source 가 아님. 일부 SKU 에서
모든 사이즈가 ACTIVE 로 평탄화되어 sold_out/restock 감지 불가한 경로가 있음.

`NikeClient._fetch_availability()` (threads API GTIN→bool) 가 이미 존재하지만
`NikePlugin.refresh()` 에서 호출되지 않음. 이 테스트는 reconcile 로직을
opt-in (NIKE_AVAILABILITY_ENABLED=1) 으로 연결한 동작을 검증한다.

회귀 대상:
- env off 시 기존 동작 그대로 (외부 API 호출 없음)
- env on + threads API 성공 시 matched GTIN 만 stock 업데이트
- env on + 응답 누락 GTIN 은 기존 stock 유지 (절대 0 강제 X)
- env on + threads API 예외/빈 응답 시 기존 결과 유지 (no-op)
"""

import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.domain.samba.plugins.sourcing.nike import NikePlugin


class _FakeProduct:
    def __init__(self, options: list | None = None) -> None:
        self.id = "col_nike_test"
        self.site_product_id = "IM1338-060"
        self.name = "테스트 상품"
        self.sale_status = "in_stock"
        self.sale_price = 143100
        self.original_price = 159000
        self.cost = 100000
        self.options = options or []


def _make_fresh_detail() -> dict:
    """메인테이너 _parse_pdp_data 가 ACTIVE→99 평탄화한 상태의 fresh detail.

    매 테스트마다 신선한 dict 를 반환 (reconcile 로직이 옵션을 mutate 하므로).
    """
    return {
        "sale_price": 143100,
        "original_price": 159000,
        "options": [
            {"name": "XS", "size": "XS", "gtin": "GTIN_XS", "stock": 99},
            {"name": "S", "size": "S", "gtin": "GTIN_S", "stock": 99},
            {"name": "M", "size": "M", "gtin": "GTIN_M", "stock": 99},
            {"name": "L", "size": "L", "gtin": "GTIN_L", "stock": 99},
            {"name": "XL", "size": "XL", "gtin": "GTIN_XL", "stock": 99},
        ],
    }


def _patch_get_detail(monkeypatch, detail: dict) -> None:
    """NikeClient.get_detail 를 fresh detail 반환으로 monkeypatch."""
    from backend.domain.samba.proxy import nike as nike_proxy

    async def _fake(self, style_color, pdp_url=None, base_info=None):  # noqa: ANN001
        return detail

    monkeypatch.setattr(
        nike_proxy.NikeClient, "get_detail", _fake, raising=True
    )


def _patch_fetch_availability(monkeypatch, result_or_exc) -> None:
    """NikeClient._fetch_availability 를 monkeypatch.

    result_or_exc: dict 면 반환, Exception 이면 raise.
    """
    from backend.domain.samba.proxy import nike as nike_proxy

    async def _fake(self, style_color):  # noqa: ANN001
        if isinstance(result_or_exc, BaseException):
            raise result_or_exc
        return result_or_exc

    monkeypatch.setattr(
        nike_proxy.NikeClient, "_fetch_availability", _fake, raising=True
    )


def _opts_by_size(options) -> dict:
    return {o["size"]: o["stock"] for o in options if isinstance(o, dict)}


class TestNikeAvailabilityReconcile:
    def test_env_off_skips_availability_call_and_keeps_parser_result(
        self, monkeypatch
    ) -> None:
        # env 비활성 → _fetch_availability 호출되면 안 됨, 기존 99 평탄화 유지
        monkeypatch.delenv("NIKE_AVAILABILITY_ENABLED", raising=False)
        _patch_get_detail(monkeypatch, _make_fresh_detail())
        called = {"n": 0}

        from backend.domain.samba.proxy import nike as nike_proxy

        async def _should_not_be_called(self, style_color):  # noqa: ANN001
            called["n"] += 1
            return {}

        monkeypatch.setattr(
            nike_proxy.NikeClient,
            "_fetch_availability",
            _should_not_be_called,
            raising=True,
        )

        product = _FakeProduct(
            options=[{"name": s, "size": s, "stock": 99} for s in ("XS", "S")]
        )
        r = asyncio.run(NikePlugin().refresh(product))

        assert called["n"] == 0
        assert _opts_by_size(r.new_options) == {
            "XS": 99,
            "S": 99,
            "M": 99,
            "L": 99,
            "XL": 99,
        }

    def test_env_on_overrides_matched_gtins_only(self, monkeypatch) -> None:
        # env on + API 응답 → matched GTIN 만 보정, 응답 없는 GTIN 은 기존 유지
        monkeypatch.setenv("NIKE_AVAILABILITY_ENABLED", "1")
        _patch_get_detail(monkeypatch, _make_fresh_detail())
        _patch_fetch_availability(
            monkeypatch,
            {
                "GTIN_XS": True,
                "GTIN_S": False,
                "GTIN_M": False,
                # L/XL 은 응답에 없음 — 누락 → 기존 stock(99) 유지 보수적
            },
        )

        product = _FakeProduct(
            options=[{"name": s, "size": s, "stock": 99} for s in ("XS", "S")]
        )
        r = asyncio.run(NikePlugin().refresh(product))

        result = _opts_by_size(r.new_options)
        assert result["XS"] == 99  # API True → 99
        assert result["S"] == 0  # API False → 0
        assert result["M"] == 0  # API False → 0
        assert result["L"] == 99  # 응답 누락 → 기존 유지
        assert result["XL"] == 99  # 응답 누락 → 기존 유지

    def test_availability_exception_falls_back_to_parser_result(
        self, monkeypatch
    ) -> None:
        # env on 이지만 _fetch_availability 가 예외 → 기존 파서 결과 유지
        monkeypatch.setenv("NIKE_AVAILABILITY_ENABLED", "1")
        _patch_get_detail(monkeypatch, _make_fresh_detail())
        _patch_fetch_availability(monkeypatch, RuntimeError("network error"))

        product = _FakeProduct(
            options=[{"name": s, "size": s, "stock": 99} for s in ("XS", "S")]
        )
        r = asyncio.run(NikePlugin().refresh(product))

        assert _opts_by_size(r.new_options) == {
            "XS": 99,
            "S": 99,
            "M": 99,
            "L": 99,
            "XL": 99,
        }

    def test_empty_availability_response_keeps_parser_result(
        self, monkeypatch
    ) -> None:
        # env on + 빈 응답({}) → no-op, 기존 결과 그대로
        monkeypatch.setenv("NIKE_AVAILABILITY_ENABLED", "1")
        _patch_get_detail(monkeypatch, _make_fresh_detail())
        _patch_fetch_availability(monkeypatch, {})

        product = _FakeProduct(
            options=[{"name": s, "size": s, "stock": 99} for s in ("XS", "S")]
        )
        r = asyncio.run(NikePlugin().refresh(product))

        assert _opts_by_size(r.new_options) == {
            "XS": 99,
            "S": 99,
            "M": 99,
            "L": 99,
            "XL": 99,
        }

    def test_all_sizes_oos_marks_sold_out(self, monkeypatch) -> None:
        # env on + 모든 GTIN false → 모든 옵션 stock=0 → new_sale_status=sold_out
        monkeypatch.setenv("NIKE_AVAILABILITY_ENABLED", "1")
        _patch_get_detail(monkeypatch, _make_fresh_detail())
        _patch_fetch_availability(
            monkeypatch,
            {
                "GTIN_XS": False,
                "GTIN_S": False,
                "GTIN_M": False,
                "GTIN_L": False,
                "GTIN_XL": False,
            },
        )

        product = _FakeProduct(
            options=[{"name": s, "size": s, "stock": 99} for s in ("XS", "S")]
        )
        r = asyncio.run(NikePlugin().refresh(product))

        assert _opts_by_size(r.new_options) == {
            "XS": 0,
            "S": 0,
            "M": 0,
            "L": 0,
            "XL": 0,
        }
        assert r.new_sale_status == "sold_out"
        assert r.stock_changed is True
