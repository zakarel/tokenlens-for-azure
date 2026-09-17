"""Coverage-aware multi-source pricing: billing basis, provenance, and
partial/zero/full coverage rendering.

Model IDs used here are clearly synthetic test fixtures (``-test-synthetic``
suffixes); they are not copied from any real trace.
"""

import io
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tokenlens.analyzer import analyze
from tokenlens.cli import app
from tokenlens.ingest import iter_records
from tokenlens.output import pricing_audit_text
from tokenlens.pricing import PriceEntry, PricingCatalog, infer_publisher
from tokenlens.presentation import model_rollups
from tokenlens.reports import report_html

MICROSOFT_MODEL = "contoso-frontier-1-test-synthetic"
CLAUDE_LIKE_MODEL = "claude-3-test-synthetic"
MINISTRAL_LIKE_MODEL = "ministral-8b-test-synthetic"


def parsed(value):
    return next(iter_records(io.StringIO(json.dumps(value) + "\n")))


def trace(*, deployment, model, input_tokens=40, output_tokens=20, timestamp="2026-09-14T12:00:00Z"):
    return parsed(
        {
            "timestamp": timestamp,
            "deployment_name": deployment,
            "model_name": model,
            "provider": "azure_foundry",
            "deployment_mode": "global",
            "messages": [],
            "usage": {"input_tokens": input_tokens, "cached_tokens": 0, "output_tokens": output_tokens},
            "latency_ms": 500,
            "status_code": 200,
        }
    )


def three_model_records():
    return [
        trace(deployment="dep-microsoft", model=MICROSOFT_MODEL, input_tokens=20, output_tokens=6),
        trace(deployment="dep-claude", model=CLAUDE_LIKE_MODEL, input_tokens=40, output_tokens=15),
        trace(deployment="dep-ministral", model=MINISTRAL_LIKE_MODEL, input_tokens=16, output_tokens=6),
    ]


def full_customer_catalog():
    return PricingCatalog(
        catalog_name="synthetic-customer-overrides",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                publisher="microsoft",
                model=MICROSOFT_MODEL,
                region="global",
                effective_from="2026-01-01",
                billing_basis="token_rate",
                confidence="customer_override",
                input_per_million=2.0,
                output_per_million=8.0,
            ),
            PriceEntry(
                provider="azure_foundry",
                publisher="anthropic",
                model=CLAUDE_LIKE_MODEL,
                region="global",
                effective_from="2026-01-01",
                billing_basis="claude_ccu_equivalent",
                confidence="customer_override",
                note="CCU-derived dollar-equivalent estimate.",
                input_per_million=5.0,
                output_per_million=20.0,
            ),
            PriceEntry(
                provider="azure_foundry",
                publisher="mistral",
                model=MINISTRAL_LIKE_MODEL,
                region="global",
                effective_from="2026-01-01",
                billing_basis="marketplace_partner_token_rate",
                confidence="customer_override",
                input_per_million=0.04,
                output_per_million=0.04,
            ),
        ],
    )


def test_infer_publisher_matches_expected_families():
    assert infer_publisher(CLAUDE_LIKE_MODEL) == "anthropic"
    assert infer_publisher(MINISTRAL_LIKE_MODEL) == "mistral"
    assert infer_publisher(MICROSOFT_MODEL) is None  # unbranded synthetic prefix; no guess is made up


