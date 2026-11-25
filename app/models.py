from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer

# Computed as Decimal, serialized as a JSON number rounded to 5 dp.
Money = Annotated[Decimal, PlainSerializer(lambda d: float(round(d, 5)), return_type=float, when_used="json")]

ErrorCode = Literal["not_configured", "not_connected", "rate_limited", "quota_exhausted", "auth_failed",
                    "upstream_error", "timeout"]


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


class Spec(BaseModel):
    name: str | None
    value: str | None


class ProductSpecs(BaseModel):
    mpn: str | None
    manufacturer: str | None = None
    description: str | None = None
    skus: list[str] = []
    datasheet_url: str | None = None
    product_url: str | None = None
    match: Literal["exact", "loose"] = "exact"
    category: str | None = None  # DigiKey leaf category: what search_by_specs takes
    category_id: int | None = None
    specs: list[Spec] = []


class PartRef(BaseModel):
    """A substitute or related product as DigiKey reports it (not priced at a quantity)."""
    kind: str | None = None  # substitute type, "alternate_packaging", "mating", "for_use_with", "associated", "kit"
    mpn: str | None = None
    manufacturer: str | None = None
    sku: str | None = None
    description: str | None = None
    stock: int | None = None
    unit_price: Money | None = None
    product_url: str | None = None


class CartItemIn(BaseModel):
    provider: Literal["mouser", "digikey"]
    sku: str = Field(min_length=1, max_length=100)
    qty: int = Field(ge=1, le=10_000_000)
    customer_ref: str | None = Field(None, max_length=100)


class CartLine(BaseModel):
    sku: str | None = None
    mpn: str | None = None
    manufacturer: str | None = None
    description: str | None = None
    qty: int | None = None
    unit_price: Money | None = None
    extended_price: Money | None = None
    customer_ref: str | None = None
    warnings: list[str] = []


class Cart(BaseModel):
    provider: str
    cart_id: str | None = None  # Mouser cart key / DigiKey list id
    name: str | None = None
    currency: str | None = None
    lines: list[CartLine] = []
    total: Money | None = None
    warnings: list[str] = []
