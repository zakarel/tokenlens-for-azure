from datetime import UTC, date, datetime

from tokenlens.pricing_sources.imports import (
    ImportedPriceRow,
    actual_cost_provider,
    contract_price_provider,
    summarize_actual_cost,
)
from tokenlens.pricing_sources.providers import BillingComponent, DeploymentIdentity


IDENTITY = DeploymentIdentity(provider="azure", model="gpt-4o", processing_scope="global")
WHEN = datetime(2026, 10, 6, tzinfo=UTC)


def row(record_id: str, price: float = 2.0, multiplier: float = 1) -> ImportedPriceRow:
    return ImportedPriceRow(
        model="gpt-4o",
        processing_scope="global",
        component="input_tokens",
        unit="1K tokens",
        unit_price=price,
        unit_multiplier=multiplier,
        source_record_id=record_id,
        meter_id="meter-1",
        effective_from=date(2026, 1, 1),
        source_scope="billing-account",
    )


def test_actual_cost_import_keeps_actual_track_and_provenance():
    result = actual_cost_provider([row("cost-row")]).resolve(
        IDENTITY,
        BillingComponent(name="input_tokens", quantity=3, unit="1K tokens"),
        WHEN,
    )
    assert result.state == "EXACT_ACTUAL"
    assert result.amount == 6
    assert result.source_record_id == "cost-row"


def test_duplicate_contract_rows_are_ambiguous_not_arbitrarily_selected():
    result = contract_price_provider([row("contract-a"), row("contract-b")]).resolve(
        IDENTITY,
        BillingComponent(name="input_tokens", quantity=3, unit="1K tokens"),
        WHEN,
    )
    assert result.state == "AMBIGUOUS"
    assert result.resolved is False


def test_imported_rate_applies_explicit_unit_multiplier():
    result = actual_cost_provider([row("cost-row", price=2.0, multiplier=1_000)]).resolve(
        IDENTITY,
        BillingComponent(name="input_tokens", quantity=3_000, unit="tokens"),
        WHEN,
    )
    assert result.amount == 6


def test_actual_cost_summary_keeps_unattributed_rows_visible():
    unmatched = row("other", price=3.0).model_copy(update={"model": "other-model", "quantity": 4})
    summary = summarize_actual_cost(
        [row("matched", price=2.0).model_copy(update={"quantity": 3}), unmatched],
        known_models={"gpt-4o"},
    )
    assert summary.matched_rows == 1
    assert summary.unattributed_rows == 1
    assert summary.total_actual_cost == 6
