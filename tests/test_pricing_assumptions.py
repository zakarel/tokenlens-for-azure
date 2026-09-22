"""Visible pricing assumptions, provenance, and source precedence.

The user-selected defaults — retail, global, standard, short context, normal
inference — are applied only where telemetry or deployment metadata did not
state the dimension, and they are never hidden: they appear in bold at the top
of Cost analysis, in the pricing provenance, and in the JSON metadata.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.pricing import PriceEntry, PricingCatalog
from tokenlens.pricing_sources.assumptions import (
    ASSUMPTIONS_BANNER,
    ASSUMPTIONS_WARNING,
    DEFAULT_PRICING_ASSUMPTIONS,
    PricingAssumptions,
)
from tokenlens.pricing_sources.azure_retail import retail_snapshot
from tokenlens.pricing_sources.cache import AZURE_RETAIL_SNAPSHOT, CLAUDE_SNAPSHOT, write_snapshot
from tokenlens.pricing_sources.claude_docs import claude_snapshot
from tokenlens.pricing_sources.fetch import FetchedPage
from tokenlens.pricing_sources.sync import (
    PUBLIC_CATALOG_NAME,
    public_pricing_catalog,
    public_sync_needed,
    sync_public_pricing,
)
from tokenlens.reference import load_bundled_reference_catalog, load_effective_reference_catalog
from tokenlens.reports import report_html

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)

EXPECTED_BANNER = "Pricing assumptions: Retail · Global · Standard · Short context · Normal inference"


# --- Fixtures ---------------------------------------------------------------


def _retail_transport():
    first = (FIXTURES / "azure_retail_page1.json").read_bytes()
    second = (FIXTURES / "azure_retail_page2.json").read_bytes()

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        return FetchedPage(url=url, status=200, body=second if "skip" in url else first)

    return transport


def _claude_transport():
    body = (FIXTURES / "claude_pricing.html").read_bytes()

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        return FetchedPage(url=url, status=200, body=body)

    return transport


def _both_transport():
    retail = _retail_transport()
    claude = _claude_transport()

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        if "prices.azure.com" in url:
            return retail(url, timeout, max_bytes)
        return claude(url, timeout, max_bytes)

    return transport


@pytest.fixture()
def synced(tmp_path, monkeypatch):
    """A populated, user-local snapshot cache built from synthetic fixtures."""
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    write_snapshot(
        retail_snapshot(transport=_retail_transport(), now=NOW), name=AZURE_RETAIL_SNAPSHOT
    )
    write_snapshot(
        claude_snapshot(models=["Claude Opus 5"], transport=_claude_transport(), now=NOW),
        name=CLAUDE_SNAPSHOT,
    )
    return tmp_path


def record(index: int, *, model: str, deployment: str = "prod", cached: int = 0):
    raw = {
        "timestamp": (datetime(2026, 9, 15, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
        "deployment_name": deployment,
        "model_name": model,
        "deployment_mode": "global",
        "messages": [{"role": "user", "content": "synthetic"}],
        "usage": {"input_tokens": 1000, "output_tokens": 500, "cached_tokens": cached},
        "status_code": 200,
    }
    return next(iter_records(io.StringIO(json.dumps(raw) + "\n")))


def analyzed(records, **kwargs):
    return analyze(
        records,
        "synthetic telemetry",
        generated_at="2026-09-22T08:00:00Z",
        data_classification="synthetic",
        **kwargs,
    )


def cost_panel(html: str) -> str:
    start = html.index('id="cost-panel"')
    return html[start : html.index('id="workloads-panel"')]


# --- The assumptions themselves --------------------------------------------


def test_the_defaults_are_exactly_the_requested_dimensions():
    assumptions = DEFAULT_PRICING_ASSUMPTIONS
    assert assumptions.purchase_model == "retail"
    assert assumptions.deployment == "global"
    assert assumptions.service_tier == "standard"
    assert assumptions.context == "short"
    assert assumptions.inference == "normal"


def test_the_banner_line_is_rendered_verbatim():
    assert ASSUMPTIONS_BANNER == EXPECTED_BANNER
    assert DEFAULT_PRICING_ASSUMPTIONS.banner_text() == EXPECTED_BANNER


def test_the_warning_states_that_exact_dimensions_win():
    assert "Exact observed or configured dimensions" in ASSUMPTIONS_WARNING
    assert "override" in ASSUMPTIONS_WARNING


def test_the_metadata_is_machine_readable_and_lists_the_excluded_modes():
    metadata = DEFAULT_PRICING_ASSUMPTIONS.to_metadata()
    assert metadata["banner"] == EXPECTED_BANNER
    assert metadata["warning"] == ASSUMPTIONS_WARNING
    assert metadata["dimensions"] == {
        "purchase_model": "retail",
        "deployment": "global",
        "service_tier": "standard",
        "context": "short",
        "inference": "normal",
    }
    assert set(metadata["excluded_modes"]) >= {
        "batch",
        "fine_tuning",
        "priority",
        "provisioned",
        "media",
        "tool",
        "session",
    }


def test_the_assumptions_are_immutable():
    with pytest.raises(Exception):
        PricingAssumptions().purchase_model = "enterprise"  # type: ignore[misc]


# --- Report rendering -------------------------------------------------------


def test_cost_analysis_opens_with_the_bold_assumptions_banner(synced):
    html = report_html(analyzed([record(i, model="gpt-5.6-luna") for i in range(4)]))
    panel = cost_panel(html)
    assert f"<strong>{EXPECTED_BANNER}</strong>" in panel
    # It is the first thing inside the panel, above the decision banner.
    assert panel.index("pricing-assumptions") < panel.index("state-banner")


def test_the_banner_carries_the_override_warning_and_a_tooltip(synced):
    panel = cost_panel(report_html(analyzed([record(i, model="gpt-5.6-luna") for i in range(4)])))
    assert 'class="assumption-warning"' in panel
    assert "Exact observed or configured dimensions" in panel
    assert 'role="note"' in panel
    assert 'title="These defaults are used only where telemetry' in panel


def test_the_banner_is_shown_even_when_nothing_could_be_priced():
    panel = cost_panel(
        report_html(
            analyzed(
                [record(i, model="no-such-model-test-synthetic") for i in range(4)],
                use_bundled_reference=False,
            )
        )
    )
    assert f"<strong>{EXPECTED_BANNER}</strong>" in panel
    assert "Exact observed or configured dimensions" in panel


def test_pricing_provenance_repeats_the_assumptions_and_names_each_source(synced):
    panel = cost_panel(report_html(analyzed([record(i, model="gpt-5.6-luna") for i in range(4)])))
    assert "Technical pricing details" in panel
    assert f"<strong>{EXPECTED_BANNER}</strong>" in panel
    assert "Synchronized source: azure_retail_prices" in panel
    assert "sha256:" in panel


def test_claude_provenance_states_the_ccu_basis_and_the_discount_caveat(synced):
    panel = cost_panel(report_html(analyzed([record(i, model="claude-opus-5") for i in range(4)])))
    assert "100 CCU = $1" in panel
    assert "private discounts are not included" in panel
    # The dollar estimate is reported with its consumption-unit equivalent.
    assert "CCU</li>" in panel or "CCU." in panel
    assert "≈" in panel


# --- JSON metadata ----------------------------------------------------------


def test_report_metadata_carries_the_assumptions_block(synced):
    report = analyzed([record(i, model="gpt-5.6-luna") for i in range(4)])
    pricing = report.report_metadata["pricing"]
    assert pricing["assumptions_banner"] == EXPECTED_BANNER
    assert pricing["assumptions_warning"] == ASSUMPTIONS_WARNING
    assert pricing["assumptions"]["dimensions"]["deployment"] == "global"
    assert pricing["assumptions"]["overridden_by"] == "exact_observed_or_configured_dimensions"
    sources = {item["source"] for item in pricing["public_sources"]}
    assert sources == {"azure_retail_prices", "claude_pricing_docs"}
    assert all(item["content_hash"].startswith("sha256:") for item in pricing["public_sources"])


def test_applied_defaults_are_listed_separately_from_the_policy(synced):
    report = analyzed([record(i, model="Ministral-3B") for i in range(4)])
    applied = report.report_metadata["pricing"]["assumed_dimensions_applied"]
    # Ministral's meters state no deployment or context, so both were assumed.
    assert "deployment=global" in applied
    assert "context=short" in applied


def test_the_json_report_serializes_the_assumptions(synced):
    report = analyzed([record(i, model="gpt-5.6-luna") for i in range(4)])
    payload = json.loads(report.model_dump_json())
    assert payload["report_metadata"]["pricing"]["assumptions_banner"] == EXPECTED_BANNER


# --- Source precedence ------------------------------------------------------


def test_the_cached_public_sync_is_preferred_over_the_packaged_catalog(synced):
    catalog = load_effective_reference_catalog()
    assert catalog.catalog_name.startswith(PUBLIC_CATALOG_NAME)
    packaged = load_bundled_reference_catalog()
    # Both sets are present; the synchronized entries rank first.
    assert len(catalog.prices) == len(packaged.prices) + len(public_pricing_catalog().prices)
    assert catalog.prices[0].source_rank == 10
    assert catalog.prices[-1].source_rank == 100


def test_a_synchronized_rate_wins_over_a_packaged_rate_for_the_same_model(synced):
    packaged = load_bundled_reference_catalog()
    stale = PriceEntry(
        provider="azure_foundry",
        publisher="microsoft",
        model="gpt-5.6-luna",
        region="global",
        effective_from=date(2026, 1, 1),
        input_per_million=99.0,
        output_per_million=99.0,
    )
    packaged.prices.append(stale)
    merged = PricingCatalog(
        catalog_name="merged-test",
        retrieved_at=date(2026, 9, 22),
        pricing_basis="analysis_date_snapshot",
        prices=[*public_pricing_catalog().prices, *packaged.prices],
    )
    report = analyzed(
        [record(i, model="gpt-5.6-luna") for i in range(4)], reference_catalog=merged
    )
    # 4 requests * (1000 input @ 0.20/M + 500 output @ 1.20/M)
    assert report.summary.estimated_cost_usd == pytest.approx(4 * (0.0002 + 0.0006))


def test_a_customer_override_still_wins_over_the_synchronized_rate(synced):
    customer = PricingCatalog(
        catalog_name="customer-overrides",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                publisher="microsoft",
                model="gpt-5.6-luna",
                region="global",
                effective_from=date(2026, 1, 1),
                confidence="customer_override",
                input_per_million=0.10,
                output_per_million=0.60,
                note="Negotiated enterprise agreement",
            )
        ],
    )
    report = analyzed(
        [record(i, model="gpt-5.6-luna") for i in range(4)],
        customer_catalog=customer,
        reference_catalog=public_pricing_catalog(),
    )
    assert report.summary.pricing_source == "customer"
    assert report.summary.estimated_cost_usd == pytest.approx(4 * (0.0001 + 0.0003))


def test_without_a_cached_snapshot_the_packaged_catalog_is_used_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "empty"))
    assert public_pricing_catalog() is None
    assert load_effective_reference_catalog().catalog_name == load_bundled_reference_catalog().catalog_name


# --- Report states ----------------------------------------------------------


def test_an_exactly_priced_portfolio_reports_complete_coverage(synced):
    report = analyzed([record(i, model="gpt-5.6-luna") for i in range(4)])
    assert report.summary.pricing_coverage_tokens_percent == pytest.approx(100.0)
    assert report.summary.unresolved_tokens == 0
    panel = cost_panel(report_html(report))
    assert "Pricing complete" in panel


def test_a_partly_priced_portfolio_reports_a_range_not_a_total(synced):
    records = [record(i, model="gpt-5.6-luna") for i in range(3)]
    records += [record(10 + i, model="no-such-model-test-synthetic", deployment="other") for i in range(3)]
    report = analyzed(records)
    assert 0 < report.summary.pricing_coverage_tokens_percent < 100
    panel = cost_panel(report_html(report))
    assert "Partial estimate" in panel
    assert "Unpriced" in panel
    # The assumptions stay visible in the partial state too.
    assert f"<strong>{EXPECTED_BANNER}</strong>" in panel


def test_an_unresolved_portfolio_withholds_cost_and_keeps_tokens_visible():
    report = analyzed(
        [record(i, model="no-such-model-test-synthetic") for i in range(4)],
        use_bundled_reference=False,
    )
    assert report.summary.estimated_cost_usd is None
    assert report.summary.unresolved_tokens > 0
    panel = cost_panel(report_html(report))
    assert "Pricing setup required" in panel
    assert "Unavailable" in panel


# --- Freshness and sync orchestration --------------------------------------


def test_an_absent_or_stale_cache_requests_a_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    assert public_sync_needed() is True
    write_snapshot(retail_snapshot(transport=_retail_transport(), now=NOW), name=AZURE_RETAIL_SNAPSHOT)
    write_snapshot(
        claude_snapshot(transport=_claude_transport(), now=NOW), name=CLAUDE_SNAPSHOT
    )
    assert public_sync_needed(now=NOW) is False
    assert public_sync_needed(now=NOW + timedelta(days=30)) is True


def test_a_full_sync_writes_both_snapshots_and_reports_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    report = sync_public_pricing(transport=_both_transport(), now=NOW)
    assert report.ok is True
    assert {item.source for item in report.outcomes} == {
        "azure_retail_prices",
        "claude_pricing_docs",
    }
    assert all(item.content_hash.startswith("sha256:") for item in report.outcomes)
    assert public_pricing_catalog() is not None


def test_one_failing_source_never_blocks_the_other(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    claude = _claude_transport()

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        if "prices.azure.com" in url:
            raise OSError("synthetic outage")
        return claude(url, timeout, max_bytes)

    report = sync_public_pricing(
        transport=transport, now=NOW, budget=_no_sleep_budget()
    )
    retail = next(item for item in report.outcomes if item.source == "azure_retail_prices")
    anthropic = next(item for item in report.outcomes if item.source == "claude_pricing_docs")
    assert retail.ok is False and retail.used_cache is False
    assert anthropic.ok is True and anthropic.entries > 0
    assert report.ok is False
    # Analysis still works: the Claude snapshot is readable offline.
    assert any(item.publisher == "anthropic" for item in public_pricing_catalog().prices)


def _no_sleep_budget():
    from tokenlens.pricing_sources.fetch import FetchBudget

    return FetchBudget(retries=0, sleep=lambda _: None)


# --- Applied assumptions come from what actually priced a record ------------


def test_only_the_defaults_that_priced_a_record_are_reported_as_applied(synced):
    """Ministral assumes deployment and context; Luna assumes neither."""
    luna = analyzed([record(i, model="gpt-5.6-luna") for i in range(4)])
    applied = luna.report_metadata["pricing"]["assumed_dimensions_applied"]
    assert applied == ["purchase_model=retail"]
    # The catalogs *could* have supplied more; that is reported separately.
    available = luna.report_metadata["pricing"]["catalog_assumed_dimensions_available"]
    assert "deployment=global" in available
    assert "context=short" in available
    assert "deployment=global" not in applied


def test_an_observed_cost_applies_no_assumption_at_all(synced):
    observed = record(0, model="gpt-5.6-luna")
    observed.observed_cost_usd = 1.23
    report = analyzed([observed])
    assert report.summary.pricing_source == "observed"
    assert report.report_metadata["pricing"]["assumed_dimensions_applied"] == []
    panel = cost_panel(report_html(report))
    assert "Applied to matched rates" not in panel
    # The policy banner is still shown; only the applied list is empty.
    assert f"<strong>{EXPECTED_BANNER}</strong>" in panel


def test_a_customer_override_with_no_defaults_applies_no_assumption(synced):
    customer = PricingCatalog(
        catalog_name="customer-overrides",
        retrieved_at=date(2026, 9, 22),
        pricing_basis="analysis_date_snapshot",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                publisher="mistral",
                model="ministral-3b",
                region="global",
                effective_from=date(2026, 1, 1),
                confidence="customer_override",
                input_per_million=0.04,
                output_per_million=0.04,
            )
        ],
    )
    report = analyzed(
        [record(i, model="Ministral-3B") for i in range(4)],
        customer_catalog=customer,
        reference_catalog=customer,
    )
    assert report.summary.pricing_source == "customer"
    assert report.report_metadata["pricing"]["assumed_dimensions_applied"] == []


def test_an_unpriced_portfolio_applies_no_assumption(synced):
    report = analyzed([record(i, model="no-such-model-test-synthetic") for i in range(4)])
    assert report.summary.estimated_cost_usd is None
    assert report.report_metadata["pricing"]["assumed_dimensions_applied"] == []


def test_a_mixed_portfolio_reports_the_union_of_what_actually_applied(synced):
    records = [record(i, model="gpt-5.6-luna") for i in range(2)]
    records += [record(10 + i, model="Ministral-3B", deployment="compact") for i in range(2)]
    applied = analyzed(records).report_metadata["pricing"]["assumed_dimensions_applied"]
    assert "deployment=global" in applied
    assert "context=short" in applied
    assert "purchase_model=retail" in applied
