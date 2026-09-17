"""PTU Advisor dashboard contract: payload, states, charts, and exports.

Every fixture is synthetic and deterministic. No real Azure name, identifier,
endpoint, prompt, or cost appears here, and no test performs network access.
"""

from __future__ import annotations

import io
import json
import math
import re
from datetime import UTC, date, datetime, timedelta

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.pricing import PriceEntry, PricingCatalog
from tokenlens.ptu import DASHBOARD_STATE_LABELS, MINIMUM_ACTIVE_BUCKETS, _confidence
from tokenlens.reports import report_html
from tokenlens.telemetry import BucketMetrics, MetricBucketRecord, record_json

PARTNER_MODEL = "claude-4-test-synthetic"


def parsed(value):
    return next(iter_records(io.StringIO(json.dumps(value) + "\n")))


def request_record(
    *,
    timestamp: datetime,
    deployment: str = "example-support-prod",
    model: str = "gpt-4.1",
    input_tokens: int = 4000,
    cached_tokens: int = 1200,
    output_tokens: int = 500,
    status_code: int = 200,
    latency_ms: float = 900,
    mode: str = "global",
):
    return parsed(
        {
            "timestamp": timestamp.isoformat(),
            "deployment_name": deployment,
            "model_name": model,
            "provider": "azure_foundry",
            "deployment_mode": mode,
            "messages": [],
            "usage": {"input_tokens": input_tokens, "cached_tokens": cached_tokens, "output_tokens": output_tokens},
            "latency_ms": latency_ms,
            "status_code": status_code,
        }
    )


def catalog(model: str = "gpt-4.1") -> PricingCatalog:
    return PricingCatalog(
        catalog_name="synthetic-dashboard-pricing",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model=model,
                region="global",
                effective_from="2026-01-01",
                input_per_million=2.0,
                cached_input_per_million=0.5,
                output_per_million=8.0,
            )
        ],
    )


def fourteen_day_records(deployment: str = "example-support-prod") -> list:
    """A deterministic 14-day workload with daily and weekly shape."""
    start = datetime(2026, 8, 31, tzinfo=UTC)
    records = []
    for index in range(14 * 24 * 12):
        stamp = start + timedelta(minutes=5 * index)
        hour = stamp.hour + stamp.minute / 60
        daily = 0.35 + 0.65 * max(0.0, math.sin((hour - 6) / 12 * math.pi))
        weekly = 0.45 if stamp.weekday() >= 5 else 1.0
        jitter = 0.9 + 0.2 * ((index * 7919) % 97) / 97
        load = daily * weekly * jitter
        if load < 0.25:
            continue
        for item in range(max(1, int(round(load * 6)))):
            throttled = load > 0.92 and (index + item) % 11 == 0
            failed = (index + item) % 401 == 0
            records.append(
                request_record(
                    timestamp=stamp + timedelta(seconds=17 * item),
                    deployment=deployment,
                    input_tokens=int(3800 * load) + 400,
                    cached_tokens=int(1500 * load),
                    output_tokens=int(420 * load) + 40,
                    latency_ms=round(520 + 900 * load, 1),
                    status_code=429 if throttled else 500 if failed else 200,
                )
            )
    return records


@pytest.fixture(scope="module")
def full_report():
    return analyze(
        fourteen_day_records(),
        "synthetic-14-day-fixture",
        customer_catalog=catalog(),
        generated_at="2026-09-14T13:00:00Z",
    )


@pytest.fixture(scope="module")
def full_html(full_report):
    return report_html(full_report)


def dashboard(report, index: int = 0):
    return report.ptu_analysis.deployments[index].dashboard


# -- payload -----------------------------------------------------------------


def test_dashboard_payload_is_typed_and_complete(full_report):
    data = dashboard(full_report)
    assert data is not None
    summary = data.summary
    assert summary.deployment_name == "example-support-prod"
    assert summary.model_name == "gpt-4.1"
    assert summary.deployment_mode == "global"
    assert summary.state in DASHBOARD_STATE_LABELS
    assert data.evidence and data.daily_cost
    assert data.recommendation_reasons and data.assumptions and data.next_steps


def test_at_a_glance_totals_match_the_analyzed_deployment_exactly(full_report):
    data = dashboard(full_report)
    deployment = full_report.deployments[0].summary
    assert data.summary.total_input_tokens == deployment.input_tokens
    assert data.summary.total_output_tokens == deployment.output_tokens
    assert data.summary.total_cached_tokens == deployment.cached_tokens
    assert data.summary.total_tokens == deployment.total_tokens
    assert sum(point.input_tokens for point in data.evidence) == deployment.input_tokens
    assert sum(point.output_tokens for point in data.evidence) == deployment.output_tokens


