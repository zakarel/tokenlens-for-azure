"""PTU cost-curve/throughput graph generation and sparse-evidence behaviour.

Model IDs are clearly synthetic test fixtures; none are copied from a real
trace.
"""

from datetime import UTC, datetime, timedelta
import io
import json

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.pricing import PriceEntry, PricingCatalog
from tokenlens.reports import report_html

CLAUDE_LIKE_MODEL = "claude-4-test-synthetic"
MINISTRAL_LIKE_MODEL = "ministral-9b-test-synthetic"


def parsed(value):
    return next(iter_records(io.StringIO(json.dumps(value) + "\n")))


def trace(
    *,
    deployment="demo",
    model="gpt-4.1",
    timestamp,
    input_tokens=5000,
    output_tokens=1000,
    status_code=200,
):
    return parsed(
        {
            "timestamp": timestamp,
            "deployment_name": deployment,
            "model_name": model,
            "provider": "azure_foundry",
            "deployment_mode": "global",
            "messages": [],
            "usage": {"input_tokens": input_tokens, "cached_tokens": 0, "output_tokens": output_tokens},
            "latency_ms": 900,
            "status_code": status_code,
        }
    )


def _sufficient_evidence_catalog():
    return PricingCatalog(
        catalog_name="synthetic-ptu-pricing",
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


def _sufficient_evidence_records(count=150):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    return [
        trace(
            deployment="sufficient-demo",
            model="gpt-4.1",
            timestamp=(start + timedelta(minutes=5 * index)).isoformat(),
            input_tokens=5000 + (index % 20) * 250,
            output_tokens=1000,
        )
        for index in range(count)
    ]


def test_sufficient_evidence_cost_curve_matches_engine_scalars_exactly():
    report = analyze(
        _sufficient_evidence_records(),
        "fixture",
        customer_catalog=_sufficient_evidence_catalog(),
        generated_at="2026-09-14T13:00:00Z",
    )
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.eligibility_status == "eligible_sufficient_evidence"
    assert assessment.cost_curve is not None
    assert assessment.throughput_series is not None

    curve = assessment.cost_curve
    # The "current" markers must reuse the exact engine scalars, never a
    # value re-derived from the idealized line.
    assert curve.payg_at_observed_average == round(assessment.payg_monthly_usd, 4)
    assert curve.hybrid_at_observed_average == round(assessment.hybrid_monthly_usd, 4)
    assert curve.selected_ptu == assessment.suggested_ptu
    assert curve.ptu_capacity_tpm == assessment.ptu_capacity_tpm

    # Hand-verify the lower break-even crossing against the same closed-form
    # formula the engine used to decide PTU vs PAYG (reserved / rate-per-tpm).
    rate_per_tpm_month = assessment.payg_monthly_usd / assessment.average_tpm
    if curve.lower_break_even_tpm is not None:
        expected = assessment.ptu_reserved_monthly_usd / rate_per_tpm_month
        # The curve is sampled on a 61-point deterministic grid and the
        # crossing is linearly interpolated between the two bracketing grid
        # points, so it approximates (rather than exactly reproduces) the
        # closed-form algebraic break-even; a tolerance well under one grid
        # step confirms it agrees with the engine's own numbers.
        assert curve.lower_break_even_tpm == pytest.approx(expected, rel=2e-3)

    throughput = assessment.throughput_series
    assert len(throughput.points) == assessment.data_points
    assert throughput.average_tpm == assessment.average_tpm

    rendered = report_html(report)
    assert 'class="columns ptu-throughput"' in rendered
    assert 'class="columns ptu-cost-explorer' in rendered
    assert "Accessible throughput sample" in rendered
    assert "Accessible cost curve sample" in rendered


def test_sparse_three_call_trace_has_no_curve_and_states_insufficient_evidence():
    start = datetime(2026, 9, 10, tzinfo=UTC)
    records = [
        trace(deployment="smoke-a", model="gpt-4.1", timestamp=start.isoformat(), input_tokens=20, output_tokens=6),
        trace(
            deployment="smoke-b",
            model=CLAUDE_LIKE_MODEL,
            timestamp=(start + timedelta(minutes=1)).isoformat(),
            input_tokens=40,
            output_tokens=15,
        ),
        trace(
            deployment="smoke-c",
            model=MINISTRAL_LIKE_MODEL,
            timestamp=(start + timedelta(minutes=2)).isoformat(),
            input_tokens=16,
            output_tokens=6,
        ),
    ]
    report = analyze(records, "fixture", generated_at="2026-09-14T13:19:18Z")
    by_model = {item.model_name: item for item in report.ptu_analysis.deployments}

    gpt = by_model["gpt-4.1"]
    assert gpt.eligibility_status == "eligible_insufficient_evidence"
    assert gpt.recommendation == "Insufficient evidence"
    assert gpt.cost_curve is None
    assert gpt.throughput_series is None

    claude = by_model[CLAUDE_LIKE_MODEL]
    assert claude.eligibility_status == "ptu_not_applicable"
    assert claude.recommendation == "PTU not applicable"
    assert claude.cost_curve is None

    ministral = by_model[MINISTRAL_LIKE_MODEL]
    assert ministral.eligibility_status == "ptu_not_applicable"
    assert ministral.recommendation == "PTU not applicable"

    rendered = report_html(report)
    assert "100 active five-minute buckets" in rendered
    assert "consumption/marketplace offer" in rendered
    # No invented economics for the sparse three-call trace.
    assert 'class="columns ptu-cost-explorer' not in rendered.split("smoke-a", 1)[0]


def test_partner_model_reports_ptu_not_applicable_not_model_not_supported():
    start = datetime(2026, 9, 10, tzinfo=UTC)
    records = [
        trace(deployment="partner-demo", model=CLAUDE_LIKE_MODEL, timestamp=start.isoformat(), input_tokens=100, output_tokens=40)
    ]
    report = analyze(records, "fixture", generated_at="2026-09-14T13:00:00Z")
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.recommendation == "PTU not applicable"
    assert assessment.recommendation != "Model not supported"
    assert assessment.eligible is False
