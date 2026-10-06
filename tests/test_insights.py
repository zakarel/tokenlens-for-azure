from tokenlens.insights import build_aggregate_insights
from tokenlens.models import AggregateAnalysisSummary
from tokenlens.analyzer import analyze


def test_incomplete_collection_produces_actionable_evidence():
    summary = AggregateAnalysisSummary(
        elapsed_buckets=100,
        observed_buckets=40,
        requests_observed=10,
        input_tokens=100,
        output_tokens=50,
        input_tokens_state="measured",
        output_tokens_state="measured",
        total_tokens_state="measured",
    )
    insights = build_aggregate_insights(summary)
    assert insights[0].insight_id == "AGG-COVERAGE"
    assert insights[0].recommendation
    assert insights[0].verification


def test_rate_limit_insight_is_not_a_ptu_verdict():
    summary = AggregateAnalysisSummary(
        elapsed_buckets=10,
        observed_buckets=10,
        requests_observed=100,
        rate_limited_requests=20,
        input_tokens=100,
        output_tokens=50,
        input_tokens_state="measured",
        output_tokens_state="measured",
        total_tokens_state="measured",
    )
    insight = next(item for item in build_aggregate_insights(summary) if item.insight_id == "AGG-RATE-LIMIT")
    assert "PTU" in " ".join(insight.caveats)


def test_insights_are_present_in_report_metadata_for_json_and_html():
    report = analyze([], "synthetic")
    assert isinstance(report.report_metadata["insights"], list)
