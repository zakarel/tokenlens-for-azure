import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.pricing import PriceEntry, PricingCatalog
from tokenlens.presentation import model_rollups
from tokenlens.reference import load_bundled_reference_catalog
from tokenlens.reports import report_html


def parsed(value):
    return next(iter_records(io.StringIO(json.dumps(value) + "\n")))


def trace(
    *,
    deployment="demo",
    model="MAI-Thinking-1",
    mode="global",
    timestamp="2026-09-14T12:00:00Z",
    input_tokens=1000,
    cached_tokens=100,
    output_tokens=100,
    latency_ms=1000,
    status_code=200,
):
    return parsed(
        {
            "timestamp": timestamp,
            "deployment_name": deployment,
            "model_name": model,
            "provider": "azure_foundry",
            "deployment_mode": mode,
            "messages": [],
            "usage": {
                "input_tokens": input_tokens,
                "cached_tokens": cached_tokens,
                "output_tokens": output_tokens,
            },
            "latency_ms": latency_ms,
            "status_code": status_code,
        }
    )


def test_official_reference_pricing_matches_exact_model_and_mode():
    catalog = load_bundled_reference_catalog()
    global_report = analyze(
        [trace(model="MAI-DS-R1", cached_tokens=0, mode="global")],
        "fixture",
        reference_catalog=catalog,
        generated_at="2026-09-14T13:00:00Z",
    )
    regional_report = analyze(
        [trace(model="mai-ds-r1", cached_tokens=0, mode="regional")],
        "fixture",
        reference_catalog=catalog,
        generated_at="2026-09-14T13:00:00Z",
    )
    assert global_report.summary.estimated_cost_usd == pytest.approx((1000 * 1.35 + 100 * 5.4) / 1_000_000)
    assert regional_report.summary.estimated_cost_usd == pytest.approx((1000 * 1.485 + 100 * 5.94) / 1_000_000)
    assert global_report.deployments[0].summary.input_price_per_million == 1.35
    assert regional_report.deployments[0].summary.input_price_per_million == 1.485


def test_missing_mode_remains_unknown_and_defers_pricing_and_ptu_sizing():
    value = trace(model="gpt-4.1")
    value.deployment_mode = "unknown"
    report = analyze([value], "fixture", generated_at="2026-09-14T13:00:00Z")
    assessment = report.ptu_analysis.deployments[0]
    assert report.summary.estimated_cost_usd is None
    assert report.deployments[0].summary.deployment_mode == "unknown"
    assert assessment.eligible
    assert assessment.suggested_ptu is None
    assert assessment.economic_result == "Unavailable"
    assert "deployment mode must be Global or Regional" in assessment.note