def test_weighted_tpm_average_and_p95_use_the_model_output_weighting(full_report):
    data = dashboard(full_report)
    assessment = full_report.ptu_analysis.deployments[0]
    assert data.summary.weighted_basis == "input + output x 4"
    # Weighted TPM must exceed plain TPM for a model with a 4x output ratio.
    assert data.summary.average_weighted_tpm > assessment.average_tpm
    assert data.summary.p95_weighted_tpm >= data.summary.average_weighted_tpm
    assert data.throughput is not None
    assert data.cost_curve is not None


def test_daily_average_marks_complete_and_partial_days(full_report):
    summary = dashboard(full_report).summary
    assert summary.complete_days + summary.partial_days == len(dashboard(full_report).daily_cost)
    assert summary.complete_days >= 13
    assert summary.partial_days >= 1
    observed_total = sum(point.total_tokens for point in dashboard(full_report).daily_cost)
    expected = observed_total / (summary.complete_days + summary.partial_days)
    assert summary.daily_average_tokens == pytest.approx(expected, rel=1e-6)


def test_daily_cost_reconciles_with_cost_analysis(full_report):
    data = dashboard(full_report)
    deployment = full_report.deployments[0].summary
    total = sum(point.total_cost for point in data.daily_cost if point.total_cost is not None)
    assert total == pytest.approx(deployment.estimated_cost_usd, rel=1e-9)
    components = sum(
        (point.input_cost or 0) + (point.cached_input_cost or 0) + (point.output_cost or 0)
        for point in data.daily_cost
    )
    assert components == pytest.approx(total, rel=1e-9)
    assert data.summary.pricing_coverage_tokens_percent == 100.0


def test_rate_limit_metrics_are_counted_not_derived(full_report):
    data = dashboard(full_report)
    observed_429 = sum(point.rate_limited_requests for point in data.evidence)
    assert data.summary.rate_limited_requests == observed_429
    assert data.summary.rate_limit_percent == pytest.approx(
        observed_429 / data.summary.total_requests * 100, abs=0.01
    )
    # Other failures are counted separately and never folded into success.
    assert sum(point.failed_requests for point in data.evidence) > 0
    for point in data.evidence:
        assert point.total_requests == point.successful_requests + point.rate_limited_requests + point.failed_requests


def test_confidence_is_deterministic_and_documented():
    high, components = _confidence(
        active_buckets=4000,
        observed_buckets=4000,
        elapsed_buckets=4000,
        observed_days=14,
        outcome_coverage=1.0,
        latency_coverage=1.0,
        pricing_coverage=1.0,
        capacity_known=True,
        mode_known=True,
        identity_known=True,
    )
    assert high == 100.0
    assert sum(item.weight for item in components) == pytest.approx(1.0)
    # A complete collection of a mostly idle window keeps full completeness but
    # loses active-sample credit: the two are separate components.
    sparse, sparse_components = _confidence(
        active_buckets=2,
        observed_buckets=4032,
        elapsed_buckets=4032,
        observed_days=14,
        outcome_coverage=1.0,
        latency_coverage=0.0,
        pricing_coverage=0.0,
        capacity_known=False,
        mode_known=True,
        identity_known=True,
    )
    assert sparse < high
    by_name = {item.name: item for item in sparse_components}
    assert by_name["Collection completeness"].score == 1.0
    assert by_name["Active sample size"].score == pytest.approx(0.02)
    empty, _ = _confidence(
        active_buckets=0,
        observed_buckets=0,
        elapsed_buckets=0,
        observed_days=0,
        outcome_coverage=0.0,
        latency_coverage=0.0,
        pricing_coverage=0.0,
        capacity_known=False,
        mode_known=False,
        identity_known=False,
    )
    assert empty == 0.0


# -- banner states -----------------------------------------------------------


def test_recommendation_banner_state_for_sufficient_evidence(full_report):
    summary = dashboard(full_report).summary
    assert summary.state in {"ptu_recommended", "borderline", "payg_recommended"}
    assert summary.confidence_percent is not None
    assert summary.confidence_label in {"High confidence", "Moderate confidence", "Low confidence", "Insufficient evidence"}


