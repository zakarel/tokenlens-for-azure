"""Cost analysis states, semantic availability, and the Workloads tab.

These assertions lock the communication contract: one decision state, one
impact statement, one action, and technical detail behind a disclosure. Every
fixture is synthetic and offline.
"""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.models import AvailabilityState
from tokenlens.pricing import PriceEntry, PricingCatalog
from tokenlens.reports import report_html
from tokenlens.workloads import WorkloadMapping, merge_identities

PRICED_MODEL = "priced-model-test-synthetic"
SECOND_PRICED_MODEL = "second-priced-model-test-synthetic"
UNPRICED_MODEL = "unpriced-model-test-synthetic"


def catalog(models=(PRICED_MODEL, SECOND_PRICED_MODEL)) -> PricingCatalog:
    return PricingCatalog(
        catalog_name="synthetic-test-catalog",
        source_url="https://example.invalid/pricing",
        retrieved_at="2026-09-10",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                publisher="microsoft",
                model=model,
                effective_from="2026-01-01",
                input_per_million=1.0,
                output_per_million=2.0,
            )
            for model in models
        ],
    )


def record(index: int, *, deployment: str, model: str, workload: str | None = None, day: int = 1):
    raw = {
        "timestamp": (datetime(2026, 9, day, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
        "deployment_name": deployment,
        "model_name": model,
        "deployment_mode": "global",
        "messages": [{"role": "user", "content": "synthetic"}],
        "usage": {"input_tokens": 1000, "output_tokens": 500},
        "status_code": 200,
    }
    if workload:
        raw["metadata"] = {"workload": workload}
    return next(iter_records(io.StringIO(json.dumps(raw) + "\n")))


def rendered(records, **kwargs) -> str:
    report = analyze(
        records,
        "2 local telemetry files",
        generated_at="2026-09-15T08:00:00Z",
        use_bundled_reference=False,
        data_classification="local_real",
        **kwargs,
    )
    return report_html(report)


def cost_panel(html: str) -> str:
    start = html.index('id="cost-panel"')
    return html[start : html.index('id="workloads-panel"')]


def workloads_panel(html: str) -> str:
    start = html.index('id="workloads-panel"')
    return html[start : html.index('id="ptu-panel"')]


# --- Semantic availability --------------------------------------------------


@pytest.mark.parametrize(
    ("status", "symbol", "tone"),
    [
        ("available", "✓", "success"),
        ("partial", "⚠", "warning"),
        ("missing_actionable", "⚠", "warning"),
        ("missing_blocking", "✕", "danger"),
        ("not_measured", "—", "neutral"),
        ("not_applicable", "i", "info"),
    ],
)
def test_availability_states_map_to_one_symbol_and_one_semantic_tone(status, symbol, tone):
    state = AvailabilityState(status=status, label="Example")
    assert state.symbol == symbol
    assert state.tone == tone
    # The plain-text rendering is what terminals, exports, and screen readers use.
    assert state.text.startswith(f"{symbol} Example")


def test_report_defines_the_semantic_status_tokens_once():
    html = rendered([record(0, deployment="a", model=PRICED_MODEL)], customer_catalog=catalog())
    for token in (
        "--status-success-fg",
        "--status-success-bg",
        "--status-warning-fg",
        "--status-warning-bg",
        "--status-danger-fg",
        "--status-danger-bg",
        "--status-info-fg",
        "--status-info-bg",
        "--status-neutral-fg",
        "--status-neutral-bg",
    ):
        assert html.count(token + ":") == 1


def test_every_coloured_state_carries_a_symbol_and_explicit_text():
    html = rendered(
        [record(i, deployment="a", model=UNPRICED_MODEL) for i in range(2)],
        customer_catalog=catalog(),
    )
    badges = re.findall(r'<span class="state state-[a-z]+">(.*?)</span></span>', html)
    assert badges
    for badge in badges:
        assert 'class="state-symbol"' in badge
        assert 'class="state-label"' in badge
        # The symbol is decorative; the label carries the meaning.
        assert 'aria-hidden="true"' in badge


# --- Cost analysis states ---------------------------------------------------


def test_zero_coverage_renders_one_banner_and_one_remediation_action():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=UNPRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert html.count('class="state-banner') == 1
    assert "Pricing setup required" in html
    assert html.count('data-open-remediation="1"') == 2  # banner action plus the empty state
    assert html.count('id="resolve-pricing"') == 1
    assert "No costs to chart until pricing is configured." in html
    # The remediation command is stated once, not repeated per card.
    assert html.count("tokenlens-azure foundry pricing") == 1


def test_the_pricing_audit_phrase_never_repeats_inside_cost_analysis():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=UNPRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert html.count("pricing-audit") <= 1


def test_unavailable_conditional_cards_are_omitted_rather_than_filling_the_grid():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=UNPRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert "Highest cost model" not in html
    assert "Pricing snapshot" not in html
    assert "Rate source" not in html
    # The four primary cards always render.
    for label in ("Estimated cost", "Pricing coverage", "Unpriced tokens", "Models requiring pricing"):
        assert label in html


def test_conditional_cards_appear_once_a_cost_resolves():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert "Highest cost model" in html
    assert "Pricing snapshot" in html
    assert "Rate source" in html


def test_no_source_or_retrieval_date_is_shown_when_no_entry_matched():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=UNPRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert "No catalog entry matched" in html
    assert "https://example.invalid/pricing" not in html
    assert "2026-09-10" not in html
    assert "Publisher:" not in html
    assert "Billing basis:" not in html


def test_provenance_details_are_collapsed_by_default():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert '<details class="provenance-details"><summary>Technical pricing details</summary>' in html
    assert "<details open" not in html
    assert 'class="how-pricing"><summary>How pricing works</summary>' in html
    # The explanatory intro paragraph is gone from the default view.
    assert "Costs use observed values first, then exact customer or bundled reference" not in html


def test_identity_failures_and_price_misses_are_counted_separately():
    records = [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)]
    records += [record(i, deployment="b", model=UNPRICED_MODEL) for i in range(2)]
    records += [record(i, deployment="c", model="unknown") for i in range(1)]
    html = cost_panel(rendered(records, customer_catalog=catalog()))
    assert "Models requiring pricing" in html
    assert "1 separate identity failure(s)" in html
    assert "Model identity missing" in html
    assert "Exact rate missing" in html


def test_unknown_is_never_offered_as_a_pricing_override_key():
    records = [record(i, deployment="c", model="unknown") for i in range(2)]
    html = cost_panel(rendered(records, customer_catalog=catalog()))
    assert "Model identity missing" in html
    assert "Suggested override key" not in html
    assert "Fix collection identity" in html


def test_partial_and_complete_coverage_are_distinguishable_without_colour():
    partial = cost_panel(
        rendered(
            [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)]
            + [record(i, deployment="b", model=UNPRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    complete = cost_panel(
        rendered(
            [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert "Partial estimate" in partial
    assert "Unpriced</span>" in partial
    assert 'class="excluded"' in partial
    assert "Pricing complete" in complete
    assert "Partial estimate" not in complete
    assert 'class="excluded"' not in complete


def test_a_component_with_no_tokens_is_omitted_not_drawn_as_a_zero_bar():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    composition = html[html.index("Cost composition") : html.index("Pricing source")]
    assert "Fresh input" in composition
    # No cached tokens were observed, so no cached component bar is drawn.
    assert "Cached input" not in composition
    assert 'style="width:0.0%' not in html


def test_the_cost_view_can_be_switched_between_workload_deployment_model_and_component():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    for view in ("workload", "deployment", "model", "component"):
        assert f'data-cost-view="{view}"' in html
        assert f'data-cost-panel="{view}"' in html
    # Workload is the default view and the only one rendered unhidden.
    assert '<button type="button" class="view-tab" data-cost-view="workload" aria-pressed="true"' in html
    assert '<div class="cost-view" data-cost-panel="deployment" hidden>' in html


def test_the_workload_cost_view_explains_how_to_configure_business_identity():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert "Showing deployment-backed technical workloads" in html
    assert "tokenlens-azure foundry workloads configure" in html


def test_unavailable_cost_is_never_rendered_as_a_currency_zero():
    html = cost_panel(
        rendered(
            [record(i, deployment="a", model=UNPRICED_MODEL) for i in range(3)],
            customer_catalog=catalog(),
        )
    )
    assert ">Unavailable<" in html
    assert "$0.00<" not in html
    # Within a pricing section the word "Pricing" is already established.
    assert "Pricing unavailable" not in html


# --- Workloads tab ----------------------------------------------------------


def test_the_workloads_tab_is_present_whenever_one_deployment_resolves():
    html = rendered(
        [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)], customer_catalog=catalog()
    )
    assert '<span class="tab-full">Workloads</span>' in html
    assert 'id="workloads-panel"' in html
    panel = workloads_panel(html)
    assert "Technical · Needs configuration" in panel
    assert "Showing deployment-backed technical workloads" in panel
    assert "tokenlens-azure foundry workloads configure" in panel


def test_configured_business_rows_are_badged_differently_from_technical_defaults():
    mapping = WorkloadMapping(
        id="support-assistant",
        name="Support assistant",
        type="agent",
        environment="production",
        deployments=["a"],
        allocation="dedicated",
    )
    html = rendered(
        [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)],
        customer_catalog=catalog(),
        workload_mappings=[mapping],
        workload_identities=merge_identities(["a"], mappings=[mapping]),
    )
    panel = workloads_panel(html)
    assert "Business · Configured" in panel
    assert "Support assistant" in panel
    # The technical reconciliation layer is preserved, not renamed.
    assert 'data-workload-panel="technical"' in panel
    assert ">a<" in panel


def test_three_workloads_two_dedicated_and_one_shared_render_exact_totals():
    mappings = [
        WorkloadMapping(id="support", name="Support assistant", deployments=["support-prod"], allocation="dedicated"),
        WorkloadMapping(id="coding", name="Coding agent", deployments=["coding-prod"], allocation="dedicated"),
        WorkloadMapping(id="docs", name="Document processor", deployments=["shared-prod"], allocation="shared"),
    ]
    records = [record(i, deployment="support-prod", model=PRICED_MODEL) for i in range(4)]
    records += [record(i, deployment="coding-prod", model=SECOND_PRICED_MODEL) for i in range(3)]
    records += [
        record(i, deployment="shared-prod", model=PRICED_MODEL, workload="Document processor" if i % 2 == 0 else None)
        for i in range(4)
    ]
    report = analyze(
        records,
        "fixture",
        generated_at="2026-09-15T08:00:00Z",
        use_bundled_reference=False,
        customer_catalog=catalog(),
        workload_mappings=mappings,
        workload_identities=merge_identities(
            ["support-prod", "coding-prod", "shared-prod"], mappings=mappings
        ),
    )
    portfolio = report.workloads
    business = {item.workload_id: item for item in portfolio.business_workloads}
    assert set(business) == {"support", "coding", "docs", "unassigned"}
    assert business["support"].total_tokens == 6000
    assert business["coding"].total_tokens == 4500
    assert business["docs"].total_tokens == 3000
    assert business["unassigned"].total_tokens == 3000
    # Business allocations plus Unassigned reconcile exactly with the technical total.
    technical_total = sum(item.total_tokens or 0 for item in portfolio.workloads)
    assert sum(item.total_tokens or 0 for item in business.values()) == technical_total
    panel = workloads_panel(report_html(report))
    assert "Unassigned workload" in panel
    assert "cannot be attributed to a business workload" in panel


def test_task_metrics_appear_only_where_task_evidence_exists():
    html = rendered(
        [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)], customer_catalog=catalog()
    )
    panel = workloads_panel(html)
    assert "Task identity and outcomes were not collected" in panel
    assert "Cost/solved task" in panel
    assert panel.count("Not measured") >= 1
    assert "$0 per solved task" not in panel


def test_workload_readiness_separates_identity_from_pricing():
    records = [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)]
    records += [record(i, deployment="b", model=UNPRICED_MODEL) for i in range(2)]
    panel = workloads_panel(rendered(records, customer_catalog=catalog()))
    assert "Identity versus pricing readiness" in panel
    assert "Full workload economics" in panel
    assert "Usage only; cost unavailable" in panel


def test_the_report_router_knows_the_workloads_tab():
    html = rendered(
        [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)], customer_catalog=catalog()
    )
    assert html.count('const names = ["overview", "cost", "workloads", "usage", "ptu"];') == 2
    assert 'aria-controls="workloads-panel"' in html


def test_the_daily_trend_uses_the_reporting_currency_not_a_hardcoded_dollar():
    """Regression: TokenLens never converts currencies, so the symbol must match."""
    euro_catalog = PricingCatalog(
        currency="EUR",
        catalog_name="synthetic-euro-catalog",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model=PRICED_MODEL,
                effective_from="2026-01-01",
                input_per_million=1.0,
                output_per_million=2.0,
            )
        ],
    )
    html = rendered(
        [record(i, deployment="a", model=PRICED_MODEL) for i in range(3)],
        customer_catalog=euro_catalog,
    )
    panel = workloads_panel(html)
    trend = panel[panel.index("Daily cost trend") : panel.index("Workload readiness")]
    assert "€" in trend
    assert "$" not in trend


