"""Live Application Insights / Log Analytics query client.

Nothing here is imported until a user runs the explicit
``collect-app-insights`` command, so the offline analyzer and the import
path (:mod:`tokenlens.telemetry.appinsights`) never depend on an Azure SDK.

The KQL query is a fixed, reviewable string that projects GenAI usage
dimensions only. It never selects a request/response body column, and the
rows it returns are converted with the same Application Insights adapter the
offline import command uses, so a live collection and an offline import of an
equivalent export produce identical canonical records.
"""

from __future__ import annotations

from typing import Any, Protocol

from .azure_clients import MONITOR_EXTRA_HINT, credential, has_package
from .monitor import CollectorError

#: A fixed, reviewable KQL projection over the ``dependencies`` table, where
#: Application Insights/OpenTelemetry exporters record outbound GenAI calls.
#: Only GenAI usage dimensions are selected — request and response content
#: columns (``customDimensions`` entries such as prompts) are never excluded
#: from the source table, but this importer only ever reads the allow-listed
#: ``gen_ai.*``/``az.*`` keys back out of it (see
#: :mod:`tokenlens.telemetry.appinsights`).
GENAI_TRACES_QUERY_TEMPLATE = (
    "dependencies\n"
    "| where timestamp > ago({days}d)\n"
    '| where isnotempty(customDimensions["gen_ai.request.model"]) '
    'or isnotempty(customDimensions["gen_ai.system"])\n'
    "| project timestamp, name, id, duration, success, resultCode, customDimensions, customMeasurements"
)


class LogsClient(Protocol):
    """The minimal Azure Monitor Logs surface TokenLens depends on."""

    def query_workspace(self, workspace_id: str, query: str, *, timespan: Any) -> Any: ...


class AppInsightsLogsClient:
    """Thin adapter over the supported Azure Monitor Logs query client."""

    def __init__(self, client: LogsClient) -> None:
        self._client = client

    @classmethod
    def create(cls, credential_object: Any | None = None) -> "AppInsightsLogsClient":
        if not has_package("azure.monitor.query"):
            raise CollectorError(f"azure-monitor-query is not installed. {MONITOR_EXTRA_HINT}")
        from azure.monitor.query import LogsQueryClient  # type: ignore[import-not-found]

        return cls(LogsQueryClient(credential_object or credential()))

    def query_genai_traces(self, workspace_id: str, *, days: int, timespan: Any) -> Any:
        if days <= 0:
            raise CollectorError("lookback days must be positive")
        query = GENAI_TRACES_QUERY_TEMPLATE.format(days=days)
        return self._client.query_workspace(workspace_id, query, timespan=timespan)


def tables_from_response(response: Any) -> dict[str, Any]:
    """Normalize a ``LogsQueryResult`` (or an already-JSON payload) to ``{"tables": [...]}``.

    Accepts the real SDK's ``LogsQueryResult`` (duck-typed via ``.tables``),
    a fake test double with the same shape, or a plain dict already in the
    exported shape, so the same normalizer works for live queries and for
    tests that never import the Azure SDK.
    """
    if isinstance(response, dict) and "tables" in response:
        return response
    tables = getattr(response, "tables", None)
    if tables is None:
        return {"tables": []}
    normalized: list[dict[str, Any]] = []
    for table in tables:
        raw_columns = getattr(table, "columns", None)
        if raw_columns is None and isinstance(table, dict):
            raw_columns = table.get("columns")
        columns = [getattr(column, "name", column) for column in (raw_columns or [])]
        raw_rows = getattr(table, "rows", None)
        if raw_rows is None and isinstance(table, dict):
            raw_rows = table.get("rows")
        rows = [list(row) for row in (raw_rows or [])]
        normalized.append({"columns": columns, "rows": rows})
    return {"tables": normalized}


def timespan_for_days(days: int):
    """Build the ``(start, end)`` timespan tuple most Azure Monitor SDKs accept."""
    from datetime import UTC, datetime, timedelta

    if days <= 0:
        raise CollectorError("lookback days must be positive")
    end = datetime.now(UTC)
    return (end - timedelta(days=days), end)


__all__ = [
    "AppInsightsLogsClient",
    "GENAI_TRACES_QUERY_TEMPLATE",
    "LogsClient",
    "tables_from_response",
    "timespan_for_days",
]
