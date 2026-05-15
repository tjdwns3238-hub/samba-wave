"""SambaWave Order DTOs."""

from typing import Optional

from pydantic import BaseModel


class OrderCreate(BaseModel):
    channel_id: Optional[str] = None
    channel_name: Optional[str] = None
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    product_image: Optional[str] = None
    source_site: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None
    customer_address_detail: Optional[str] = None
    customer_postal_code: Optional[str] = None
    quantity: int = 1
    sale_price: float = 0
    cost: float = 0
    shipping_fee: float = 0
    fee_rate: float = 0
    customer_note: Optional[str] = None
    notes: Optional[str] = None
    source: Optional[str] = None
    shipment_id: Optional[str] = None


class OrderUpdate(BaseModel):
    order_number: Optional[str] = None
    ext_order_number: Optional[str] = None
    channel_name: Optional[str] = None
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    product_image: Optional[str] = None
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None
    customer_address_detail: Optional[str] = None
    customer_postal_code: Optional[str] = None
    quantity: Optional[int] = None
    sale_price: Optional[float] = None
    cost: Optional[float] = None
    shipping_fee: Optional[float] = None
    fee_rate: Optional[float] = None
    shipping_company: Optional[str] = None
    tracking_number: Optional[str] = None
    customer_note: Optional[str] = None
    notes: Optional[str] = None
    sourcing_order_number: Optional[str] = None
    sourcing_account_id: Optional[str] = None
    source_url: Optional[str] = None
    source_site: Optional[str] = None
    coupang_display_name: Optional[str] = None
    action_tag: Optional[str] = None


class OrderStatusUpdate(BaseModel):
    status: str


class FetchProductImageRequest(BaseModel):
    url: str
