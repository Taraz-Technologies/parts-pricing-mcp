from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, PlainSerializer

# Computed as Decimal, serialized as a JSON number rounded to 5 dp.
Money = Annotated[Decimal, PlainSerializer(lambda d: float(round(d, 5)), return_type=float, when_used="json")]

ErrorCode = Literal["not_configured", "rate_limited", "quota_exhausted", "auth_failed", "upstream_error", "timeout"]


class PriceBreak(BaseModel):
    qty: int
    unit_price: Money


class Offer(BaseModel):
    provider: str
    mpn: str
    manufacturer: str | None = None
    sku: str | None = None
    description: str | None = None
    packaging: str | None = None
    marketplace: bool = False
    stock: int | None = None
    on_order: int | None = None
    moq: int | None = None
    multiple: int | None = None
    price_breaks: list[PriceBreak] = []
    currency: str | None = None
    extra_fee: Money | None = None
    lead_time_weeks: int | None = None
    lifecycle: str | None = None
    rohs: str | None = None
    datasheet_url: str | None = None
    product_url: str | None = None
    image_url: str | None = None
    match: Literal["exact", "loose"] = "exact"
    fetched_at: datetime
    from_cache: bool = False
    warnings: list[str] = []


class BetterBuy(BaseModel):
    qty: int
    unit_price: Money
    extended_price: Money


class PricedOffer(Offer):
    requested_qty: int
    order_qty: int | None = None
    unit_price: Money | None = None
    extended_price: Money | None = None
    in_stock: bool | None = None
    better_buy: BetterBuy | None = None
    excluded_from_best: str | None = None  # marketplace|manufacturer_mismatch|loose_match|no_price


class ProviderStatus(BaseModel):
    provider: str
    ok: bool = True
    found: bool = False
    from_cache: bool = False
    error_code: ErrorCode | None = None
    message: str | None = None
    latency_ms: int | None = None
    quota_remaining_today: int | None = None


class ProviderLookup(BaseModel):
    """Internal: one provider's answer for one MPN."""
    found: bool
    offers: list[Offer] = []
    status: ProviderStatus
    warnings: list[str] = []


class PartQuery(BaseModel):
    mpn: str
    qty: int
    manufacturer: str | None = None


class PartResult(BaseModel):
    line_id: str | None = None
    query: PartQuery
    offers: list[PricedOffer] = []
    best_by_provider: dict[str, PricedOffer | None] = {}
    best: PricedOffer | None = None
    best_reason: str | None = None  # cheapest_in_stock|no_stock_anywhere
    provider_status: list[ProviderStatus] = []
    warnings: list[str] = []


class BomResult(BaseModel):
    lines: list[PartResult]
    summary: dict
    provider_status: list[ProviderStatus]
    quota: dict
