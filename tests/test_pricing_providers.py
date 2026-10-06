from datetime import UTC, datetime

from tokenlens.pricing_sources.providers import (
    BillingComponent,
    DeploymentIdentity,
    LayeredPricingResolver,
    StaticPricingProvider,
)


WHEN = datetime(2026, 10, 6, tzinfo=UTC)
IDENTITY = DeploymentIdentity(
    provider="azure",
    model="gpt-4o",
    processing_scope="global",
)


def provider(name: str, state: str, price: float, currency: str = "USD"):
    return StaticPricingProvider(
        name=name,
        state=state,
        currency=currency,
        rates={("gpt-4o", "global", "input_tokens"): (price, f"{name}-row", f"{name}-meter")},
    )


def test_layered_resolution_preserves_provider_precedence_and_provenance():
    result = LayeredPricingResolver([
        provider("override", "EXACT_OVERRIDE", 1.0),
        provider("retail", "EXACT_PUBLIC_RETAIL", 2.0),
    ]).resolve(
        IDENTITY,
        BillingComponent(name="input_tokens", quantity=10, unit="tokens"),
        WHEN,
    )
    assert result.state == "EXACT_OVERRIDE"
    assert result.amount == 10
    assert result.source_record_id == "override-row"
    assert result.meter_id == "override-meter"


def test_verified_rate_without_usage_is_not_reported_as_exact_cost():
    result = provider("contract", "EXACT_CONTRACT", 1.0).resolve(
        IDENTITY,
        BillingComponent(name="input_tokens", quantity=None, unit="tokens"),
        WHEN,
    )
    assert result.resolved is False
    assert result.reason == "verified rate but usage quantity is unavailable"


def test_currency_mismatch_does_not_silently_convert():
    result = LayeredPricingResolver([provider("eur", "EXACT_CONTRACT", 1.0, "EUR")]).resolve(
        IDENTITY,
        BillingComponent(name="input_tokens", quantity=10, unit="tokens", currency="USD"),
        WHEN,
    )
    assert result.resolved is False
    assert result.state == "NO_VERIFIED_PRICE"
