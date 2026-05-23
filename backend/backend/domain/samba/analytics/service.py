"""SambaWave Analytics service - cross-domain statistics (ported from js/modules/analytics.js)."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional

from backend.domain.samba.channel.repository import SambaChannelRepository
from backend.domain.samba.order.repository import SambaOrderRepository
from backend.domain.samba.product.repository import SambaProductRepository


class SambaAnalyticsService:
    def __init__(
        self,
        order_repo: SambaOrderRepository,
        product_repo: SambaProductRepository,
        channel_repo: SambaChannelRepository,
    ):
        self.order_repo = order_repo
        self.product_repo = product_repo
        self.channel_repo = channel_repo

    # ==================== 내부 헬퍼 ====================

    @staticmethod
    def _filter_by_tenant(orders: list, tenant_id: Optional[str]) -> list:
        """tenant_id가 주어진 경우 해당 테넌트 주문만 반환.
        NULL tenant_id 레코드는 기존 레거시 데이터로 간주해 포함한다.
        backfill 완료 후 `not o.tenant_id` 조건을 제거할 것.
        """
        if not tenant_id:
            return orders
        return [o for o in orders if not o.tenant_id or o.tenant_id == tenant_id]

    # ==================== Today ====================

    async def get_today_stats(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """오늘 기준 매출/주문/수익 통계."""
        now = datetime.now(UTC)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._compute_stats_for_period(
            start_of_day, now, tenant_id=tenant_id
        )

    # ==================== Date Range ====================

    async def get_stats_by_date_range(
        self, start_date: datetime, end_date: datetime, tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """기간별 매출/주문/수익 통계."""
        return await self._compute_stats_for_period(
            start_date, end_date, tenant_id=tenant_id
        )

    # ==================== By Channel ====================

    async def get_sales_by_channel(
        self, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """채널별 매출 통계."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )
        all_channels = await self.channel_repo.list_async()

        channel_map: Dict[str, str] = {ch.id: ch.name for ch in all_channels}

        agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"sales": 0.0, "orders": 0, "profit": 0.0}
        )

        for order in all_orders:
            ch_id = order.channel_id or "unknown"
            ch_name = channel_map.get(ch_id, order.channel_name or "기타")
            key = ch_id

            agg[key]["channel_name"] = ch_name
            agg[key]["sales"] += order.sale_price * order.quantity
            agg[key]["orders"] += 1
            agg[key]["profit"] += order.profit

        result = list(agg.values())
        result.sort(key=lambda x: x["sales"], reverse=True)
        return result

    # ==================== By Product ====================

    async def get_sales_by_product(
        self, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """상품별 매출 통계."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )

        agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"sales": 0.0, "orders": 0, "profit": 0.0, "units": 0}
        )

        for order in all_orders:
            p_id = order.product_id or "unknown"
            p_name = order.product_name or "기타"

            agg[p_id]["product_name"] = p_name
            agg[p_id]["sales"] += order.sale_price * order.quantity
            agg[p_id]["orders"] += 1
            agg[p_id]["profit"] += order.profit
            agg[p_id]["units"] += order.quantity

        result = list(agg.values())
        result.sort(key=lambda x: x["sales"], reverse=True)
        return result

    # ==================== Daily Trend ====================

    async def get_daily_trend(
        self, days: int = 30, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """일별 매출 트렌드."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=days)

        daily: Dict[str, Dict[str, Any]] = {}

        # 빈 날짜 초기화
        for i in range(days):
            date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            daily[date] = {"date": date, "sales": 0.0, "orders": 0, "profit": 0.0}

        for order in all_orders:
            if order.created_at < cutoff:
                continue
            date_str = order.created_at.strftime("%Y-%m-%d")
            if date_str not in daily:
                daily[date_str] = {
                    "date": date_str,
                    "sales": 0.0,
                    "orders": 0,
                    "profit": 0.0,
                }

            daily[date_str]["sales"] += order.sale_price * order.quantity
            daily[date_str]["orders"] += 1
            daily[date_str]["profit"] += order.profit

        result = list(daily.values())
        result.sort(key=lambda x: x["date"])
        return result

    # ==================== Monthly Comparison ====================

    async def get_monthly_comparison(
        self, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """월별 매출 비교."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )

        monthly: Dict[str, Dict[str, Any]] = {}

        for order in all_orders:
            month_str = order.created_at.strftime("%Y-%m")
            if month_str not in monthly:
                monthly[month_str] = {
                    "month": month_str,
                    "sales": 0.0,
                    "orders": 0,
                    "profit": 0.0,
                }

            monthly[month_str]["sales"] += order.sale_price * order.quantity
            monthly[month_str]["orders"] += 1
            monthly[month_str]["profit"] += order.profit

        result = list(monthly.values())
        result.sort(key=lambda x: x["month"])
        return result

    # ==================== KPI Summary ====================

    async def get_kpi_summary(self, tenant_id: Optional[str] = None) -> Dict[str, Any]:
        """종합 KPI 요약."""
        today = await self.get_today_stats(tenant_id=tenant_id)
        channels = await self.get_sales_by_channel(tenant_id=tenant_id)
        products = await self.get_sales_by_product(tenant_id=tenant_id)

        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )
        all_products = await self.product_repo.list_async()
        all_channels = await self.channel_repo.list_async()

        total_sales = sum(o.sale_price * o.quantity for o in all_orders)
        total_profit = sum(o.profit for o in all_orders)

        return {
            "today": today,
            "overall": {
                "total_sales": total_sales,
                "total_orders": len(all_orders),
                "total_profit": total_profit,
                "avg_order_value": total_sales / len(all_orders) if all_orders else 0,
                "profit_rate": (total_profit / total_sales * 100)
                if total_sales > 0
                else 0,
            },
            "top_channels": channels[:5],
            "top_products": products[:5],
            "total_products": len(all_products),
            "total_channels": len(all_channels),
        }

    # ==================== Order Status Stats ====================

    async def get_order_status_stats(
        self, tenant_id: Optional[str] = None
    ) -> Dict[str, int]:
        """주문 상태별 건수."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )

        status_counts: Dict[str, int] = {
            "pending": 0,
            "shipped": 0,
            "delivered": 0,
            "cancelled": 0,
            "returned": 0,
        }

        for order in all_orders:
            status = order.status
            if status in status_counts:
                status_counts[status] += 1
            else:
                status_counts[status] = status_counts.get(status, 0) + 1

        return status_counts

    # ==================== Sourcing ROI ====================

    async def get_sourcing_roi(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """소싱처별 ROI 분석 — 원가, 매출, 이윤, 전환율."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )
        if start_date:
            all_orders = [o for o in all_orders if o.created_at >= start_date]
        if end_date:
            all_orders = [o for o in all_orders if o.created_at <= end_date]

        agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "total_cost": 0.0,
                "total_revenue": 0.0,
                "total_profit": 0.0,
                "order_count": 0,
            }
        )

        for order in all_orders:
            site = order.source_site or "미분류"
            agg[site]["source_site"] = site
            agg[site]["total_cost"] += order.cost * order.quantity
            agg[site]["total_revenue"] += order.sale_price * order.quantity
            agg[site]["total_profit"] += order.profit
            agg[site]["order_count"] += 1

        result = []
        for item in agg.values():
            cnt = item["order_count"]
            rev = item["total_revenue"]
            cost = item["total_cost"]
            item["avg_profit_per_order"] = (
                round(item["total_profit"] / cnt, 0) if cnt else 0
            )
            item["avg_margin_rate"] = round(
                (item["total_profit"] / rev * 100) if rev > 0 else 0, 1
            )
            item["roi"] = round(((rev - cost) / cost * 100) if cost > 0 else 0, 1)
            result.append(item)

        result.sort(key=lambda x: x["total_revenue"], reverse=True)
        return result

    # ==================== Best / Worst Sellers ====================

    async def get_best_sellers(
        self, limit: int = 10, days: int = 30, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """매출 상위 상품 (베스트셀러)."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )
        cutoff = datetime.now(UTC) - timedelta(days=days)
        filtered = [o for o in all_orders if o.created_at >= cutoff]

        agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"sales": 0.0, "profit": 0.0, "orders": 0, "units": 0}
        )

        for order in filtered:
            # 수집상품 ID 우선(마켓·계정 무관한 안정 ID) → 마켓 product_id → 상품명 폴백
            pid = (
                order.collected_product_id
                or order.product_id
                or f"name:{order.product_name or 'unknown'}"
            )
            agg[pid]["product_name"] = order.product_name or "기타"
            agg[pid]["source_site"] = order.source_site or ""
            agg[pid]["sales"] += order.sale_price * order.quantity
            agg[pid]["profit"] += order.profit
            agg[pid]["orders"] += 1
            agg[pid]["units"] += order.quantity

        result = list(agg.values())
        result.sort(key=lambda x: x["sales"], reverse=True)
        return result[:limit]

    async def get_worst_sellers(
        self, limit: int = 10, days: int = 30, tenant_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """이윤 최하위 상품 (워스트셀러)."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )
        cutoff = datetime.now(UTC) - timedelta(days=days)
        filtered = [o for o in all_orders if o.created_at >= cutoff]

        agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"sales": 0.0, "profit": 0.0, "orders": 0, "units": 0}
        )

        for order in filtered:
            # 수집상품 ID 우선(마켓·계정 무관한 안정 ID) → 마켓 product_id → 상품명 폴백
            pid = (
                order.collected_product_id
                or order.product_id
                or f"name:{order.product_name or 'unknown'}"
            )
            agg[pid]["product_name"] = order.product_name or "기타"
            agg[pid]["source_site"] = order.source_site or ""
            agg[pid]["sales"] += order.sale_price * order.quantity
            agg[pid]["profit"] += order.profit
            agg[pid]["orders"] += 1
            agg[pid]["units"] += order.quantity

        result = list(agg.values())
        result.sort(key=lambda x: x["profit"])
        return result[:limit]

    # ==================== Brand Analysis ====================

    async def get_sales_by_brand(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """브랜드별 매출 분석."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )
        if start_date:
            all_orders = [o for o in all_orders if o.created_at >= start_date]
        if end_date:
            all_orders = [o for o in all_orders if o.created_at <= end_date]

        # collected_product_id → brand 매핑 구축
        cp_ids = {o.collected_product_id for o in all_orders if o.collected_product_id}
        brand_map: Dict[str, str] = {}
        if cp_ids:
            from backend.domain.samba.collector.model import SambaCollectedProduct

            # product_repo가 SambaProduct이므로 collector repo 별도 조회
            try:
                from sqlmodel import select

                session = self.order_repo.session
                stmt = select(
                    SambaCollectedProduct.id, SambaCollectedProduct.brand
                ).where(SambaCollectedProduct.id.in_(list(cp_ids)[:500]))
                rows = (await session.execute(stmt)).all()
                for pid, brand in rows:
                    if brand:
                        brand_map[pid] = brand
            except Exception:
                pass

        agg: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {"sales": 0.0, "profit": 0.0, "orders": 0}
        )

        for order in all_orders:
            brand = brand_map.get(order.collected_product_id or "", "") or "미분류"
            agg[brand]["brand"] = brand
            agg[brand]["sales"] += order.sale_price * order.quantity
            agg[brand]["profit"] += order.profit
            agg[brand]["orders"] += 1

        result = []
        for item in agg.values():
            rev = item["sales"]
            item["avg_margin_rate"] = round(
                (item["profit"] / rev * 100) if rev > 0 else 0, 1
            )
            result.append(item)

        result.sort(key=lambda x: x["sales"], reverse=True)
        return result

    # ==================== Internal ====================

    async def _compute_stats_for_period(
        self, start: datetime, end: datetime, tenant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """기간 내 주문으로 통계 계산."""
        all_orders = self._filter_by_tenant(
            await self.order_repo.list_async(), tenant_id
        )

        filtered = [o for o in all_orders if start <= o.created_at <= end]

        total_sales = sum(o.sale_price * o.quantity for o in filtered)
        total_profit = sum(o.profit for o in filtered)
        total_orders = len(filtered)

        return {
            "total_sales": total_sales,
            "total_orders": total_orders,
            "total_profit": total_profit,
            "avg_order_value": total_sales / total_orders if total_orders > 0 else 0,
            "profit_rate": (total_profit / total_sales * 100) if total_sales > 0 else 0,
        }