def test_three_model_customer_catalog_yields_full_coverage_and_billing_basis():
    report = analyze(
        three_model_records(),
        "fixture",
        customer_catalog=full_customer_catalog(),
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    assert report.summary.pricing_coverage_requests_percent == 100.0
    assert report.summary.pricing_coverage_tokens_percent == 100.0
    assert report.summary.estimated_cost_usd is not None
    assert report.summary.unresolved_requests == 0
    assert report.summary.unresolved_tokens == 0

    rollups = {item.model_name: item for item in model_rollups(report)}
    assert rollups[CLAUDE_LIKE_MODEL].pricing_billing_basis == "claude_ccu_equivalent"
    assert rollups[MINISTRAL_LIKE_MODEL].pricing_billing_basis == "marketplace_partner_token_rate"
    assert rollups[MICROSOFT_MODEL].pricing_billing_basis == "token_rate"

    rendered = report_html(report)
    assert "CCU-equivalent estimate" in rendered
    assert "Marketplace partner rate" in rendered
    assert "Azure token rate" in rendered


def test_report_pricing_provenance_selects_customer_catalog():
    catalog = PricingCatalog(
        catalog_name="synthetic-customer-catalog",
        source_url="https://example.com/customer-pricing",
        retrieved_at="2026-09-14",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model=MICROSOFT_MODEL,
                region="global",
                effective_from="2026-01-01",
                input_per_million=1.0,
                output_per_million=4.0,
            )
        ],
    )
    report = analyze(
        [trace(deployment="dep", model=MICROSOFT_MODEL)],
        "fixture",
        customer_catalog=catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    pricing = report.report_metadata["pricing"]
    assert pricing["catalog_selected"] == "synthetic-customer-catalog"
    assert pricing["source_url"] == "https://example.com/customer-pricing"
    assert pricing["retrieved_at"] == "2026-09-14"


def test_removing_one_rate_produces_exact_partial_coverage_not_rounded_to_full():
    partial_catalog = full_customer_catalog()
    partial_catalog.prices = [price for price in partial_catalog.prices if price.model != MINISTRAL_LIKE_MODEL]
    report = analyze(
        three_model_records(),
        "fixture",
        customer_catalog=partial_catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    assert 0 < report.summary.pricing_coverage_requests_percent < 100
    assert report.summary.estimated_cost_usd is not None  # partial coverage still yields a usable estimate
    assert report.summary.unresolved_requests == 1
    assert report.summary.unresolved_tokens == 22
    assert MINISTRAL_LIKE_MODEL in report.summary.suggested_override_keys
    assert "no-exact-model-mode-price" in report.summary.unresolved_reasons

    rendered = report_html(report)
    # Partial coverage keeps a hatched unpriced segment and one concise warning
    # rather than a paragraph repeated in every card.
    assert "Unpriced</span>" in rendered
    assert "Partial estimate" in rendered
    # The remediation command appears once, not in every card.
    assert rendered.count("tokenlens-azure foundry pricing") <= 1
    assert rendered.count("pricing-audit") <= 1
    assert MINISTRAL_LIKE_MODEL in rendered


def test_zero_coverage_cost_panel_shows_explicit_empty_state_not_zero_bars():
    report = analyze(
        three_model_records(),
        "fixture",
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    assert report.summary.estimated_cost_usd is None
    assert report.summary.unresolved_requests == 3
    rendered = report_html(report)
    assert "No costs to chart until pricing is configured." in rendered
    assert "cost-empty" in rendered
    # The old copy stated a fact about the tool, not what the user must do.
    assert "No priced components yet" not in rendered
    # A genuinely zero-cost panel must never render three zero-width bars
    # that could be mistaken for a resolved $0 cost.
    assert 'style="width:0.0%' not in rendered


def test_unresolved_models_table_lists_reason_and_suggested_override_key():
    report = analyze(
        three_model_records(),
        "fixture",
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    rendered = report_html(report)
    # One remediation disclosure carries the affected models and the exact key.
    assert 'id="resolve-pricing"' in rendered
    assert "Suggested override key" in rendered
    for model in (MICROSOFT_MODEL, CLAUDE_LIKE_MODEL, MINISTRAL_LIKE_MODEL):
        assert model in rendered


def test_adaptive_precision_never_renders_a_nonzero_cost_as_zero():
    tiny_catalog = PricingCatalog(
        catalog_name="tiny-rate-catalog",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="tiny-rate-model-test-synthetic",
                region="global",
                effective_from="2026-01-01",
                input_per_million=0.001,
                output_per_million=0.001,
            )
        ],
    )
    report = analyze(
        [trace(deployment="tiny", model="tiny-rate-model-test-synthetic", input_tokens=5, output_tokens=1)],
        "fixture",
        customer_catalog=tiny_catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    cost = report.summary.estimated_cost_usd
    assert cost is not None and cost > 0
    rendered = report_html(report)
    assert "$0.00<" not in rendered
    assert "$0.00 " not in rendered
    assert "$0.00\n" not in rendered
    assert "$0.00<" not in rendered


def test_pricing_audit_text_lists_unresolved_models_without_leaking_identifiers():
    report = analyze(
        three_model_records(),
        "fixture",
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    text = pricing_audit_text(report)
    for model in (MICROSOFT_MODEL, CLAUDE_LIKE_MODEL, MINISTRAL_LIKE_MODEL):
        assert model in text
    assert "suggested-override-key" in text
    assert "unresolved: 1 requests" in text
    # No endpoint/resource/tenant/request-id vocabulary should ever appear.
    for forbidden in ("endpoint", "resource_name", "subscription", "request_id", "tenant"):
        assert forbidden not in text.casefold()


def test_pricing_audit_cli_command_runs_offline(tmp_path: Path):
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "timestamp": "2026-09-14T12:00:00Z",
                    "deployment_name": "dep",
                    "model_name": MICROSOFT_MODEL,
                    "provider": "azure_foundry",
                    "deployment_mode": "global",
                    "messages": [],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }
            )
            for _ in range(1)
        )
        + "\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["pricing-audit", str(trace_path)])
    assert result.exit_code == 0, result.output
    assert MICROSOFT_MODEL in result.output
    assert "Pricing audit" in result.output


def test_tool_step_event_with_observed_cost_resolves_without_crashing():
    """Regression test: ToolStepEvent has no model_name, so publisher inference
    on the observed-cost path must not assume ModelCallEvent's shape."""
    from datetime import UTC, datetime

    from tokenlens.events import ToolStepEvent
    from tokenlens.pricing import resolve_event_cost

    event = ToolStepEvent(
        event_id="e1",
        timestamp=datetime.now(UTC),
        task_id="t1",
        task_type="tt",
        attempt_id="a1",
        execution_strategy="default",
        strategy_version="v1",
        step_index=1,
        tool_category="search",
        observed_cost_usd=0.002,
    )
    resolution = resolve_event_cost(event)
    assert resolution.resolved is True
    assert resolution.cost_usd == pytest.approx(0.002)
    assert resolution.publisher is None
    assert resolution.billing_basis == "observed_cost"
