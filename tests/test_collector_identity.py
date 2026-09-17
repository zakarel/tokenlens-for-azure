"""Collector identity, dimension, and outcome normalization.

Everything here is synthetic. The fixtures reproduce the *shapes* the Azure SDK
returns — a ``metadata_values`` dictionary, legacy metadata objects, raw REST
dictionaries, and status-code split series — without any real endpoint, resource
name, deployment name, or metric value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tokenlens.foundry.dimensions import normalize_dimensions
from tokenlens.foundry.metrics_catalog import MetricDefinition, select_metrics
from tokenlens.foundry.monitor import (
    CollectionWindow,
    CollectorError,
    IdentityError,
    collect_metrics,
    reconcile_inventory,
)

WINDOW = CollectionWindow(
    start=datetime(2026, 8, 31, tzinfo=UTC),
    end=datetime(2026, 9, 14, tzinfo=UTC),
    granularity_minutes=5,
)
START = WINDOW.start


class MetadataValue:
    """The legacy SDK shape: an object with ``name.value`` and ``value``."""

    class _Name:
        def __init__(self, value: str) -> None:
            self.value = value

    def __init__(self, name: str, value: str) -> None:
        self.name = self._Name(name)
        self.value = value


class FakeClient:
    def __init__(self, responses: dict[str, dict] | list[dict]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def query_resource(self, resource, metric_names, *, timespan, granularity, aggregations, filter=None, page_token=None):
        self.calls.append({"metric_names": list(metric_names), "filter": filter, "aggregations": list(aggregations)})
        if isinstance(self.responses, list):
            return self.responses[len(self.calls) - 1]
        return self.responses.get(filter or "", {"metrics": []})


def collect(client, **overrides):
    kwargs = dict(
        metrics_client=client,
        subscription_id="00000000-0000-0000-0000-000000000000",
        resource_group="example-resource-group",
        account="example-account",
        window=WINDOW,
        available_metrics=[],
        sleep=lambda seconds: None,
        now=lambda: datetime(2026, 9, 14, 13, 0, tzinfo=UTC),
    )
    kwargs.update(overrides)
    return collect_metrics(**kwargs)


def definition(name: str, dimensions: list[str]) -> MetricDefinition:
    return MetricDefinition(name=name, dimensions=frozenset(dimensions), dimensions_known=True)


LIVE_DEFINITIONS = [
    definition("InputTokens", ["ModelDeploymentName", "ModelName", "ModelVersion"]),
    definition("OutputTokens", ["ModelDeploymentName", "ModelName", "ModelVersion"]),
    definition("ModelRequests", ["ModelDeploymentName", "ModelName", "ModelVersion", "StatusCode"]),
    definition("SuccessfulCalls", ["ApiName", "OperationName", "StatusCode"]),
    definition("TotalErrors", ["ApiName", "OperationName", "StatusCode"]),
    definition("Latency", ["ApiName", "OperationName"]),
]


def point(stamp: datetime, value: float, aggregation: str = "total") -> dict:
    return {"timeStamp": stamp.isoformat(), aggregation: value}


def dict_series(metric: str, dimensions: dict, points: list[dict]) -> dict:
    """The installed QueryMetrics shape: ``metadata_values`` is a dictionary."""
    return {"name": metric, "timeseries": [{"metadata_values": dimensions, "data": points}]}


def audited_responses() -> dict[str, dict]:
    """The audited window: 4,032 elapsed buckets, two active, four requests."""
    active = [START + timedelta(minutes=5), START + timedelta(minutes=10)]
    token_filter = "ModelDeploymentName eq 'chat-route'"
    status_filter = "ModelDeploymentName eq 'chat-route' and StatusCode eq '*'"
    return {
        token_filter: {
            "metrics": [
                dict_series(
                    "InputTokens",
                    {"modeldeploymentname": "chat-route", "modelname": "gpt-4.1", "modelversion": "2026-04-14"},
                    [point(active[0], 18), point(active[1], 12)],
                ),
                dict_series(
                    "OutputTokens",
                    {"modeldeploymentname": "chat-route", "modelname": "gpt-4.1", "modelversion": "2026-04-14"},
                    [point(active[0], 9), point(active[1], 6)],
                ),
            ]
        },
        status_filter: {
            "metrics": [
                {
                    "name": "ModelRequests",
                    "timeseries": [
                        {
                            "metadata_values": {"modeldeploymentname": "chat-route", "statuscode": "200"},
                            "data": [point(active[0], 1), point(active[1], 1)],
                        },
                        {
                            "metadata_values": {"modeldeploymentname": "chat-route", "statuscode": "400"},
                            "data": [point(active[0], 1), point(active[1], 1)],
                        },
                    ],
                }
            ]
        },
    }


def test_query_metrics_dictionary_dimensions_resolve_identity():
    raw = {"modeldeploymentname": "chat-route", "ModelName": "gpt-4.1", "unexpected": "value"}
    assert normalize_dimensions(raw) == {
        "ModelDeploymentName": "chat-route",
        "ModelName": "gpt-4.1",
        "azure.unexpected": "value",
    }


def test_legacy_sequence_dimensions_resolve_identity():
    legacy = [MetadataValue("ModelDeploymentName", "chat-route"), MetadataValue("StatusCode", "429")]
    assert normalize_dimensions(legacy) == {"ModelDeploymentName": "chat-route", "StatusCode": "429"}


def test_raw_rest_dictionary_sequence_dimensions_resolve_identity():
    payload = [
        {"name": {"value": "modelDeploymentName"}, "value": "chat-route"},
        {"name": "MODELVERSION", "value": "2026-04-14"},
    ]
    assert normalize_dimensions(payload) == {
        "ModelDeploymentName": "chat-route",
        "ModelVersion": "2026-04-14",
    }


def test_unsupported_shapes_never_invent_dimensions():
    assert normalize_dimensions(None) == {}
    assert normalize_dimensions("modeldeploymentname") == {}


def test_live_definitions_choose_the_documented_priority_and_exclude_bad_filters():
    selection = select_metrics(LIVE_DEFINITIONS, deployment_filtered=True)
    assert selection.resolved["input_tokens"] == "InputTokens"
    assert selection.resolved["output_tokens"] == "OutputTokens"
    assert selection.resolved["requests"] == "ModelRequests"
    assert selection.plans["requests"].status_dimensioned is True
    # Account-scope metrics are excluded before the request, not after a 400.
    assert "SuccessfulCalls" in selection.excluded
    assert "TotalErrors" in selection.excluded
    assert "Latency" in selection.excluded
    assert "cached_tokens" in selection.missing_fields
    assert "average_latency_ms" in selection.missing_fields


def test_audited_window_resolves_identity_tokens_and_outcomes():
    client = FakeClient(audited_responses())
    result = collect(
        client,
        available_metrics=LIVE_DEFINITIONS,
        deployments=["chat-route"],
        deployment_modes={"chat-route": "global"},
    )
    assert result.expected_buckets == 4032
    assert len(result.records) == 2
    assert result.active_buckets == 2
    assert {record.deployment_name for record in result.records} == {"chat-route"}
    assert {record.model_name for record in result.records} == {"gpt-4.1"}
    assert {record.model_version for record in result.records} == {"2026-04-14"}
    assert sum(record.metrics.input_tokens or 0 for record in result.records) == 30
    assert sum(record.metrics.output_tokens or 0 for record in result.records) == 15
    assert sum(record.metrics.requests or 0 for record in result.records) == 4
    assert sum(record.metrics.successful_requests or 0 for record in result.records) == 2
    assert sum(record.metrics.failed_requests or 0 for record in result.records) == 2
    # Complete status coverage with no 429 series is a genuine zero.
    assert sum(record.metrics.throttled_requests or 0 for record in result.records) == 0
    assert result.outcome_coverage_complete is True
    assert result.status_codes == {"200": 2, "400": 2}
    # Cached tokens were never reported, so they stay unavailable.
    assert all(record.metrics.cached_tokens is None for record in result.records)
    assert all("cached_tokens" in record.missing_metrics for record in result.records)
    assert result.records[0].metric_provenance["input_tokens"] == "InputTokens"
    assert result.records[0].metric_provenance["successful_requests"] == "ModelRequests"
    assert result.records[0].expected_buckets == 4032
    assert result.records[0].outcome_coverage == "complete"


def test_missing_deployment_dimension_falls_back_to_the_single_requested_deployment():
    payload = {
        "ModelDeploymentName eq 'chat-route'": {
            "metrics": [dict_series("InputTokens", {}, [point(START, 100)])]
        }
    }
    result = collect(
        FakeClient(payload),
        available_metrics=[definition("InputTokens", ["ModelDeploymentName"])],
        deployments=["chat-route"],
    )
    assert result.records[0].deployment_name == "chat-route"
    assert result.records[0].model_name == "unknown"


def test_missing_deployment_dimension_with_two_requested_deployments_fails_clearly():
    payload = {
        "ModelDeploymentName eq 'chat-route' or ModelDeploymentName eq 'batch-route'": {
            "metrics": [dict_series("InputTokens", {}, [point(START, 100)])]
        }
    }
    with pytest.raises(IdentityError) as error:
        collect(
            FakeClient(payload),
            available_metrics=[definition("InputTokens", ["ModelDeploymentName"])],
            deployments=["chat-route", "batch-route"],
        )
    assert "one deployment at a time" in str(error.value)


def test_inventory_supplies_model_identity_when_dimensions_omit_it():
    payload = {
        "ModelDeploymentName eq 'chat-route'": {
            "metrics": [dict_series("InputTokens", {"modeldeploymentname": "chat-route"}, [point(START, 100)])]
        }
    }
    result = collect(
        FakeClient(payload),
        available_metrics=[definition("InputTokens", ["ModelDeploymentName"])],
        deployments=["chat-route"],
        deployment_inventory=[{"name": "chat-route", "model": "gpt-4.1", "model_version": "2026-04-14", "sku": "GlobalStandard"}],
    )
    assert result.records[0].model_name == "gpt-4.1"
    assert result.records[0].model_version == "2026-04-14"
    assert result.identity_conflicts == []


def test_inventory_and_dimension_conflicts_are_surfaced_not_overwritten():
    payload = {
        "ModelDeploymentName eq 'chat-route'": {
            "metrics": [
                dict_series(
                    "InputTokens",
                    {"modeldeploymentname": "chat-route", "modelname": "gpt-4.1", "modelversion": "2026-04-14"},
                    [point(START, 100)],
                )
            ]
        }
    }
    result = collect(
        FakeClient(payload),
        available_metrics=[definition("InputTokens", ["ModelDeploymentName", "ModelName", "ModelVersion"])],
        deployments=["chat-route"],
        deployment_inventory=[{"name": "chat-route", "model": "gpt-4.1", "model_version": "2026-01-01"}],
    )
    assert result.records[0].model_version == "2026-04-14"
    assert any("does not match metric version" in item for item in result.identity_conflicts)
    assert reconcile_inventory(result.records, [{"name": "chat-route", "model": "gpt-4o"}])


def test_partial_dimension_series_coalesce_into_one_bucket():
    """Metrics exposing different dimensions must not split one interval."""
    payload = {
        "ModelDeploymentName eq 'chat-route'": {
            "metrics": [
                dict_series("InputTokens", {"modeldeploymentname": "chat-route"}, [point(START, 60)]),
                dict_series(
                    "OutputTokens",
                    {"modeldeploymentname": "chat-route", "modelname": "gpt-4.1", "modelversion": "2026-04-14"},
                    [point(START, 20)],
                ),
            ]
        }
    }
    result = collect(
        FakeClient(payload),
        available_metrics=[
            definition("InputTokens", ["ModelDeploymentName"]),
            definition("OutputTokens", ["ModelDeploymentName", "ModelName", "ModelVersion"]),
        ],
        deployments=["chat-route"],
    )
    assert len(result.records) == 1
    record = result.records[0]
    assert record.metrics.input_tokens == 60
    assert record.metrics.output_tokens == 20
    assert record.model_name == "gpt-4.1"


def test_secondary_dimensions_are_summed_not_dropped():
    """Cached tokens split by context length are one interval, not two."""
    payload = {
        "ModelDeploymentName eq 'chat-route'": {
            "metrics": [
                {
                    "name": "cacheReadInputTokens",
                    "timeseries": [
                        {
                            "metadata_values": {"modeldeploymentname": "chat-route", "contextlength": "0-32k"},
                            "data": [point(START, 10)],
                        },
                        {
                            "metadata_values": {"modeldeploymentname": "chat-route", "contextlength": "32k-128k"},
                            "data": [point(START, 5)],
                        },
                    ],
                }
            ]
        }
    }
    result = collect(
        FakeClient(payload),
        available_metrics=[definition("cacheReadInputTokens", ["ModelDeploymentName", "ContextLength"])],
        deployments=["chat-route"],
    )
    assert len(result.records) == 1
    assert result.records[0].metrics.cached_tokens == 15
    assert result.coalesced_series == 1
    assert result.records[0].metric_provenance["cached_tokens"] == "cacheReadInputTokens"


def test_unmodelled_dimensions_are_kept_for_diagnostics_only():
    payload = {
        "ModelDeploymentName eq 'chat-route'": {
            "metrics": [
                dict_series(
                    "InputTokens",
                    {"modeldeploymentname": "chat-route", "futureDimension": "preview"},
                    [point(START, 10)],
                )
            ]
        }
    }
    result = collect(
        FakeClient(payload),
        available_metrics=[definition("InputTokens", ["ModelDeploymentName"])],
        deployments=["chat-route"],
    )
    assert result.observed_dimensions == {"azure.futureDimension": "preview"}
    # Diagnostics stay in the collection result; telemetry records never carry
    # an unreviewed Azure dimension value.
    assert "futureDimension" not in result.records[0].model_dump_json()


def test_status_series_without_a_status_dimension_stay_unavailable():
    payload = {
        "ModelDeploymentName eq 'chat-route'": {
            "metrics": [dict_series("ModelRequests", {"modeldeploymentname": "chat-route"}, [point(START, 7)])]
        }
    }
    result = collect(
        FakeClient(payload),
        available_metrics=[definition("ModelRequests", ["ModelDeploymentName"])],
        deployments=["chat-route"],
    )
    record = result.records[0]
    assert record.metrics.requests == 7
    assert record.metrics.successful_requests is None
    assert record.metrics.throttled_requests is None
    assert record.outcome_coverage == "unavailable"
    assert result.outcome_coverage_complete is False


def test_a_resource_with_no_queryable_metric_is_an_explicit_error():
    with pytest.raises(CollectorError):
        collect(
            FakeClient({}),
            available_metrics=[definition("SuccessfulCalls", ["ApiName", "StatusCode"])],
            deployments=["chat-route"],
        )


def test_collection_summary_distinguishes_unavailable_from_partial_outcomes():
    """A source with no status series is unavailable, never 'partial'."""
    payload = {
        "ModelDeploymentName eq 'chat-route'": {
            "metrics": [dict_series("ModelRequests", {"modeldeploymentname": "chat-route"}, [point(START, 7)])]
        }
    }
    result = collect(
        FakeClient(payload),
        available_metrics=[definition("ModelRequests", ["ModelDeploymentName"])],
        deployments=["chat-route"],
    )
    assert result.outcome_coverage == "unavailable"
    assert result.summary()["outcome_coverage"] == "unavailable"

    complete = collect(
        FakeClient(audited_responses()),
        available_metrics=LIVE_DEFINITIONS,
        deployments=["chat-route"],
    )
    assert complete.summary()["outcome_coverage"] == "complete"