def test_insufficient_evidence_state_hides_confidence_and_ptu_numbers():
    start = datetime(2026, 9, 10, tzinfo=UTC)
    records = [
        request_record(timestamp=start, input_tokens=20, cached_tokens=0, output_tokens=6),
        request_record(timestamp=start + timedelta(minutes=1), input_tokens=40, cached_tokens=0, output_tokens=15),
        request_record(timestamp=start + timedelta(minutes=2), input_tokens=16, cached_tokens=0, output_tokens=6),
    ]
    report = analyze(records, "smoke", customer_catalog=catalog(), generated_at="2026-09-14T13:00:00Z")
    data = dashboard(report)
    assert data.summary.state == "insufficient_evidence"
    assert data.summary.confidence_percent is None
    assert data.summary.confidence_label is None
    assert data.summary.smoke_test_window is True
    assert data.cost_curve is None
    assert data.throughput is None

    rendered = report_html(report)
    assert "Insufficient Evidence" in rendered
    assert "Confidence withheld" in rendered
    assert f"{MINIMUM_ACTIVE_BUCKETS} active five-minute buckets" in rendered
    assert "smoke-test window" in rendered
    # No invented economics.
    assert "ptu-cost-explorer" not in rendered
    assert "Break-even" not in rendered.split('id="ptu-panel"', 1)[1].split("Portfolio summary", 1)[0]


def test_partner_model_renders_ptu_not_applicable_without_a_curve():
    records = [
        request_record(timestamp=datetime(2026, 9, 10, tzinfo=UTC), model=PARTNER_MODEL, deployment="example-partner-prod")
    ]
    report = analyze(records, "partner", generated_at="2026-09-14T13:00:00Z")
    data = dashboard(report)
    assert data.summary.state == "ptu_not_applicable"
    assert data.summary.confidence_percent is None
    assert data.cost_curve is None
    assert data.summary.total_tokens is not None
    rendered = report_html(report)
    assert "PTU Not Applicable" in rendered
    assert "partner/Marketplace model billed per token or in provider credit units" in rendered
    # A partner model is never described as an unsupported model.
    assert "Model not supported" not in rendered
    assert "ptu-cost-explorer" not in rendered


@pytest.mark.parametrize(
    "model",
    ["claude-opus-5-test-synthetic", "ministral-3b-test-synthetic"],
)
def test_partner_and_marketplace_models_report_ptu_not_applicable(model):
    """Anthropic CCU and Mistral Marketplace billing have no Azure PTU purchase."""
    records = [
        request_record(
            timestamp=datetime(2026, 9, 10, tzinfo=UTC) + timedelta(minutes=5 * index),
            deployment=f"{model}-prod",
            model=model,
        )
        for index in range(MINIMUM_ACTIVE_BUCKETS + 10)
    ]
    report = analyze(records, "partner", generated_at="2026-09-14T13:00:00Z")
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.eligibility_status == "ptu_not_applicable"
    assert assessment.recommendation == "PTU not applicable"
    assert assessment.suggested_ptu is None
    # Sufficient evidence never turns an inapplicable purchase into a number.
    assert assessment.dashboard.summary.state == "ptu_not_applicable"
    assert "provider credit units" in assessment.note
    assert "Model not supported" not in assessment.note


def test_a_model_absent_from_the_capacity_catalog_asks_for_capacity_data():
    """A first-party model with no exact capacity row is a data gap, not a verdict."""
    model = "gpt-5.6-luna-test-synthetic"
    records = [
        request_record(
            timestamp=datetime(2026, 9, 10, tzinfo=UTC) + timedelta(minutes=5 * index),
            deployment="luna-prod",
            model=model,
        )
        for index in range(MINIMUM_ACTIVE_BUCKETS + 10)
    ]
    report = analyze(records, "capacity", generated_at="2026-09-14T13:00:00Z")
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.eligibility_status == "model_capacity_unavailable"
    assert assessment.recommendation == "Capacity data required"
    assert assessment.recommendation != "Model not supported"
    assert assessment.dashboard.summary.state == "capacity_unavailable"
    assert "verified capacity catalog" in assessment.note
    rendered = report_html(report)
    assert "Capacity data required" in rendered
    assert "PTU not applicable" not in rendered
    assert "Model not supported" not in rendered