def test_dated_model_versions_require_literal_or_explicit_alias_matches():
    catalog = PricingCatalog(
        catalog_name="versioned-test-pricing",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="gpt-4o-2024-05-13",
                region="global",
                effective_from="2026-01-01",
                input_per_million=1,
                output_per_million=2,
            ),
            PriceEntry(
                provider="azure_foundry",
                model="gpt-4o-2024-08-06",
                aliases=["gpt-4o-latest"],
                region="global",
                effective_from="2026-01-01",
                input_per_million=3,
                output_per_million=4,
            ),
        ],
    )
    may = analyze(
        [trace(model="gpt-4o-2024-05-13", cached_tokens=0)],
        "fixture",
        customer_catalog=catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    august = analyze(
        [trace(model="gpt-4o-2024-08-06", cached_tokens=0)],
        "fixture",
        customer_catalog=catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    alias = analyze(
        [trace(model="gpt-4o-latest", cached_tokens=0)],
        "fixture",
        customer_catalog=catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    assert may.deployments[0].summary.input_price_per_million == 1
    assert august.deployments[0].summary.input_price_per_million == 3
    assert alias.deployments[0].summary.input_price_per_million == 3
    # An exact dated model absent from the verified PTU capacity catalog is a
    # missing-capacity state, never a claim that Azure does not support it.
    assert may.ptu_analysis.deployments[0].recommendation == "Capacity data required"
    assert may.ptu_analysis.deployments[0].eligibility_status == "model_capacity_unavailable"

    combined = analyze(
        [
            trace(deployment="versioned", model="gpt-4o-2024-05-13", cached_tokens=0),
            trace(deployment="versioned", model="gpt-4o-2024-08-06", cached_tokens=0),
        ],
        "fixture",
        customer_catalog=catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    assert len(combined.deployments) == 2
    assert {item.model_name for item in model_rollups(combined)} == {
        "gpt-4o-2024-05-13",
        "gpt-4o-2024-08-06",
    }


def test_analysis_never_sums_catalogs_with_different_currencies():
    customer = PricingCatalog(
        currency="EUR",
        catalog_name="euro-customer-pricing",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="customer-model",
                region="global",
                effective_from="2026-01-01",
                input_per_million=1,
                output_per_million=1,
            )
        ],
    )
    report = analyze(
        [
            trace(deployment="customer", model="customer-model", cached_tokens=0),
            trace(deployment="reference", model="MAI-DS-R1", cached_tokens=0),
        ],
        "fixture",
        customer_catalog=customer,
        generated_at="2026-09-14T13:00:00Z",
    )
    assert report.summary.pricing_currency == "EUR"
    assert report.summary.estimated_cost_usd == pytest.approx(0.0011)
    assert report.summary.pricing_coverage_requests_percent == 50
    assert next(
        item for item in report.deployments if item.summary.deployment_name == "reference"
    ).summary.estimated_cost_usd is None
    assert "€0.0011" in report_html(report)


def test_reference_snapshot_prices_historical_traces_as_current_estimates():
    report = analyze(
        [trace(model="MAI-DS-R1", cached_tokens=0, timestamp="2026-09-03T12:00:00Z")],
        "fixture",
        generated_at="2026-09-14T13:00:00Z",
    )
    assert report.summary.estimated_cost_usd is not None
    assert report.report_metadata["pricing"]["pricing_basis"] == "analysis_date_snapshot"


def test_cached_input_uses_cached_rate_and_unknown_model_is_not_guessed():
    priced = analyze(
        [trace()],
        "fixture",
        generated_at="2026-09-14T13:00:00Z",
    )
    expected = (900 * 2.0 + 100 * 0.2 + 100 * 8.0) / 1_000_000
    assert priced.summary.estimated_cost_usd == pytest.approx(expected)
    assert priced.summary.fresh_input_cost_usd == pytest.approx(900 * 2.0 / 1_000_000)
    unknown = analyze(
        [trace(model="unknown-model")],
        "fixture",
        generated_at="2026-09-14T13:00:00Z",
    )
    assert unknown.summary.estimated_cost_usd is None
    assert unknown.summary.pricing_coverage_requests_percent == 0
    assert unknown.ptu_analysis.deployments[0].recommendation == "Capacity data required"


def test_ptu_uses_five_minute_series_without_unknown_model_fallback():
    start = datetime(2026, 9, 10, tzinfo=UTC)
    records = [
        trace(
            model="gpt-4.1",
            timestamp=(start + timedelta(minutes=5 * index)).isoformat(),
            input_tokens=5000,
            cached_tokens=0,
            output_tokens=1000,
            latency_ms=6500,
            status_code=429 if index % 10 == 0 else 200,
        )
        for index in range(120)
    ]
    customer = PricingCatalog(
        catalog_name="synthetic-test-pricing",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="gpt-4.1",
                region="global",
                effective_from="2026-01-01",
                input_per_million=2,
                cached_input_per_million=0.5,
                output_per_million=8,
            )
        ],
    )
    report = analyze(
        records,
        "fixture",
        customer_catalog=customer,
        generated_at="2026-09-14T13:00:00Z",
    )
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.data_points == 120
    assert assessment.eligible
    assert assessment.suggested_ptu == 15
    assert assessment.economic_result == "PAYG lower"
    assert assessment.recommendation == "Borderline"
    assert {item.name for item in assessment.dimensions} == {
        "Workload shape",
        "Capacity pressure",
        "Latency sensitivity",
        "Load predictability",
    }
    rendered = report_html(report)
    assert "PTU Advisor" in rendered
    assert "eb0558cd4c6d" in rendered


def test_ptu_hybrid_sweeps_increments_and_selects_lower_cost_baseline():
    start = datetime(2026, 9, 10, tzinfo=UTC)
    records = [
        trace(
            model="gpt-4.1",
            timestamp=(start + timedelta(minutes=5 * index)).isoformat(),
            input_tokens=500_000 if index == 0 else 5_000,
            cached_tokens=0,
            output_tokens=100_000 if index == 0 else 1_000,
        )
        for index in range(120)
    ]
    customer = PricingCatalog(
        catalog_name="synthetic-test-pricing",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="gpt-4.1",
                region="global",
                effective_from="2026-01-01",
                input_per_million=1000,
                cached_input_per_million=250,
                output_per_million=4000,
            )
        ],
    )
    assessment = analyze(
        records,
        "fixture",
        customer_catalog=customer,
        generated_at="2026-09-14T13:00:00Z",
    ).ptu_analysis.deployments[0]
    assert assessment.suggested_ptu > 15
    assert assessment.spillover_percent is not None
    assert assessment.hybrid_monthly_usd is not None


