"""Offline tiered pricing with explicit provenance."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .events import ModelCallEvent, ToolStepEvent
from .models import TraceRecord, Usage


def canonical_model_name(value: str) -> str:
    """Normalize harmless naming variants without using family fallbacks."""
    normalized = re.sub(r"[\s_]+", "-", value.strip().casefold())
    normalized = re.sub(r"-+", "-", normalized)
    return normalized


class PriceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    aliases: list[str] = Field(default_factory=list)
    service_tier: str = "standard"
    region: str | None = None
    effective_from: date
    effective_to: date | None = None
    context_length_min: int | None = Field(default=None, ge=0)
    context_length_max: int | None = Field(default=None, ge=0)
    input_per_million: float = Field(ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)
    cache_write_per_million: float = Field(default=0, ge=0)
    output_per_million: float = Field(ge=0)

    def model_match_rank(self, model_name: str) -> int | None:
        candidate = canonical_model_name(model_name)
        if candidate == canonical_model_name(self.model):
            return 0
        if candidate in {canonical_model_name(alias) for alias in self.aliases}:
            return 1
        return None

    def applies_to(self, event: ModelCallEvent, when: datetime) -> bool:
        current = when.date()
        if self.provider.casefold() != event.provider.casefold():
            return False
        if self.model_match_rank(event.model_name) is None:
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
    pricing_basis: Literal["effective_period", "analysis_date_snapshot"] = "effective_period"
    prices: list[PriceEntry] = Field(default_factory=list)

    def resolve(self, event: ModelCallEvent) -> PriceEntry | None:
        return self.resolve_for(event, event.timestamp)

    def resolve_for(self, event: object, when: datetime) -> PriceEntry | None:
        pricing_when = when
        if self.pricing_basis == "analysis_date_snapshot" and self.retrieved_at is not None:
            pricing_when = datetime.combine(self.retrieved_at, time.min, tzinfo=UTC)
        candidates = [price for price in self.prices if price.applies_to(event, pricing_when)]
        if not candidates:
            return None
        # A literal model is always safer than an alias that happens to overlap.
        return sorted(
            candidates,
            key=lambda price: (
                price.model_match_rank(event.model_name),
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
    pricing_basis: Literal["effective_period", "analysis_date_snapshot"] | None = None
    input_per_million: float | None = None
    cached_input_per_million: float | None = None
    output_per_million: float | None = None
    fresh_input_cost_usd: float | None = None
    cached_input_cost_usd: float | None = None
    output_cost_usd: float | None = None
    unresolved_reason: str | None = None


def _calculated_usage_cost(usage: Usage | object, price: PriceEntry) -> tuple[float, float, float, float] | None:
    fresh = usage.input_tokens - usage.cached_input_tokens - usage.cache_write_tokens
    if usage.cached_input_tokens and price.cached_input_per_million is None:
        return None
    cached_rate = price.cached_input_per_million or 0
    fresh_cost = (fresh * price.input_per_million + usage.cache_write_tokens * price.cache_write_per_million) / 1_000_000
    cached_cost = usage.cached_input_tokens * cached_rate / 1_000_000
    output_cost = usage.output_tokens * price.output_per_million / 1_000_000
    return fresh_cost + cached_cost + output_cost, fresh_cost, cached_cost, output_cost


def calculated_model_cost(event: ModelCallEvent, price: PriceEntry) -> float:
    calculated = _calculated_usage_cost(event.usage, price)
    if calculated is None:
        raise ValueError("Cached input was observed but the selected price has no cached-input rate")
    return calculated[0]


def _resolved_catalog_cost(
    *,
    usage: Usage | object,
    price: PriceEntry,
    source: Literal["customer", "reference"],
    catalog: PricingCatalog,
) -> PricingResolution:
    calculated = _calculated_usage_cost(usage, price)
    if calculated is None:
        return PricingResolution(
            resolved=False,
            source="unresolved",
            currency=catalog.currency,
            catalog_name=catalog.catalog_name,
            unresolved_reason="cached-input-rate-unavailable",
        )
    total, fresh, cached, output = calculated
    return PricingResolution(
        resolved=True,
        cost_usd=total,
        source=source,
        catalog_name=catalog.catalog_name,
        effective_from=price.effective_from,
        currency=catalog.currency,
        pricing_basis=catalog.pricing_basis,
        input_per_million=price.input_per_million,
        cached_input_per_million=price.cached_input_per_million,
        output_per_million=price.output_per_million,
        fresh_input_cost_usd=fresh,
        cached_input_cost_usd=cached,
        output_cost_usd=output,
    )


def resolve_event_cost(
    event: ModelCallEvent | ToolStepEvent,
    *,
    customer_catalog: PricingCatalog | None = None,
    reference_catalog: PricingCatalog | None = None,
    required_currency: str | None = None,
) -> PricingResolution:
    if event.observed_cost_usd is not None:
        if required_currency and required_currency.casefold() != "usd":
            return PricingResolution(
                resolved=False,
                source="unresolved",
                currency=required_currency,
                unresolved_reason="currency-conversion-required",
            )
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
        if required_currency and catalog.currency.casefold() != required_currency.casefold():
            continue
        price = catalog.resolve(event)
        if price is not None:
            return _resolved_catalog_cost(
                usage=event.usage,
                price=price,
                source=source,  # type: ignore[arg-type]
                catalog=catalog,
            )
    return PricingResolution(
        resolved=False,
        source="unresolved",
        currency="USD",
        unresolved_reason="no-exact-model-mode-price",
    )


def resolve_trace_cost(
    record: TraceRecord,
    *,
    when: datetime,
    customer_catalog: PricingCatalog | None = None,
    reference_catalog: PricingCatalog | None = None,
    required_currency: str | None = None,
) -> PricingResolution:
    """Resolve legacy request telemetry with the same precedence as task events."""
    if record.observed_cost_usd is not None:
        if required_currency and required_currency.casefold() != "usd":
            return PricingResolution(
                resolved=False,
                source="unresolved",
                currency=required_currency,
                unresolved_reason="currency-conversion-required",
            )
        return PricingResolution(
            resolved=True,
            cost_usd=record.observed_cost_usd,
            source="observed",
            currency="USD",
        )

    class RequestPriceTarget:
        provider = record.provider
        model_name = record.model_name
        service_tier = record.service_tier
        region = record.deployment_mode
        context_length = record.usage.input_tokens

    for catalog, source in ((customer_catalog, "customer"), (reference_catalog, "reference")):
        if catalog is None:
            continue
        if required_currency and catalog.currency.casefold() != required_currency.casefold():
            continue
        price = catalog.resolve_for(RequestPriceTarget(), when)
        if price is not None:
            class RequestUsage:
                input_tokens = record.usage.input_tokens
                cached_input_tokens = record.usage.cached_tokens
                cache_write_tokens = 0
                output_tokens = record.usage.output_tokens

            return _resolved_catalog_cost(
                usage=RequestUsage(),
                price=price,
                source=source,  # type: ignore[arg-type]
                catalog=catalog,
            )
    available_currencies = {
        catalog.currency.upper()
        for catalog in (customer_catalog, reference_catalog)
        if catalog is not None and catalog.resolve_for(RequestPriceTarget(), when) is not None
    }
    reason = (
        "currency-conversion-required"
        if required_currency and any(currency.casefold() != required_currency.casefold() for currency in available_currencies)
        else "no-exact-model-mode-price"
    )
    return PricingResolution(
        resolved=False,
        source="unresolved",
        currency=required_currency or "USD",
        unresolved_reason=reason,
    )


def catalog_from_dict(value: dict[str, object], *, default_name: str) -> PricingCatalog:
    """Load a YAML/JSON-shaped catalog while keeping its provenance explicit."""
    payload = dict(value)
    payload.setdefault("catalog_name", default_name)
    return PricingCatalog.model_validate(payload)


PriceCatalog = PricingCatalog
resolve_cost = resolve_event_cost