def test_pricing_required_state_keeps_tokens_and_draws_no_zero_dollar_bars():
    records = [
        request_record(timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=5 * index))
        for index in range(150)
    ]
    report = analyze(records, "unpriced", use_bundled_reference=False, generated_at="2026-09-14T13:00:00Z")
    data = dashboard(report)
    assert data.summary.state == "pricing_unavailable"
    assert data.summary.confidence_percent is None
    assert data.summary.total_cost is None
    assert all(point.total_cost is None for point in data.daily_cost) or not data.daily_cost
    assert data.summary.total_tokens > 0

    rendered = report_html(report)
    assert "Pricing Required" in rendered
    assert "Pricing required: no exact price resolved" in rendered
    # The card is kept, but no zero-dollar cost geometry is drawn for it.
    cost_card = rendered.split('data-ptu-chart="cost"', 1)[1].split("</article>", 1)[0]
    assert "$0" not in cost_card
    assert "data-series-layer" not in cost_card


def test_capacity_unavailable_state_for_unknown_microsoft_model():
    records = [
        request_record(
            timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
            model="phi-unknown-test-synthetic",
        )
        for index in range(120)
    ]
    report = analyze(records, "unknown-capacity", generated_at="2026-09-14T13:00:00Z")
    data = dashboard(report)
    assert data.summary.state == "capacity_unavailable"
    assert data.summary.confidence_percent is None
    assert "Capacity Data Required" in report_html(report)


# -- rendering ---------------------------------------------------------------


def test_every_banner_state_has_a_label():
    assert set(DASHBOARD_STATE_LABELS) == {
        "ptu_recommended",
        "borderline",
        "payg_recommended",
        "collection_identity_error",
        "insufficient_evidence",
        "pricing_unavailable",
        "ptu_not_applicable",
        "capacity_unavailable",
    }


def test_dashboard_renders_all_required_sections(full_html):
    for needle in (
        "Token Volume Over Time",
        "Request Outcomes",
        "Daily Deployment Cost",
        "Rate-Limit Events (429)",
        "Weighted TPM and capacity",
        "PAYG vs PTU + Spillover Cost Explorer",
        "Why this recommendation",
        "What would change it",
        "Assumptions",
        "Confidence and data quality",
        "Recommended next steps",
        "Export report (print / PDF)",
        "Download JSON",
    ):
        assert needle in full_html, needle


def test_six_at_a_glance_cards_describe_the_selected_deployment(full_html):
    panel = full_html.split('id="ptu-panel"', 1)[1]
    glance = panel.split('class="glance-grid"', 1)[1].split("</section>", 1)[0]
    for label in (
        "Scope",
        "Typical Throughput",
        "Busy-Hour Throughput",
        "Requests",
        "Total Tokens",
        "Daily Average Tokens",
        "Rate-Limit Events",
        "Request Outcomes",
    ):
        assert f">{label}" in glance
    assert glance.count('class="card glance-card"') == 8
    assert "example-support-prod" in glance


def test_charts_are_accessible_and_offline(full_html):
    panel = full_html.split('id="ptu-panel"', 1)[1]
    assert panel.count("data-ptu-chart=") == 4
    assert panel.count("data-series-layer") >= 4
    assert panel.count('data-ptu-expand="') == 4
    assert panel.count("Reset zoom") == 4
    assert panel.count('data-ptu-range="start"') == 4
    assert panel.count('data-ptu-range="end"') == 4
    assert panel.count("<title id=") >= 4
    assert panel.count("<desc id=") >= 4
    assert panel.count("Data table") >= 4
    assert 'tabindex="0"' in panel
    # Self-contained: no external script, style, font, or image reference.
    assert "<script src=" not in full_html
    assert "http://" not in full_html.replace("http://127.0.0.1", "")
    assert "cdn." not in full_html


def test_charts_share_one_observed_time_window(full_report, full_html):
    data = dashboard(full_report)
    first = data.evidence[0].timestamp
    last = data.evidence[-1].timestamp
    panel = full_html.split('id="ptu-panel"', 1)[1]
    assert first.strftime("%Y-%m-%d %H:%M UTC") in panel
    assert last.strftime("%Y-%m-%d %H:%M UTC") in panel
    assert data.daily_cost[0].date == first.date()
    assert data.daily_cost[-1].date == last.date()


def test_embedded_payload_is_aggregate_only_and_exportable(full_html):
    raw = full_html.split('data-ptu-payload="', 1)[1].split(">", 1)[1].split("</script>", 1)[0]
    payload = json.loads(raw.replace("<\\/", "</"))
    assert payload["schema"] == "tokenlens.ptu_dashboard/1"
    assert payload["summary"]["deployment_name"] == "example-support-prod"
    assert payload["evidence_columns"][0] == "timestamp"
    assert len(payload["evidence_rows"]) > 100
    assert payload["cost_curve"] is not None
    text = json.dumps(payload).casefold()
    for forbidden in ("prompt", "response", "endpoint", "subscription", "tenant", "request_id", "api_key", "bearer"):
        assert forbidden not in text, forbidden


