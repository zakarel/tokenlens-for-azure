"""Azure Monitor collector behaviour with mocked clients.

No Azure SDK is imported, no credential is constructed, and no network call is
made. The fake client returns synthetic metric series shaped like Azure
Monitor's response so paging, retries, partial dimensions, authorization
failures, and idempotency can all be exercised offline.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from tokenlens.analyzer import analyze
from tokenlens.foundry.monitor import (
    CAPABILITY_MATRIX,
    METRIC_MAP,
    AuthorizationError,
    CollectionWindow,
    CollectorError,
    collect_metrics,
    dedupe,
    resource_uri,
    supported_metrics,
)
from tokenlens.foundry.azure_clients import metrics_endpoint_for_location
from tokenlens.ingest import iter_records
from tokenlens.pricing import PriceEntry, PricingCatalog
from tokenlens.telemetry import record_json

FULL_METRICS = [
    "ProcessedPromptTokens",
    "GeneratedTokens",
    "ProcessedCachedPromptTokens",
    "AzureOpenAIRequests",
    "SuccessfulCalls",
    "ThrottledCalls",
    "TotalErrors",
    "NormalizedTimeToFirstByte",
]

WINDOW = CollectionWindow(
    start=datetime(2026, 8, 31, tzinfo=UTC),
    end=datetime(2026, 9, 14, tzinfo=UTC),
    granularity_minutes=5,
)


def test_metrics_endpoint_is_derived_from_the_account_location():
    assert (
        metrics_endpoint_for_location("East US 2")
        == "https://eastus2.metrics.monitor.azure.com"
    )


class FakeStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def series(metric: str, points, *, deployment="example-support-prod", model="gpt-4.1", version="2026-04-14", aggregation="total"):
    metadata = [
        {"name": "ModelDeploymentName", "value": deployment},
        {"name": "ModelName", "value": model},
    ]
    if version:
        metadata.append({"name": "ModelVersion", "value": version})
    return {
        "name": metric,
        "timeseries": [
            {
                "metadata_values": metadata,
                "data": [{"timeStamp": stamp.isoformat(), aggregation: value} for stamp, value in points],
            }
        ],
    }


class FakeMetricsClient:
    """Minimal Azure Monitor double with scripted pages and failures."""

    def __init__(self, pages, failures=None) -> None:
        self.pages = pages
        self.failures = list(failures or [])
        self.calls = []

    def query_resource(self, resource, metric_names, *, timespan, granularity, aggregations, filter=None, page_token=None):
        self.calls.append(
            {
                "resource": resource,
                "metric_names": list(metric_names),
                "timespan": timespan,
                "granularity": granularity,
                "aggregations": list(aggregations),
                "filter": filter,
                "page_token": page_token,
            }
        )
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure
        index = 0 if page_token is None else int(page_token)
        return self.pages[index]


def bucket_pages(bucket_count=200, *, include_outcomes=True, include_cached=True, pages=1):
    start = datetime(2026, 8, 31, tzinfo=UTC)
    stamps = [start + timedelta(minutes=5 * index) for index in range(bucket_count)]
    chunk = max(1, bucket_count // pages)
    result = []
    for page in range(pages):
        window = stamps[page * chunk : (page + 1) * chunk]
        metrics = [
            series("ProcessedPromptTokens", [(stamp, 120_000 + (index % 7) * 5_000) for index, stamp in enumerate(window)]),
            series("GeneratedTokens", [(stamp, 18_000) for stamp in window]),
            series("AzureOpenAIRequests", [(stamp, 60) for stamp in window]),
            series(
                "NormalizedTimeToFirstByte",
                [(stamp, 420.0) for stamp in window],
                aggregation="average",
            ),
        ]
        if include_cached:
            metrics.append(series("ProcessedCachedPromptTokens", [(stamp, 40_000) for stamp in window]))
        if include_outcomes:
            metrics.append(series("SuccessfulCalls", [(stamp, 58) for stamp in window]))
            metrics.append(series("ThrottledCalls", [(stamp, 2) for stamp in window]))
            metrics.append(series("TotalErrors", [(stamp, 0) for stamp in window]))
        payload = {"metrics": metrics}
        if page < pages - 1:
            payload["nextPageToken"] = str(page + 1)
        result.append(payload)
    return result


def collect(client, **overrides):
    kwargs = dict(
        metrics_client=client,
        subscription_id="00000000-0000-0000-0000-000000000000",
        resource_group="example-resource-group",
        account="example-foundry-account",
        window=WINDOW,
        available_metrics=FULL_METRICS,
        deployments=["example-support-prod"],
        deployment_modes={"example-support-prod": "global"},
        sleep=lambda seconds: None,
        now=lambda: datetime(2026, 9, 14, 13, 0, tzinfo=UTC),
    )
    kwargs.update(overrides)
    return collect_metrics(**kwargs)


def test_capability_matrix_and_metric_map_stay_explicit():
    assert set(CAPABILITY_MATRIX) == {"azure_openai", "claude_foundry", "partner_model", "unknown"}
    # Every canonical field the matrix expects must have documented candidates.
    for family, expected in CAPABILITY_MATRIX.items():
        assert expected <= set(METRIC_MAP), family


def test_supported_metrics_never_guesses_an_unavailable_metric():
    resolved, missing = supported_metrics(["ProcessedPromptTokens", "GeneratedTokens"], family="azure_openai")
    assert resolved == {"input_tokens": "ProcessedPromptTokens", "output_tokens": "GeneratedTokens"}
    assert set(missing) == {
        "cached_tokens",
        "requests",
        "successful_requests",
        "throttled_requests",
        "failed_requests",
        "average_latency_ms",
    }
    claude_resolved, claude_missing = supported_metrics(["ModelRequests"], family="claude_foundry")
    assert claude_resolved == {"requests": "ModelRequests"}
    assert set(claude_missing) == {"input_tokens", "output_tokens", "throttled_requests"}


def test_collection_produces_five_minute_buckets_with_no_identifiers():
    client = FakeMetricsClient(bucket_pages(200))
    result = collect(client)
    assert len(result.records) == 200
    assert result.pages_read == 1
    record = result.records[0]
    assert record.record_type == "foundry_metric_bucket"
    assert record.bucket_minutes == 5
    assert record.deployment_name == "example-support-prod"
    # The version dimension stays separate: no combined model ID is invented.
    assert record.model_name == "gpt-4.1"
    assert record.model_version == "2026-04-14"
    assert record.deployment_mode == "global"
    assert record.metrics.input_tokens == 120_000
    assert record.metrics.throttled_requests == 2
    # Azure Monitor reports an average, so the percentile fields stay empty.
    assert record.metrics.average_latency_ms == 420.0
    assert record.metrics.p50_latency_ms is None
    serialized = json.dumps(record_json(record))
    assert "subscriptions" not in serialized
    assert "example-foundry-account" not in serialized
    assert "example-resource-group" not in serialized
    # The query itself is scoped to the selected account only.
    assert client.calls[0]["resource"] == resource_uri(
        "00000000-0000-0000-0000-000000000000", "example-resource-group", "example-foundry-account"
    )
    assert client.calls[0]["filter"] == "ModelDeploymentName eq 'example-support-prod'"


def test_paging_is_followed_to_completion():
    client = FakeMetricsClient(bucket_pages(120, pages=3))
    result = collect(client)
    assert result.pages_read == 3
    assert len(result.records) == 120
    assert [call["page_token"] for call in client.calls] == [None, "1", "2"]


def test_only_retryable_responses_are_retried():
    client = FakeMetricsClient(bucket_pages(10), failures=[FakeStatusError(429), FakeStatusError(503), None])
    result = collect(client)
    assert result.retries == 2
    assert len(result.records) == 10

    fatal = FakeMetricsClient(bucket_pages(10), failures=[FakeStatusError(400)])
    with pytest.raises(CollectorError):
        collect(fatal)
    assert len(fatal.calls) == 1


def test_authorization_failures_are_explicit():
    client = FakeMetricsClient(bucket_pages(10), failures=[FakeStatusError(403)])
    with pytest.raises(AuthorizationError):
        collect(client)


def test_missing_metrics_are_reported_not_zero_filled():
    client = FakeMetricsClient(bucket_pages(120, include_outcomes=False, include_cached=False))
    available = [name for name in FULL_METRICS if name not in {"SuccessfulCalls", "ThrottledCalls", "TotalErrors", "ProcessedCachedPromptTokens"}]
    result = collect(client, available_metrics=available)
    assert "cached_tokens" in result.missing_metrics
    assert "throttled_requests" in result.missing_metrics
    record = result.records[0]
    assert record.metrics.cached_tokens is None
    assert record.metrics.throttled_requests is None
    assert record.metrics.successful_requests is None
    assert "cached_tokens" in record.missing_metrics


def test_null_data_points_are_skipped_rather_than_recorded_as_zero():
    start = datetime(2026, 8, 31, tzinfo=UTC)
    payload = {
        "metrics": [
            {
                "name": "ProcessedPromptTokens",
                "timeseries": [
                    {
                        "metadata_values": [
                            {"name": "ModelDeploymentName", "value": "example-support-prod"},
                            {"name": "ModelName", "value": "gpt-4.1"},
                        ],
                        "data": [
                            {"timeStamp": start.isoformat(), "total": 1000},
                            {"timeStamp": (start + timedelta(minutes=5)).isoformat()},
                            {"timeStamp": (start + timedelta(minutes=10)).isoformat(), "total": 2000},
                        ],
                    }
                ],
            }
        ]
    }
    result = collect(FakeMetricsClient([payload]), available_metrics=["ProcessedPromptTokens"])
    assert len(result.records) == 2
    assert [record.metrics.input_tokens for record in result.records] == [1000, 2000]


def test_partial_dimensions_are_preserved_as_unknown():
    start = datetime(2026, 8, 31, tzinfo=UTC)
    payload = {
        "metrics": [
            {
                "name": "ProcessedPromptTokens",
                "timeseries": [
                    {
                        "metadata_values": [{"name": "ModelDeploymentName", "value": "example-support-prod"}],
                        "data": [{"timeStamp": start.isoformat(), "total": 5000}],
                    }
                ],
            }
        ]
    }
    result = collect(FakeMetricsClient([payload]), available_metrics=["ProcessedPromptTokens"])
    assert result.records[0].model_name == "unknown"
    assert result.records[0].deployment_name == "example-support-prod"


def test_reruns_are_idempotent_through_deterministic_bucket_keys():
    first = collect(FakeMetricsClient(bucket_pages(50)))
    second = collect(FakeMetricsClient(bucket_pages(50)))
    assert [record.event_id for record in first.records] == [record.event_id for record in second.records]
    assert len(dedupe(first.records + second.records)) == 50


def test_same_named_deployments_in_different_accounts_have_distinct_event_ids():
    first = collect(FakeMetricsClient(bucket_pages(1)), account="example-account-a")
    second = collect(FakeMetricsClient(bucket_pages(1)), account="example-account-b")
    assert first.records[0].event_id != second.records[0].event_id
    assert len(dedupe(first.records + second.records)) == 2


def test_collection_without_any_known_metric_is_an_error():
    with pytest.raises(CollectorError):
        collect(FakeMetricsClient([{"metrics": []}]), available_metrics=["SomeUnrelatedMetric"])


def test_collected_buckets_analyze_into_a_ptu_dashboard():
    result = collect(FakeMetricsClient(bucket_pages(240)))
    records = [
        next(iter_records(io.StringIO(json.dumps(record_json(item)) + "\n"))) for item in result.records
    ]
    catalog = PricingCatalog(
        catalog_name="synthetic-monitor-pricing",
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
    report = analyze(records, "monitor", customer_catalog=catalog, generated_at="2026-09-14T13:00:00Z")
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.observed_buckets == 240
    assert assessment.eligibility_status == "eligible_sufficient_evidence"
    data = assessment.dashboard
    assert data.summary.rate_limited_requests == 240 * 2
    assert data.summary.total_cached_tokens == 240 * 40_000
    assert data.throughput is not None
    assert data.cost_curve is not None


def test_three_request_smoke_fixture_stays_insufficient():
    client = FakeMetricsClient(bucket_pages(3))
    result = collect(client)
    records = [
        next(iter_records(io.StringIO(json.dumps(record_json(item)) + "\n"))) for item in result.records
    ]
    report = analyze(records, "smoke", generated_at="2026-09-14T13:00:00Z")
    assessment = report.ptu_analysis.deployments[0]
    assert assessment.recommendation == "Insufficient evidence"
    assert assessment.dashboard.summary.state == "insufficient_evidence"
    assert assessment.dashboard.summary.confidence_percent is None


def test_request_level_and_aggregate_fixtures_agree_on_throughput():
    aggregate = collect(FakeMetricsClient(bucket_pages(150)))
    aggregate_records = [
        next(iter_records(io.StringIO(json.dumps(record_json(item)) + "\n"))) for item in aggregate.records
    ]
    request_records = []
    for item in aggregate.records:
        # Same bucket totals expressed as one request-level event per bucket.
        request_records.append(
            next(
                iter_records(
                    io.StringIO(
                        json.dumps(
                            {
                                "timestamp": item.timestamp.isoformat(),
                                "deployment_name": item.deployment_name,
                                "model_name": item.model_name,
                                "provider": "azure_foundry",
                                "deployment_mode": "global",
                                "messages": [],
                                "usage": {
                                    "input_tokens": item.metrics.input_tokens,
                                    "cached_tokens": item.metrics.cached_tokens,
                                    "output_tokens": item.metrics.output_tokens,
                                },
                                "status_code": 200,
                            }
                        )
                        + "\n"
                    )
                )
            )
        )
    aggregate_report = analyze(aggregate_records, "aggregate", generated_at="2026-09-14T13:00:00Z")
    request_report = analyze(request_records, "requests", generated_at="2026-09-14T13:00:00Z")
    left = aggregate_report.ptu_analysis.deployments[0]
    right = request_report.ptu_analysis.deployments[0]
    # Aggregate analysis uses the elapsed-window basis for headline economics;
    # compare the active-window throughput with the request-level fixture.
    assert left.dashboard.summary.active_average_weighted_tpm == pytest.approx(
        right.dashboard.summary.active_average_weighted_tpm
    )
    assert left.dashboard.summary.active_p95_weighted_tpm == pytest.approx(
        right.dashboard.summary.active_p95_weighted_tpm
    )
    assert left.observed_buckets == right.observed_buckets


def test_collection_window_helper_rejects_nonpositive_lookback():
    with pytest.raises(CollectorError):
        CollectionWindow.for_days(0)
    window = CollectionWindow.for_days(14, now=datetime(2026, 9, 14, 13, 7, 30, tzinfo=UTC))
    assert window.end == datetime(2026, 9, 14, 13, 7, tzinfo=UTC)
    assert (window.end - window.start).days == 14


def test_deployment_mode_applies_to_unrestricted_collections():
    """A confirmed mode must reach every bucket, not only explicitly named deployments."""
    client = FakeMetricsClient(bucket_pages(20))
    result = collect(client, deployments=None, deployment_modes=None, default_deployment_mode="global")
    assert result.records
    assert {record.deployment_mode for record in result.records} == {"global"}
    assert client.calls[0]["filter"] == "ModelDeploymentName eq '*'"


def test_deployment_mode_default_stays_unknown_when_not_supplied():
    result = collect(FakeMetricsClient(bucket_pages(5)), deployments=None, deployment_modes=None)
    assert {record.deployment_mode for record in result.records} == {"unknown"}


def test_explicit_per_deployment_modes_win_over_the_default():
    result = collect(
        FakeMetricsClient(bucket_pages(5)),
        deployments=None,
        deployment_modes={"example-support-prod": "regional"},
        default_deployment_mode="global",
    )
    assert {record.deployment_mode for record in result.records} == {"regional"}


def test_collected_window_is_idempotent_against_an_existing_output_file(tmp_path):
    from tokenlens.telemetry import TelemetryConfig, TelemetryWriter

    config = TelemetryConfig(output_dir=tmp_path / "metrics", retention_days=0)
    first_run = collect(FakeMetricsClient(bucket_pages(40)))
    assert TelemetryWriter(config).write_all(first_run.records) == 40

    # Re-running an overlapping window appends only the buckets that are new.
    second_run = collect(FakeMetricsClient(bucket_pages(55)))
    writer = TelemetryWriter(config)
    assert writer.write_all(second_run.records) == 15
    assert writer.skipped_duplicates == 40
    lines = sum(
        len(path.read_text(encoding="utf-8").splitlines())
        for path in (tmp_path / "metrics").glob("*.jsonl")
    )
    assert lines == 55