def test_ptu_requires_homogeneous_mode_and_complete_pricing_coverage():
    global_record = trace(
        deployment="shared",
        model="gpt-4.1",
        mode="global",
        cached_tokens=0,
    )
    unknown_record = trace(
        deployment="shared",
        model="gpt-4.1",
        mode="unknown",
        cached_tokens=0,
    )
    split = analyze(
        [unknown_record, global_record],
        "fixture",
        generated_at="2026-09-14T13:00:00Z",
    )
    assert len(split.deployments) == 2
    by_mode = {item.deployment_mode: item for item in split.ptu_analysis.deployments}
    assert by_mode["unknown"].suggested_ptu is None
    assert by_mode["unknown"].economic_result == "Unavailable"

    partial_catalog = PricingCatalog(
        catalog_name="partial-pricing",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="gpt-4.1",
                region="global",
                effective_from="2026-01-01",
                input_per_million=2,
                output_per_million=8,
            )
        ],
    )
    partial = analyze(
        [
            trace(deployment="partial", model="gpt-4.1", cached_tokens=0),
            trace(deployment="partial", model="gpt-4.1", cached_tokens=100),
        ],
        "fixture",
        customer_catalog=partial_catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    ).ptu_analysis.deployments[0]
    assert partial.payg_monthly_usd is None
    assert partial.hybrid_monthly_usd is None
    assert partial.economic_result == "Unavailable"


def test_ptu_uses_exact_completeness_not_rounded_coverage():
    catalog = PricingCatalog(
        catalog_name="mostly-complete-pricing",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="gpt-4.1",
                region="global",
                effective_from="2026-01-01",
                input_per_million=2,
                output_per_million=8,
            )
        ],
    )
    records = [
        trace(
            deployment="rounded",
            model="gpt-4.1",
            cached_tokens=0,
            timestamp=(datetime(2026, 9, 10, tzinfo=UTC) + timedelta(minutes=5 * index)).isoformat(),
        )
        for index in range(2000)
    ]
    records.append(
        trace(
            deployment="rounded",
            model="gpt-4.1",
            cached_tokens=1,
            timestamp=(datetime(2026, 9, 10, tzinfo=UTC) + timedelta(minutes=5 * 2000)).isoformat(),
        )
    )
    report = analyze(
        records,
        "fixture",
        customer_catalog=catalog,
        use_bundled_reference=False,
        generated_at="2026-09-14T13:00:00Z",
    )
    assert report.deployments[0].summary.pricing_coverage_requests_percent == 100.0
    assert not report.deployments[0].summary.pricing_complete
    assert report.ptu_analysis.deployments[0].payg_monthly_usd is None


def test_sparse_elapsed_buckets_remain_insufficient_evidence():
    start = datetime(2026, 9, 10, tzinfo=UTC)
    report = analyze(
        [
            trace(model="gpt-4.1", cached_tokens=0, timestamp=start.isoformat()),
            trace(model="gpt-4.1", cached_tokens=0, timestamp=(start + timedelta(hours=9)).isoformat()),
        ],
        "fixture",
        generated_at="2026-09-14T13:00:00Z",
    )
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.data_points == 109
    assert assessment.observed_buckets == 2
    assert assessment.recommendation == "Insufficient evidence"


def test_same_named_deployments_are_scoped_by_resource_and_keep_slice_findings():
    first = trace(deployment="shared", model="model-a", cached_tokens=0)
    first.resource_name = "resource-a"
    first.retry_of = "prior"
    second = trace(deployment="shared", model="model-b", cached_tokens=0)
    second.resource_name = "resource-b"
    report = analyze(
        [first, second],
        "fixture",
        generated_at="2026-09-14T13:00:00Z",
    )
    assert len(report.deployments) == 2
    assert len(report.ptu_analysis.deployments) == 2
    assert {item.summary.resource_name for item in report.deployments} == {
        "resource-a",
        "resource-b",
    }
    rendered = report_html(report)
    model_a_section = rendered.split("<span>model-a", 1)[1].split("</details>", 1)[0]
    model_b_section = rendered.split("<span>model-b", 1)[1].split("</details>", 1)[0]
    assert "Retry amplification" in model_a_section
    assert "Retry amplification" not in model_b_section


def test_pricing_metadata_and_cost_columns_are_rendered():
    report = analyze(
        [trace()],
        "fixture",
        generated_at="2026-09-14T13:00:00Z",
    )
    rendered = report_html(report)
    assert "microsoft-foundry-reference-2026-09-14" in rendered
    assert "Price I / C / O" in rendered
    assert "Est. cost" in rendered
    assert report.report_metadata["pricing"]["source_url"].endswith("/microsoft/")
