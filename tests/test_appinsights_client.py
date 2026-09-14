"""Application Insights live-query adapter, exercised with fake clients only.

No Azure SDK is imported and no network call is made: `AppInsightsLogsClient`
is constructed directly around a fake client object that mimics the shape of
`azure.monitor.query.LogsQueryClient`/`LogsQueryResult`, and `.create()` is
tested only up to the point where it would need the optional dependency.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tokenlens.foundry.appinsights_client import (
    AppInsightsLogsClient,
    tables_from_response,
    timespan_for_days,
)
from tokenlens.foundry.monitor import CollectorError
from tokenlens.telemetry.appinsights import import_export

CANARY_PROMPT = "CANARY-PROMPT-app-insights-collector-should-never-store-this"


class FakeColumn:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeTable:
    def __init__(self, columns, rows) -> None:
        self.columns = [FakeColumn(name) for name in columns]
        self.rows = rows


class FakeLogsQueryResult:
    def __init__(self, tables) -> None:
        self.tables = tables


class FakeLogsClient:
    """Duck-typed like `azure.monitor.query.LogsQueryClient`."""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    def query_workspace(self, workspace_id, query, *, timespan):
        self.calls.append({"workspace_id": workspace_id, "query": query, "timespan": timespan})
        return self.result


def _fake_result() -> FakeLogsQueryResult:
    columns = ["timestamp", "name", "id", "duration", "success", "resultCode", "customDimensions", "customMeasurements"]
    row = [
        "2026-09-14T12:00:00Z",
        "chat example-support-prod",
        "row-1",
        1450,
        "True",
        "200",
        {
            "gen_ai.system": "az.ai.openai",
            "gen_ai.request.model": "example-support-prod",
            "gen_ai.response.model": "gpt-4.1-2026-04-14",
            "gen_ai.usage.input_tokens": 4200,
            "gen_ai.usage.output_tokens": 380,
            "gen_ai.prompt": CANARY_PROMPT,
        },
        {},
    ]
    return FakeLogsQueryResult([FakeTable(columns, [row])])


def test_query_genai_traces_sends_a_bounded_kql_query_without_body_columns():
    client = AppInsightsLogsClient(FakeLogsClient(_fake_result()))
    response = client.query_genai_traces("workspace-id", days=14, timespan=timespan_for_days(14))
    assert response is client._client.result  # noqa: SLF001 - test-only introspection
    sent_query = client._client.calls[0]["query"]  # noqa: SLF001
    assert "dependencies" in sent_query
    assert "customDimensions" in sent_query
    assert "content" not in sent_query.casefold()
    assert "body" not in sent_query.casefold()


def test_query_genai_traces_rejects_a_non_positive_window():
    client = AppInsightsLogsClient(FakeLogsClient(_fake_result()))
    with pytest.raises(CollectorError):
        client.query_genai_traces("workspace-id", days=0, timespan=timespan_for_days(1))


def test_create_fails_clearly_when_the_optional_dependency_is_missing(monkeypatch):
    monkeypatch.setattr("tokenlens.foundry.appinsights_client.has_package", lambda name: False)
    with pytest.raises(CollectorError) as error:
        AppInsightsLogsClient.create()
    assert "foundry-monitor" in str(error.value)


def test_tables_from_response_normalizes_a_fake_logs_query_result():
    normalized = tables_from_response(_fake_result())
    assert normalized["tables"][0]["columns"][0] == "timestamp"
    assert len(normalized["tables"][0]["rows"]) == 1


def test_tables_from_response_passes_through_an_already_normalized_dict():
    payload = {"tables": [{"columns": ["a"], "rows": [[1]]}]}
    assert tables_from_response(payload) is payload


def test_tables_from_response_handles_a_response_with_no_tables():
    assert tables_from_response(SimpleNamespace()) == {"tables": []}


def test_live_query_result_maps_through_the_same_importer_as_the_offline_path():
    """The live-query and offline-import paths must agree on one shape."""
    normalized = tables_from_response(_fake_result())
    result = import_export(normalized)
    assert result.spans_mapped == 1
    record = result.records[0]
    assert record.usage.input_tokens == 4200
    assert record.usage.output_tokens == 380
    assert record.status_code == 200
    assert CANARY_PROMPT not in record.model_dump_json()