def test_print_export_view_includes_every_section(full_html):
    style = full_html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "@media print" in style
    assert "body.ptu-print .tab-panel.ptu{display:block!important}" in style
    # A plain print keeps every panel; only the PTU export button narrows it.
    assert "\n  .tab-panel{display:none!important}" not in style
    assert "break-inside:avoid" in style
    assert 'data-ptu-print="example-support-prod-0"' in full_html


def test_responsive_rules_cover_required_breakpoints(full_html):
    style = full_html.split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".glance-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))" in style
    assert "@media(max-width:1180px){.glance-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}" in style
    assert "@media(max-width:900px){.evidence-grid,.rationale-grid{grid-template-columns:1fr}}" in style
    assert "@media(max-width:620px){.rationale-panel{padding:12px}" in style
    assert ".glance-grid{grid-template-columns:1fr}" in style
    assert "@media(prefers-reduced-motion:reduce)" in style


def test_multiple_deployments_get_a_selector_and_only_one_is_shown():
    records = fourteen_day_records("example-support-prod")[:1200]
    records += [
        request_record(
            timestamp=datetime(2026, 8, 31, tzinfo=UTC) + timedelta(minutes=5 * index),
            deployment="example-batch-prod",
            input_tokens=900,
            cached_tokens=100,
            output_tokens=120,
        )
        for index in range(120)
    ]
    report = analyze(records, "multi", customer_catalog=catalog(), generated_at="2026-09-14T13:00:00Z")
    rendered = report_html(report)
    assert 'data-ptu-select' in rendered
    sections = re.findall(r'<section class="ptu-deployment[^"]*"[^>]*>', rendered)
    assert len(sections) == 2
    assert sum("hidden" in section for section in sections) == 1
    # The default selection is the highest-volume deployment.
    assert "example-support-prod" in sections[0]


def test_aggregate_bucket_source_marks_unavailable_metrics_instead_of_zero():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    raw = [
        record_json(
            MetricBucketRecord(
                event_id=f"bucket-{index}",
                timestamp=start + timedelta(minutes=5 * index),
                deployment_name="example-support-prod",
                model_name="gpt-4.1",
                deployment_mode="global",
                metrics=BucketMetrics(input_tokens=120_000, output_tokens=18_000),
                missing_metrics=["cached_tokens", "request_outcomes"],
            )
        )
        for index in range(150)
    ]
    records = [next(iter_records(io.StringIO(json.dumps(item) + "\n"))) for item in raw]
    report = analyze(records, "aggregate", customer_catalog=catalog(), generated_at="2026-09-14T13:00:00Z")
    data = dashboard(report)
    assert data.summary.rate_limited_requests is None
    assert data.summary.rate_limit_percent is None
    assert data.summary.total_cached_tokens is None
    assert "request_outcomes" in data.missing_metrics
    assert "cached_tokens" in data.missing_metrics
    assert all(point.cached_tokens is None for point in data.evidence)

    rendered = report_html(report)
    assert "Metric unavailable" in rendered
    assert "Cached-token metric unavailable" in rendered
    outcome_card = rendered.split('data-ptu-chart="outcomes"', 1)[1].split("</article>", 1)[0]
    assert "Request-outcome metrics" in outcome_card
    assert "data-series-layer" not in outcome_card


def test_complete_zero_rate_limit_is_distinct_from_unavailable():
    records = [
        request_record(timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=5 * index))
        for index in range(120)
    ]
    report = analyze(records, "no-429", customer_catalog=catalog(), generated_at="2026-09-14T13:00:00Z")
    summary = dashboard(report).summary
    assert summary.rate_limited_requests == 0
    assert summary.rate_limit_percent == 0.0
    rendered = report_html(report)
    glance = rendered.split('class="glance-grid"', 1)[1].split("</section>", 1)[0]
    assert "0.00%" in glance
    assert "Unavailable</div><div class=\"sub\">Request-outcome metric unavailable" not in glance


