"""SambaWave Order service."""

from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from backend.domain.samba.order.model import SambaOrder
from backend.domain.samba.order.repository import SambaOrderRepository


class SambaOrderService:
    def __init__(self, repo: SambaOrderRepository):
        self.repo = repo

    @staticmethod
    def _compute_financials(
        *,
        sale_price: float,
        total_payment_amount: float | None = None,
        cost: float,
        shipping_fee: float,
        fee_rate: float,
        revenue: float | None = None,
    ) -> Dict[str, Any]:
        resolved_revenue = (
            float(revenue)
            if revenue is not None
            else float(sale_price) * (1 - float(fee_rate) / 100)
        )
        profit = resolved_revenue - float(cost) - float(shipping_fee)
        payment_amount = (
            float(total_payment_amount)
            if total_payment_amount is not None
            else float(sale_price)
        )
        profit_rate = (
            f"{(profit / payment_amount * 100):.2f}" if payment_amount > 0 else "0.00"
        )
        return {
            "revenue": resolved_revenue,
            "profit": profit,
            "profit_rate": profit_rate,
        }

    async def list_orders(
        self, skip: int = 0, limit: int = 50, status: Optional[str] = None
    ) -> List[SambaOrder]:
        if status:
            return await self.repo.list_by_status(status)
        return await self.repo.list_async(
            skip=skip, limit=limit, order_by="-created_at"
        )

    async def get_order(self, order_id: str) -> Optional[SambaOrder]:
        return await self.repo.get_async(order_id)

    async def create_order(
        self, data: Dict[str, Any], commit: bool = True
    ) -> SambaOrder:
        sale_price = float(data.get("sale_price", 0))
        total_payment_amount = (
            float(data["total_payment_amount"])
            if data.get("total_payment_amount") is not None
            else None
        )
        cost = float(data.get("cost", 0))
        shipping_fee = float(data.get("shipping_fee", 0))
        fee_rate = float(data.get("fee_rate", 0))
        revenue = float(data["revenue"]) if data.get("revenue") is not None else None
        data.update(
            self._compute_financials(
                sale_price=sale_price,
                total_payment_amount=total_payment_amount,
                cost=cost,
                shipping_fee=shipping_fee,
                fee_rate=fee_rate,
                revenue=revenue,
            )
        )

        return await self.repo.create_async(commit=commit, **data)

    async def update_order(
        self, order_id: str, data: Dict[str, Any], commit: bool = True
    ) -> Optional[SambaOrder]:
        order = await self.repo.get_async(order_id)
        if not order:
            return None

        financial_keys = {
            "sale_price",
            "total_payment_amount",
            "cost",
            "shipping_fee",
            "fee_rate",
            "revenue",
        }
        if financial_keys.intersection(data):
            sale_price = float(data.get("sale_price", order.sale_price) or 0)
            total_payment_amount = (
                float(data["total_payment_amount"])
                if data.get("total_payment_amount") is not None
                else (
                    float(order.total_payment_amount)
                    if order.total_payment_amount is not None
                    else None
                )
            )
            cost = float(data.get("cost", order.cost) or 0)
            shipping_fee = float(data.get("shipping_fee", order.shipping_fee) or 0)
            fee_rate = float(data.get("fee_rate", order.fee_rate) or 0)
            revenue = (
                float(data["revenue"])
                if data.get("revenue") is not None
                else (
                    None
                    if ("sale_price" in data or "fee_rate" in data)
                    else float(order.revenue or 0)
                )
            )
            data.update(
                self._compute_financials(
                    sale_price=sale_price,
                    total_payment_amount=total_payment_amount,
                    cost=cost,
                    shipping_fee=shipping_fee,
                    fee_rate=fee_rate,
                    revenue=revenue,
                )
            )
        return await self.repo.update_async(order_id, commit=commit, **data)

    async def update_order_status(
        self, order_id: str, new_status: str
    ) -> Optional[SambaOrder]:
        updates: Dict[str, Any] = {"status": new_status}
        now = datetime.now(UTC)

        if new_status == "shipped":
            updates["shipped_at"] = now
            updates["shipping_status"] = "배송중"
        elif new_status == "delivered":
            updates["delivered_at"] = now
            updates["shipping_status"] = "배송완료"
        elif new_status == "confirmed":
            updates["shipping_status"] = "구매확정"

        return await self.repo.update_async(order_id, **updates)

    async def delete_order(self, order_id: str) -> bool:
        return await self.repo.delete_async(order_id)

    async def search_orders(self, query: str) -> List[SambaOrder]:
        return await self.repo.search(query)
