"""Offline tiered pricing with explicit provenance."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .events import ModelCallEvent, ToolStepEvent


class PriceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    service_tier: str = "standard"
    region: str | None = None
    effective_from: date
    effective_to: date | None = None
    context_length_min: int | None = Field(default=None, ge=0)
    context_length_max: int | None = Field(default=None, ge=0)
    input_per_million: float = Field(ge=0)
    cached_input_per_million: float = Field(default=0, ge=0)
    cache_write_per_million: float = Field(default=0, ge=0)
    output_per_million: float = Field(ge=0)

    def applies_to(self, event: ModelCallEvent, when: datetime) -> bool:
        current = when.date()
        if self.provider.casefold() != event.provider.casefold():
            return False
        if self.model.casefold() != event.model_name.casefold():
            return False
        if self.service_tier.casefold() != event.service_tier.casefold():
            return False
        if self.region is not None and (event.region or "").casefold() != self.region.casefold():
            return False
        if current < self.effective_from or (self.effective_to and current > self.effective_to):
            return False
        if self.context_length_min is not None and (event.context_length or 0) < self.context_length_min:
            return False
        if self.context_length_max is not None and (event.context_length or 0) > self.context_length_max:
            return False
        return True


class PricingCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    catalog_name: str
    source_url: str | None = None
    retrieved_at: date | None = None
    prices: list[PriceEntry] = Field(default_factory=list)

    def resolve(self, event: ModelCallEvent) -> PriceEntry | None:
        candidates = [price for price in self.prices if price.applies_to(event, event.timestamp)]
        if not candidates:
            return None
        # Prefer the narrowest context band, then the most recent effective date.
        return sorted(
            candidates,
            key=lambda price: (
                0 if price.context_length_min is not None or price.context_length_max is not None else 1,
                -(price.effective_from.toordinal()),
            ),
        )[0]


class PricingResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved: bool
    cost_usd: float | None = None
    source: Literal["observed", "customer", "reference", "unresolved"]
    catalog_name: str | None = None
    effective_from: date | None = None
    currency: str = "USD"


def calculated_model_cost(event: ModelCallEvent, price: PriceEntry) -> float:
    usage = event.usage
    fresh = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens
    return (
        fresh * price.input_per_million
        + usage.cached_input_tokens * price.cached_input_per_million
        + usage.cache_write_tokens * price.cache_write_per_million
        + usage.output_tokens * price.output_per_million
    ) / 1_000_000


def resolve_event_cost(
    event: ModelCallEvent | ToolStepEvent,
    *,
    customer_catalog: PricingCatalog | None = None,
    reference_catalog: PricingCatalog | None = None,
) -> PricingResolution:
    if event.observed_cost_usd is not None:
        return PricingResolution(
            resolved=True,
            cost_usd=event.observed_cost_usd,
            source="observed",
            currency="USD",
        )
    if isinstance(event, ToolStepEvent):
        return PricingResolution(resolved=False, source="unresolved", currency="USD")
    for catalog, source in ((customer_catalog, "customer"), (reference_catalog, "reference")):
        if catalog is None:
            continue
        price = catalog.resolve(event)
        if price is not None:
            return PricingResolution(
                resolved=True,
                cost_usd=calculated_model_cost(event, price),
                source=source,  # type: ignore[arg-type]
                catalog_name=catalog.catalog_name,
                effective_from=price.effective_from,
                currency=catalog.currency,
            )
    return PricingResolution(resolved=False, source="unresolved", currency="USD")


def catalog_from_dict(value: dict[str, object], *, default_name: str) -> PricingCatalog:
    """Load a YAML/JSON-shaped catalog while keeping its provenance explicit."""
    payload = dict(value)
    payload.setdefault("catalog_name", default_name)
    return PricingCatalog.model_validate(payload)


PriceCatalog = PricingCatalog
resolve_cost = resolve_event_cost
