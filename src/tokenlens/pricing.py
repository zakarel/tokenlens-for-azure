"""Offline tiered pricing with explicit provenance."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
import re
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .aggregate import telemetry_kind
from .events import ModelCallEvent, ToolStepEvent
from .models import TraceRecord, Usage


def canonical_model_name(value: str) -> str:
    """Normalize harmless naming variants without using family fallbacks."""
    normalized = re.sub(r"[\s_]+", "-", value.strip().casefold())
    normalized = re.sub(r"-+", "-", normalized)
    return normalized


BillingBasis = Literal[
    "token_rate",
    "claude_ccu_equivalent",
    "marketplace_partner_token_rate",
    "observed_cost",
]

# Best-effort publisher inference for canonical model names TokenLens has not
# been given an explicit catalog entry for. This never changes pricing
# resolution (no family fallback); it only classifies which purchasing
# programs (for example Azure PTU) plausibly apply to the underlying model.
_PUBLISHER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("claude-", "anthropic"),
    ("ministral-", "mistral"),
    ("mistral-", "mistral"),
    ("llama-", "meta"),
    ("meta-llama", "meta"),
    ("deepseek-", "deepseek"),
    ("gemini-", "google"),
    ("gemma-", "google"),
    ("cohere-", "cohere"),
    ("command-", "cohere"),
    ("gpt-", "microsoft"),
    ("o1", "microsoft"),
    ("o3-", "microsoft"),
    ("o4-", "microsoft"),
    ("phi-", "microsoft"),
    ("mai-", "microsoft"),
)


def infer_publisher(model_name: str) -> str | None:
    """Infer a model's publisher from its canonical name for display and PTU eligibility only."""
    canonical = canonical_model_name(model_name)
    for prefix, publisher in _PUBLISHER_PREFIXES:
        if canonical.startswith(prefix):
            return publisher
    return None


class PriceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    publisher: str = ""
    model: str
    aliases: list[str] = Field(default_factory=list)
    service_tier: str = "standard"
    region: str | None = None
    effective_from: date
    effective_to: date | None = None
    context_length_min: int | None = Field(default=None, ge=0)
    context_length_max: int | None = Field(default=None, ge=0)
    billing_basis: BillingBasis = "token_rate"
    confidence: Literal["verified", "customer_override"] = "verified"
    source_url: str | None = None
    note: str | None = None
    #: Lower wins. A synchronized official rate (10) is preferred over the
    #: packaged dated snapshot (100) for the same model and mode.
    source_rank: int = 100
    #: Which pricing dimensions came from a documented default rather than from
    #: the source row itself, for example ``deployment=global``.
    assumed_dimensions: list[str] = Field(default_factory=list)
    context_window: Literal["short", "long"] | None = None
    #: Dimension name -> source meter identifier, for exact provenance.
    meter_ids: dict[str, str] = Field(default_factory=dict)
    content_hash: str | None = None
    retrieved_at: datetime | None = None
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
    #: The default pricing dimensions this catalog was built under, when it was
    #: produced by a synchronization rather than hand-curated.
    assumptions: dict[str, str] | None = None
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
        # A literal model is always safer than an alias that happens to overlap,
        # and a synchronized official rate outranks the packaged snapshot.
        return sorted(
            candidates,
            key=lambda price: (
                price.model_match_rank(event.model_name),
                price.source_rank,
                0 if price.context_length_min is not None or price.context_length_max is not None else 1,
                -(price.effective_from.toordinal()),
            ),
        )[0]

    def assumed_dimensions(self) -> list[str]:
        """Every default that was actually applied to a priced entry."""
        return sorted({item for price in self.prices for item in price.assumed_dimensions})


class PricingResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved: bool
    cost_usd: float | None = None
    source: Literal["observed", "customer", "reference", "unresolved"]
    catalog_name: str | None = None
    effective_from: date | None = None
    currency: str = "USD"
    pricing_basis: Literal["effective_period", "analysis_date_snapshot"] | None = None
    billing_basis: BillingBasis | None = None
    publisher: str | None = None
    confidence: Literal["verified", "customer_override", "observed"] | None = None
    note: str | None = None
    input_per_million: float | None = None
    cached_input_per_million: float | None = None
    output_per_million: float | None = None
    fresh_input_cost_usd: float | None = None
    cached_input_cost_usd: float | None = None
    output_cost_usd: float | None = None
    assumed_dimensions: list[str] = Field(default_factory=list)
    meter_ids: dict[str, str] = Field(default_factory=dict)
    content_hash: str | None = None
    source_url: str | None = None
    unresolved_reason: str | None = None
    suggested_override_key: str | None = None


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


def context_assumptions(price: PriceEntry, observed_context_length: int | None) -> list[str]:
    """Name the context default when the record could not state its own.

    A context-scoped entry chosen because the prompt size was unavailable is an
    assumption and is reported as one. A context-scoped entry chosen because the
    observed prompt size fell inside its documented band is exact, and adds
    nothing.
    """
    if price.context_window is None or observed_context_length is not None:
        return []
    return [f"context={price.context_window}"]


def _resolved_catalog_cost(
    *,
    usage: Usage | object,
    price: PriceEntry,
    source: Literal["customer", "reference"],
    catalog: PricingCatalog,
    extra_assumptions: Sequence[str] = (),
) -> PricingResolution:
    calculated = _calculated_usage_cost(usage, price)
    if calculated is None:
        return PricingResolution(
            resolved=False,
            source="unresolved",
            currency=catalog.currency,
            catalog_name=catalog.catalog_name,
            billing_basis=price.billing_basis,
            publisher=price.publisher or None,
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
        billing_basis=price.billing_basis,
        publisher=price.publisher or None,
        confidence=price.confidence,
        note=price.note,
        input_per_million=price.input_per_million,
        cached_input_per_million=price.cached_input_per_million,
        output_per_million=price.output_per_million,
        fresh_input_cost_usd=fresh,
        cached_input_cost_usd=cached,
        output_cost_usd=output,
        assumed_dimensions=sorted({*price.assumed_dimensions, *extra_assumptions}),
        meter_ids=dict(price.meter_ids),
        content_hash=price.content_hash,
        source_url=price.source_url or catalog.source_url,
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
            billing_basis="observed_cost",
            confidence="observed",
            publisher=infer_publisher(event.model_name) if isinstance(event, ModelCallEvent) else None,
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
                extra_assumptions=context_assumptions(price, event.context_length),
            )
    return PricingResolution(
        resolved=False,
        source="unresolved",
        currency="USD",
        publisher=infer_publisher(event.model_name),
        unresolved_reason="no-exact-model-mode-price",
        suggested_override_key=canonical_model_name(event.model_name),
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
            billing_basis="observed_cost",
            confidence="observed",
            publisher=infer_publisher(record.model_name),
        )

    # An aggregate bucket's ``input_tokens`` is an interval total, not one
    # prompt, so it is never used as a context length. Without a per-request
    # measure the documented default context applies, and says so.
    observed_context_length = (
        record.usage.input_tokens if telemetry_kind(record) == "request" else None
    )

    class RequestPriceTarget:
        provider = record.provider
        model_name = record.model_name
        service_tier = record.service_tier
        region = record.deployment_mode
        context_length = observed_context_length

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
                extra_assumptions=context_assumptions(price, observed_context_length),
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
        publisher=infer_publisher(record.model_name),
        unresolved_reason=reason,
        suggested_override_key=canonical_model_name(record.model_name),
    )


def catalog_from_dict(value: dict[str, object], *, default_name: str) -> PricingCatalog:
    """Load a YAML/JSON-shaped catalog while keeping its provenance explicit."""
    payload = dict(value)
    payload.setdefault("catalog_name", default_name)
    return PricingCatalog.model_validate(payload)


PriceCatalog = PricingCatalog
resolve_cost = resolve_event_cost
