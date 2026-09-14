"""Application Insights import that reuses the OpenTelemetry GenAI mapper.

Application Insights/Log Analytics exposes already-collected GenAI telemetry
in one of two exported shapes:

1. A **Logs query result** — the JSON shape returned by
   ``az monitor app-insights query``, the Log Analytics REST API, and the
   Azure Monitor Logs SDK: ``{"tables": [{"columns": [...], "rows": [...]}]}``.
2. A **flat export** — the shape produced by Application Insights continuous
   export or "Export to JSON": a JSON array of records, each carrying its
   attributes in ``customDimensions``/``customMeasurements``.

Rather than re-implement attribute allow-listing and content rejection, this
module folds either shape into the same span-shaped ``dict`` the
OpenTelemetry importer already understands and calls
:func:`tokenlens.telemetry.otel.import_spans`/``map_span`` directly. GenAI
semantic-convention attributes (``gen_ai.*``) are expected in both shapes,
because Application Insights ingests them unchanged when an application is
instrumented with OpenTelemetry GenAI conventions and exported through Azure
Monitor. Anything else is discarded by the same allow list OTel import uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .otel import ImportResult, OtelImportError, import_spans
from .schema import ResourceScope

__all__ = [
    "ImportResult",
    "OtelImportError",
    "import_export",
    "import_file",
]


def _row_records(table: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Turn one Logs-query table into per-row dicts keyed by column name."""
    columns = []
    for column in table.get("columns") or []:
        name = column.get("name") if isinstance(column, dict) else column
        columns.append(name)
    for row in table.get("rows") or []:
        if not isinstance(row, (list, tuple)):
            continue
        yield {name: value for name, value in zip(columns, row) if name}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _epoch_nanos(value: Any) -> int | None:
    """Best-effort conversion of an Application Insights timestamp to epoch ns."""
    from datetime import UTC, datetime

    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Application Insights/Log Analytics timestamps in JSON are ISO
        # strings; a bare number is treated as already being epoch seconds.
        return int(value * 1_000_000_000)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return int(parsed.timestamp() * 1_000_000_000)
    return None


def _as_span(record: dict[str, Any]) -> dict[str, Any]:
    """Fold one Application Insights row/record into an OTel-shaped span dict."""
    attributes: dict[str, Any] = {}
    for key, value in record.items():
        if key in {"customDimensions", "customMeasurements"}:
            continue
        if str(key).startswith("gen_ai.") or str(key).startswith("az."):
            attributes[key] = value
    attributes.update(_as_dict(record.get("customDimensions")))
    attributes.update(_as_dict(record.get("customMeasurements")))

    start_nanos = _epoch_nanos(record.get("timestamp") or record.get("timestamp [UTC]"))
    duration_ms = record.get("duration") or record.get("DurationMs")
    end_nanos = None
    if start_nanos is not None and duration_ms is not None:
        try:
            end_nanos = start_nanos + int(float(duration_ms) * 1_000_000)
        except (TypeError, ValueError):
            end_nanos = None

    success = record.get("success")
    failed = isinstance(success, str) and success.strip().casefold() == "false"
    span: dict[str, Any] = {
        "name": str(record.get("name") or record.get("target") or "app-insights"),
        "spanId": str(record.get("id") or record.get("itemId") or record.get("operation_Id") or ""),
        "attributes": attributes,
        "status": {"code": "STATUS_CODE_ERROR" if failed else "STATUS_CODE_OK"},
    }
    if start_nanos is not None:
        span["startTimeUnixNano"] = start_nanos
    if end_nanos is not None:
        span["endTimeUnixNano"] = end_nanos
    result_code = record.get("resultCode") or record.get("ResultCode")
    if result_code is not None and "http.response.status_code" not in attributes:
        attributes["http.response.status_code"] = result_code
    return span


def _iter_export_records(payload: Any) -> Iterator[dict[str, Any]]:
    """Yield per-row records from either supported Application Insights shape."""
    if isinstance(payload, dict) and "tables" in payload:
        for table in payload.get("tables") or []:
            if isinstance(table, dict):
                yield from _row_records(table)
        return
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(payload, dict):
        yield payload


def import_export(payload: Any, *, scope: ResourceScope | None = None, max_rows: int = 1_000_000) -> ImportResult:
    """Import an already-parsed Application Insights export or Logs query result."""
    spans = (_as_span(record) for record in _iter_export_records(payload))
    return import_spans(spans, scope=scope, max_spans=max_rows)


def import_file(path: str | Path, *, scope: ResourceScope | None = None, max_rows: int = 1_000_000) -> ImportResult:
    """Import an Application Insights export (JSON or JSONL) without network access."""
    source = Path(path)
    if not source.is_file():
        raise OtelImportError(f"Input file not found: {source}")
    text = source.read_text(encoding="utf-8")
    if not text.strip():
        return ImportResult()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return import_spans(_iter_jsonl_export(text), scope=scope, max_spans=max_rows)
    return import_export(payload, scope=scope, max_rows=max_rows)


def _iter_jsonl_export(text: str) -> Iterator[dict[str, Any]]:
    parsed_any = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OtelImportError(
                f"Input is not valid Application Insights JSON or JSONL (line {line_number})"
            ) from exc
        parsed_any = True
        for record in _iter_export_records(payload):
            yield _as_span(record)
    if not parsed_any:
        raise OtelImportError("Input is not valid Application Insights JSON or JSONL")