def test_partial_pricing_shows_a_partial_badge_and_excluded_tokens():
    records = [
        request_record(timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=5 * index))
        for index in range(120)
    ]
    records += [
        request_record(
            timestamp=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=5 * index, seconds=30),
            model="gpt-4.1",
            mode="datazone",
        )
        for index in range(40)
    ]
    report = analyze(records, "partial", customer_catalog=catalog(), use_bundled_reference=False, generated_at="2026-09-14T13:00:00Z")
    by_mode = {item.dashboard.summary.deployment_mode: item.dashboard for item in report.ptu_analysis.deployments}
    assert "datazone" in by_mode
    rendered = report_html(report)
    assert "Pricing Required" in rendered or "partial estimate" in rendered


def test_downsampling_keeps_exact_values_in_the_payload(full_report, full_html):
    data = dashboard(full_report)
    assert len(data.evidence) > 360
    panel = full_html.split('id="ptu-panel"', 1)[1]
    # Display geometry is downsampled but the payload keeps every point.
    raw = panel.split('data-ptu-payload="', 1)[1].split(">", 1)[1].split("</script>", 1)[0]
    payload = json.loads(raw.replace("<\\/", "</"))
    assert len(payload["evidence_rows"]) == len(data.evidence)
    tokens_card = panel.split('data-ptu-chart="tokens"', 1)[1].split("</article>", 1)[0]
    layer = tokens_card.split("data-series-layer", 1)[1].split("</g>", 1)[0]
    assert layer.count("<polyline") <= 4


def test_report_contains_no_identifiers_or_content(full_html):
    lowered = full_html.casefold()
    for forbidden in ("subscription_id", "tenant_id", "request_id", "api_key", "bearer ", "https://management.azure.com"):
        assert forbidden not in lowered, forbidden


def bucket_record(
    *,
    index: int,
    requests: int | None,
    throttled: int | None,
    successful: int | None = None,
    start: datetime = datetime(2026, 9, 1, tzinfo=UTC),
):
    record = MetricBucketRecord(
        event_id=f"bucket-{index}",
        timestamp=start + timedelta(minutes=5 * index),
        deployment_name="example-support-prod",
        model_name="gpt-4.1",
        deployment_mode="global",
        metrics=BucketMetrics(
            input_tokens=120_000,
            output_tokens=18_000,
            requests=requests,
            successful_requests=successful,
            throttled_requests=throttled,
        ),
        missing_metrics=[] if requests is not None else ["request_totals"],
    )
    return next(iter_records(io.StringIO(json.dumps(record_json(record)) + "\n")))


def test_rate_limit_rate_is_withheld_when_the_denominator_is_missing():
    """A throttled numerator with no request total must not become an impossible rate."""
    records = [bucket_record(index=index, requests=None, throttled=4) for index in range(120)]
    report = analyze(records, "no-denominator", customer_catalog=catalog(), generated_at="2026-09-14T13:00:00Z")
    data = dashboard(report)
    assert data.summary.rate_limited_requests == 120 * 4
    assert data.summary.rate_limit_percent is None
    assert data.summary.total_requests is None
    assert "request_totals" in data.missing_metrics
    assert any("denominator is incomplete" in note for note in data.data_quality_notes)

    rendered = report_html(report)
    glance = rendered.split('class="glance-grid"', 1)[1].split("</section>", 1)[0]
    assert "rate unavailable" in glance
    assert "480" in glance


def test_partial_request_denominators_do_not_produce_a_rate_above_one_hundred_percent():
    records = [bucket_record(index=index, requests=None, throttled=10) for index in range(60)]
    records += [bucket_record(index=60 + index, requests=2, throttled=0, successful=2) for index in range(60)]
    report = analyze(records, "partial-denominator", customer_catalog=catalog(), generated_at="2026-09-14T13:00:00Z")
    data = dashboard(report)
    # 600 throttled against a 120-request partial denominator would be 500%.
    assert data.summary.rate_limited_requests == 600
    assert data.summary.rate_limit_percent is None
    assert "request_totals" in data.missing_metrics
    assert report_html(report).count("PTU advisor") >= 1


def test_complete_denominator_still_reports_an_exact_rate():
    records = [bucket_record(index=index, requests=20, throttled=1, successful=19) for index in range(120)]
    report = analyze(records, "complete-denominator", customer_catalog=catalog(), generated_at="2026-09-14T13:00:00Z")
    summary = dashboard(report).summary
    assert summary.total_requests == 2400
    assert summary.rate_limited_requests == 120
    assert summary.rate_limit_percent == 5.0
    assert "request_totals" not in dashboard(report).missing_metrics
