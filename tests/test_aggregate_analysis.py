"""Aggregate telemetry semantics, diagnostic applicability, and PTU evidence.

The fixture reproduces the *shape* of a sparse 14-day Azure Monitor collection
with entirely synthetic values: 4,032 elapsed five-minute buckets, two active
buckets, 45 tokens, and four requests split across HTTP 200 and HTTP 400. No
real deployment, resource, endpoint, or metric value appears here.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from tokenlens.aggregate import MixedTelemetryError, aggregate_summary
from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.reports import report_html
from tokenlens.telemetry.schema import BucketMetrics, MetricBucketRecord, record_json

WINDOW_START = datetime(2026, 8, 31, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(days=14)
ELAPSED_BUCKETS = 14 * 24 * 12  # 4,032 five-minute intervals


def bucket(
    *,
    index: int,
    input_tokens: int | None,
    output_tokens: int | None,
    status_codes: dict[str, int] | None,
    cached: int | None = None,
    deployment: str = "chat-route",
    model: str = "gpt-4.1",
    mode: str = "global",
):
    requests = sum(status_codes.values()) if status_codes is not None else None
    successful = sum(count for code, count in (status_codes or {}).items() if code.startswith("2"))
    rate_limited = (status_codes or {}).get("429", 0)
    other = (requests or 0) - successful - rate_limited
    record = MetricBucketRecord(
        event_id=f"synthetic-bucket-{deployment}-{index}",
        timestamp=WINDOW_START + timedelta(minutes=5 * index),
        deployment_name=deployment,
        model_name=model,
        model_version="2026-04-14",
        deployment_mode=mode,
        metrics=BucketMetrics(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            requests=requests,
            successful_requests=successful if status_codes is not None else None,
            throttled_requests=rate_limited if status_codes is not None else None,
            failed_requests=other if status_codes is not None else None,
        ),
        missing_metrics=["cached_tokens"] if cached is None else [],
        metric_provenance={"input_tokens": "InputTokens", "output_tokens": "OutputTokens", "requests": "ModelRequests"},
        status_codes=status_codes,
        outcome_coverage="complete" if status_codes is not None else "unavailable",
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        expected_buckets=ELAPSED_BUCKETS,
    )
    return next(iter_records(io.StringIO(json.dumps(record_json(record)) + "\n")))


def audited_records(*, zero_filled: bool = True):
    """Two active buckets inside a complete, mostly idle 14-day timeline."""
    records = [
        bucket(index=1, input_tokens=18, output_tokens=9, status_codes={"200": 1, "400": 1}),
        bucket(index=2, input_tokens=12, output_tokens=6, status_codes={"200": 1, "400": 1}),
    ]
    if zero_filled:
        records += [
            bucket(index=index, input_tokens=0, output_tokens=0, status_codes={})
            for index in range(3, 40)
        ]
    return records


@pytest.fixture()
def audited_report():
    return analyze(audited_records(), "2 local metric files", generated_at="2026-09-14T13:00:00Z")


def test_requests_come_from_the_metric_not_the_bucket_count(audited_report):
    summary = audited_report.summary
    assert summary.analysis_unit == "metric_buckets"
    assert summary.requests_observed == 4
    assert summary.total_tokens == 45
    assert summary.input_tokens == 30
    assert summary.output_tokens == 15
    # 45 tokens over four actual requests, never over a bucket count.
    assert summary.average_tokens_per_request == 11.25


def test_active_observed_and_elapsed_buckets_stay_distinct(audited_report):
    aggregate = audited_report.summary.aggregate
    assert aggregate is not None
    assert aggregate.active_buckets == 2
    assert aggregate.observed_buckets == 39
    assert aggregate.elapsed_buckets == ELAPSED_BUCKETS
    assert aggregate.idle_buckets == 37
    # Collection completeness is observed versus expected, never the activity rate.
    assert aggregate.collection_completeness_percent == pytest.approx(0.97, abs=0.05)


def test_zero_idle_buckets_are_not_active():
    summary = aggregate_summary(
        [bucket(index=index, input_tokens=0, output_tokens=0, status_codes={}) for index in range(50)]
    )
    assert summary.observed_buckets == 50
    assert summary.active_buckets == 0
    assert summary.requests_observed == 0


def test_retries_are_unavailable_rather_than_zero_or_bucket_count(audited_report):
    assert audited_report.summary.retries_available is False
    assert audited_report.summary.retries == 0
    rendered = report_html(audited_report)
    assert "retries observed" not in rendered


def test_missing_request_metric_is_unavailable_not_zero():
    records = [
        bucket(index=index, input_tokens=100, output_tokens=20, status_codes=None)
        for index in range(1, 5)
    ]
    report = analyze(records, "1 local metric file", generated_at="2026-09-14T13:00:00Z")
    assert report.summary.requests_available is False
    assert report.summary.requests_observed is None
    assert report.summary.average_tokens_per_request is None
    assert any(item.code == "requests_unavailable" for item in report.data_quality)
    assert "Unavailable" in report_html(report)


def test_cached_tokens_stay_unavailable_when_the_metric_is_missing(audited_report):
    assert audited_report.summary.cached_tokens_available is False
    assert audited_report.summary.aggregate.cached_tokens is None
    rendered = report_html(audited_report)
    assert "cached metric unavailable" in rendered


def test_complete_status_coverage_reports_a_genuine_zero_rate_limit(audited_report):
    aggregate = audited_report.summary.aggregate
    assert aggregate.outcome_coverage == "complete"
    assert aggregate.successful_requests == 2
    assert aggregate.failed_requests == 2
    assert aggregate.rate_limited_requests == 0
    assert aggregate.status_codes == {"200": 2, "400": 2}


def test_mixed_sources_are_rejected_by_default():
    request_record = next(
        iter_records(
            io.StringIO(
                json.dumps(
                    {
                        "timestamp": "2026-09-01T00:05:00Z",
                        "deployment_name": "chat-route",
                        "model_name": "gpt-4.1",
                        "deployment_mode": "global",
                        "messages": [],
                        "usage": {"input_tokens": 18, "output_tokens": 9},
                    }
                )
                + "\n"
            )
        )
    )
    mixed = audited_records(zero_filled=False) + [request_record]
    with pytest.raises(MixedTelemetryError):
        analyze(mixed, "mixed", generated_at="2026-09-14T13:00:00Z")
    # The explicit merge policy is available once non-overlap has been proven.
    merged = analyze(mixed, "mixed", generated_at="2026-09-14T13:00:00Z", mixed_source_policy="allow")
    assert merged.summary.total_tokens == 45 + 27


def test_aggregate_telemetry_produces_no_request_only_findings(audited_report):
    assert audited_report.findings == []
    statuses = {item.rule_id: item.status for item in audited_report.diagnostics.evaluations}
    for rule_id in ("TL001", "TL002", "TL003", "TL004", "TL005", "TL006", "TL007", "TL008"):
        assert statuses[rule_id] == "not_evaluated", rule_id
    assert audited_report.summary.findings == 0
    assert audited_report.summary.not_evaluated_rules == 8


def test_empty_content_never_becomes_a_retry_finding():
    """Identical empty message lists must not hash into a retry cluster."""
    records = [
        next(
            iter_records(
                io.StringIO(
                    json.dumps(
                        {
                            "timestamp": (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
                            "deployment_name": "chat-route",
                            "model_name": "gpt-4.1",
                            "deployment_mode": "global",
                            "messages": [],
                            "usage": {"input_tokens": 10, "output_tokens": 4},
                        }
                    )
                    + "\n"
                )
            )
        )
        for index in range(20)
    ]
    report = analyze(records, "contentless requests", generated_at="2026-09-14T13:00:00Z")
    assert not [item for item in report.findings if item.rule_id == "TL005"]
    statuses = {item.rule_id: item.status for item in report.diagnostics.evaluations}
    assert statuses["TL005"] == "not_evaluated"


def test_unknown_model_never_produces_model_sizing():
    records = [
        next(
            iter_records(
                io.StringIO(
                    json.dumps(
                        {
                            "timestamp": (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
                            "deployment_name": "chat-route",
                            "model_name": "unknown",
                            "deployment_mode": "global",
                            "messages": [{"role": "user", "content": "classify this"}],
                            "usage": {"input_tokens": 10, "output_tokens": 4},
                        }
                    )
                    + "\n"
                )
            )
        )
        for index in range(20)
    ]
    report = analyze(records, "unknown model", generated_at="2026-09-14T13:00:00Z")
    assert not [item for item in report.findings if item.rule_id == "TL008"]


def test_explicit_retry_metadata_still_supports_a_retry_finding():
    records = [
        next(
            iter_records(
                io.StringIO(
                    json.dumps(
                        {
                            "timestamp": (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
                            "deployment_name": "chat-route",
                            "model_name": "gpt-4.1",
                            "deployment_mode": "global",
                            "messages": [],
                            "usage": {"input_tokens": 500, "output_tokens": 40},
                            "retry_of": "request-0",
                        }
                    )
                    + "\n"
                )
            )
        )
        for index in range(6)
    ]
    report = analyze(records, "explicit retries", generated_at="2026-09-14T13:00:00Z")
    assert [item for item in report.findings if item.rule_id == "TL005"]


def test_finding_counts_reconcile_with_the_applicability_registry(audited_report):
    coverage = audited_report.diagnostics
    assert coverage.findings + coverage.no_issue + coverage.not_evaluated == 8
    assert coverage.findings == len(audited_report.findings)


def test_sparse_evidence_yields_insufficient_evidence_not_a_ptu_verdict(audited_report):
    assessment = audited_report.ptu_analysis.deployments[0]
    data = assessment.dashboard
    assert data.summary.state == "insufficient_evidence"
    assert data.summary.active_buckets == 2
    assert data.summary.elapsed_buckets == ELAPSED_BUCKETS
    assert data.summary.total_requests == 4
    assert assessment.suggested_ptu is None
    assert assessment.cost_curve is None
    assert data.summary.confidence_percent is None
    verdicts = {item.name: item.verdict for item in assessment.dimensions}
    assert set(verdicts.values()) == {"insufficient"}
    assert "100 are required" in " ".join(data.recommendation_reasons)


def test_tiny_nonzero_throughput_uses_adaptive_precision(audited_report):
    rendered = report_html(audited_report)
    glance = rendered.split('class="glance-grid"', 1)[1].split("</section>", 1)[0]
    assert "&lt;0.01" in glance
    assert "Insufficient sample" in glance


def test_overview_widgets_reconcile_with_the_aggregate_summary(audited_report):
    rendered = report_html(audited_report)
    overview = rendered.split('id="overview-panel"', 1)[1].split('id="usage-panel"', 1)[0]
    assert ">4</div>" in overview
    assert "4,032" not in overview.replace("4,032 elapsed", "")
    assert "2 active of 4,032 elapsed" in overview
    assert "1 deployment" in overview
    assert "Single selected route" in overview
    assert "Request-level optimisation not evaluated" in overview


def test_report_states_data_quality_before_the_kpis(audited_report):
    rendered = report_html(audited_report)
    assert rendered.index('class="data-quality') < rendered.index('id="overview-panel"')
    assert "Cached-token metric unavailable" in rendered


def test_report_never_renders_local_source_paths():
    records = audited_records(zero_filled=False)
    report = analyze(
        records,
        "/private/local-traces/foundry-metrics/metrics-2026-09-14.jsonl",
        generated_at="2026-09-14T13:00:00Z",
        source_files=15,
    )
    rendered = report_html(report)
    header = rendered.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "/private/" not in header
    assert "15 local metric files" in header
    assert "14-day window" in header
    assert "Local offline analysis" in header


def test_missing_token_metric_is_unavailable_not_zero():
    """A source with no input-token series must not report zero input tokens."""
    records = [
        bucket(index=index, input_tokens=None, output_tokens=40, status_codes={"200": 2})
        for index in range(1, 5)
    ]
    report = analyze(records, "1 local metric file", generated_at="2026-09-14T13:00:00Z")
    assert report.summary.input_tokens_available is False
    assert report.summary.output_tokens_available is True
    assert report.summary.aggregate.input_tokens is None
    assert any(item.code == "input_tokens_unavailable" for item in report.data_quality)
    rendered = report_html(report)
    assert "input metric unavailable" in rendered
    assert "partial" in rendered.split('class="metrics"', 1)[1].split("</section>", 1)[0]


def test_pricing_coverage_never_exceeds_one_hundred_percent():
    """Coverage numerator and denominator must describe the same population."""
    from tokenlens.presentation import model_rollups
    from tokenlens.pricing import PriceEntry, PricingCatalog

    catalog = PricingCatalog(
        catalog_name="synthetic-coverage-catalog",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="gpt-4.1",
                region="global",
                effective_from="2026-01-01",
                input_per_million=2.0,
                cached_input_per_million=0.5,
                output_per_million=8.0,
            )
        ],
    )
    records = [
        bucket(index=1, input_tokens=18, output_tokens=9, status_codes=None),
        bucket(index=2, input_tokens=12, output_tokens=6, status_codes=None),
    ]
    records += [bucket(index=index, input_tokens=0, output_tokens=0, status_codes=None) for index in range(3, 31)]
    report = analyze(records, "1 local metric file", customer_catalog=catalog, generated_at="2026-09-14T13:00:00Z")
    for item in model_rollups(report):
        assert 0 <= item.pricing_coverage_requests_percent <= 100


def test_insufficient_evidence_withholds_ptu_economics_even_when_pricing_resolves():
    """A resolved price must never unlock a PTU amount the evidence cannot support."""
    from tokenlens.pricing import PriceEntry, PricingCatalog

    catalog = PricingCatalog(
        catalog_name="synthetic-ptu-catalog",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model="gpt-4.1",
                region="global",
                effective_from="2026-01-01",
                input_per_million=2.0,
                cached_input_per_million=0.5,
                output_per_million=8.0,
            )
        ],
    )
    records = [
        bucket(index=index, input_tokens=1200, output_tokens=300, cached=0, status_codes={"200": 4})
        for index in range(1, 21)
    ]
    report = analyze(records, "1 local metric file", customer_catalog=catalog, generated_at="2026-09-14T13:00:00Z")
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.eligibility_status == "eligible_insufficient_evidence"
    assert assessment.suggested_ptu is None
    assert assessment.payg_monthly_usd is None
    assert assessment.hybrid_monthly_usd is None
    assert assessment.economic_result == "Unavailable"
    rendered = report_html(report)
    portfolio = rendered.split("Portfolio summary", 1)[1]
    assert "Unavailable" in portfolio