def test_the_workload_cost_view_reconciles_with_the_deployment_view():
    """Regression: unattributed traffic must stay visible in the business scope."""
    records = [record(i, deployment="tagged-prod", model=PRICED_MODEL, workload="Support assistant") for i in range(3)]
    records += [record(i, deployment="untagged-prod", model=PRICED_MODEL) for i in range(3)]
    report = analyze(
        records,
        "fixture",
        generated_at="2026-09-15T08:00:00Z",
        use_bundled_reference=False,
        customer_catalog=catalog(),
    )
    portfolio = report.workloads
    business_cost = sum(item.estimated_cost or 0 for item in portfolio.business_workloads)
    technical_cost = sum(item.estimated_cost or 0 for item in portfolio.workloads)
    assert business_cost == pytest.approx(technical_cost)
    assert business_cost == pytest.approx(report.summary.estimated_cost_usd)
    html = cost_panel(report_html(report))
    workload_view = html[html.index('data-cost-panel="workload"') : html.index('data-cost-panel="deployment"')]
    assert "Support assistant" in workload_view
    assert "Unassigned" in workload_view


def test_a_mixed_rollup_reports_the_rate_problem_not_a_blocking_identity_failure():
    """A blocking identity state is claimed only when the whole rollup is unidentified."""
    records = [record(i, deployment="shared-prod", model=UNPRICED_MODEL, workload="Docs") for i in range(2)]
    records += [record(i + 5, deployment="shared-prod", model=UNPRICED_MODEL) for i in range(4)]
    records += [record(i, deployment="legacy-prod", model="unknown") for i in range(1)]
    panel = workloads_panel(rendered(records, customer_catalog=catalog()))
    unassigned = panel[panel.index("Unassigned") :]
    assert "Exact rate missing" in unassigned
    assert "also need collection identity" in unassigned


def test_a_fully_unidentified_rollup_still_reports_a_blocking_identity_failure():
    records = [record(i, deployment="tagged-prod", model=PRICED_MODEL, workload="Docs") for i in range(2)]
    records += [record(i, deployment="legacy-prod", model="unknown") for i in range(2)]
    panel = workloads_panel(rendered(records, customer_catalog=catalog()))
    assert "Model identity missing" in panel
