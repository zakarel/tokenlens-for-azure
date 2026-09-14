"""OpenTelemetry GenAI ingestion and optional span emission.

TokenLens prefers OpenTelemetry semantic conventions over a bespoke wire
protocol. This module maps already-generated spans onto the canonical telemetry
schema using a strict attribute allow list: anything content-bearing is counted
and discarded, never read into a record.

No OpenTelemetry package is required to import an export file. The optional
:class:`TokenLensSpanProcessor` is duck-typed, so it can be added to an existing
``TracerProvider`` without replacing the application's own exporter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import TelemetryConfig
from .privacy import is_content_key
from .schema import (
    ErrorCategory,
    ModelRequestRecord,
    ResourceScope,
    TelemetryUsage,
    deterministic_event_id,
)
from .writer import TelemetryWriter

#: GenAI semantic-convention attributes TokenLens understands. Everything else
#: is ignored; content attributes are counted separately.
ALLOWED_ATTRIBUTES = frozenset(
    {
        "gen_ai.system",
        "gen_ai.provider.name",
        "gen_ai.operation.name",
        "gen_ai.request.model",
        "gen_ai.response.model",
        "gen_ai.request.max_tokens",
        "gen_ai.request.service_tier",
        "gen_ai.response.service_tier",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.usage.prompt_tokens",
        "gen_ai.usage.completion_tokens",
        "gen_ai.usage.cached_input_tokens",
        "gen_ai.usage.reasoning_tokens",
        "gen_ai.azure.deployment_name",
        "gen_ai.openai.request.service_tier",
        "az.deployment.mode",
        "server.address",
        "error.type",
        "http.response.status_code",
        "tokenlens.deployment_mode",
        "tokenlens.workload",
    }
)

#: Attributes that identify a network location rather than a workload. They are
#: recognised so they can be dropped explicitly rather than silently.
DROPPED_IDENTITY_ATTRIBUTES = frozenset({"server.address", "server.port", "url.full", "http.url"})

_ERROR_TYPE_CATEGORIES: dict[str, ErrorCategory] = {
    "ratelimiterror": "rate_limited",
    "rate_limit": "rate_limited",
    "429": "rate_limited",
    "authenticationerror": "authentication",
    "401": "authentication",
    "permissiondeniederror": "authorization",
    "403": "authorization",
    "notfounderror": "not_found",
    "404": "not_found",
    "badrequesterror": "bad_request",
    "400": "bad_request",
    "timeout": "timeout",
    "timeouterror": "timeout",
    "408": "timeout",
    "connectionerror": "connection",
    "internalservererror": "server_error",
    "500": "server_error",
    "503": "server_error",
}


class OtelImportError(ValueError):
    """Raised when an OpenTelemetry export cannot be mapped deterministically."""


@dataclass
class ImportResult:
    """Bounded summary of an import. Discarded values are counted, never shown."""

    records: list[ModelRequestRecord] = field(default_factory=list)
    spans_seen: int = 0
    spans_mapped: int = 0
    spans_skipped: int = 0
    duplicates_skipped: int = 0
    content_attributes_discarded: int = 0
    ignored_attribute_names: set[str] = field(default_factory=set)

    def summary(self) -> dict[str, int]:
        return {
            "spans_seen": self.spans_seen,
            "spans_mapped": self.spans_mapped,
            "spans_skipped": self.spans_skipped,
            "duplicates_skipped": self.duplicates_skipped,
            "content_attributes_discarded": self.content_attributes_discarded,
        }


def _attribute_value(value: Any) -> Any:
    """Unwrap an OTLP/JSON ``AnyValue``."""
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            raw = value[key]
            if key == "intValue":
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None
            return raw
    if "arrayValue" in value or "kvlistValue" in value:
        return None
    return None


def _attributes(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        collected: dict[str, Any] = {}
        for item in raw:
            if isinstance(item, dict) and "key" in item:
                collected[str(item["key"])] = _attribute_value(item.get("value"))
        return collected
    return {}


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _timestamp(span: dict[str, Any]) -> datetime:
    for key in ("startTimeUnixNano", "start_time_unix_nano", "startTime", "timestamp"):
        value = span.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.isdigit():
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        try:
            nanos = int(value)
        except (TypeError, ValueError):
            continue
        return datetime.fromtimestamp(nanos / 1_000_000_000, tz=UTC)
    raise OtelImportError("span has no usable start timestamp")


def _latency_ms(span: dict[str, Any]) -> float | None:
    start = span.get("startTimeUnixNano") or span.get("start_time_unix_nano")
    end = span.get("endTimeUnixNano") or span.get("end_time_unix_nano")
    try:
        return round((int(end) - int(start)) / 1_000_000, 3)
    except (TypeError, ValueError):
        return None


def _error_category(attributes: dict[str, Any], span: dict[str, Any]) -> tuple[ErrorCategory | None, int | None]:
    status_code = attributes.get("http.response.status_code")
    status = _int(status_code) or None
    error_type = attributes.get("error.type")
    span_status = span.get("status") if isinstance(span.get("status"), dict) else {}
    failed = str(span_status.get("code", "")).upper() in {"STATUS_CODE_ERROR", "ERROR", "2"}
    if error_type:
        category = _ERROR_TYPE_CATEGORIES.get(str(error_type).casefold())
        if category is None:
            category = "unknown"
        return category, status
    if failed:
        if status and status in _ERROR_TYPE_CATEGORIES:
            return _ERROR_TYPE_CATEGORIES[str(status)], status
        return "unknown", status
    return None, status or 200


def _provider_for(system: str) -> str:
    """Map a GenAI system name onto TokenLens's provider vocabulary.

    Azure emits several system names (``az.ai.openai``, ``az.ai.inference``,
    ``azure_ai_openai``). They all resolve to the Foundry provider so pricing
    and PTU analysis group them correctly.
    """
    lowered = system.casefold()
    if lowered.startswith("az.") or "azure" in lowered:
        return "azure_foundry"
    return system


def map_span(span: dict[str, Any], *, scope: ResourceScope | None = None) -> tuple[ModelRequestRecord | None, int, set[str]]:
    """Map one span to a canonical record, discarding content attributes."""
    attributes = _attributes(span.get("attributes"))
    discarded = 0
    ignored: set[str] = set()
    kept: dict[str, Any] = {}
    for key, value in attributes.items():
        if key in ALLOWED_ATTRIBUTES and key not in DROPPED_IDENTITY_ATTRIBUTES:
            kept[key] = value
            continue
        if is_content_key(key):
            discarded += 1
            continue
        ignored.add(key)
    operation = str(kept.get("gen_ai.operation.name") or span.get("name") or "")
    request_model = kept.get("gen_ai.request.model")
    response_model = kept.get("gen_ai.response.model")
    if not request_model and not response_model:
        return None, discarded, ignored
    deployment = str(kept.get("gen_ai.azure.deployment_name") or request_model or response_model)
    usage = TelemetryUsage(
        input_tokens=_int(kept.get("gen_ai.usage.input_tokens", kept.get("gen_ai.usage.prompt_tokens"))),
        output_tokens=_int(kept.get("gen_ai.usage.output_tokens", kept.get("gen_ai.usage.completion_tokens"))),
        cached_tokens=_int(kept.get("gen_ai.usage.cached_input_tokens")),
        reasoning_tokens=_int(kept.get("gen_ai.usage.reasoning_tokens")),
    )
    category, status = _error_category(kept, span)
    system = str(kept.get("gen_ai.provider.name") or kept.get("gen_ai.system") or "unknown")
    provider = _provider_for(system)
    # A span identifier is local and opaque; it is hashed so no upstream trace
    # identity is written into telemetry, while duplicates stay detectable.
    span_id = str(span.get("spanId") or span.get("span_id") or "")
    record = ModelRequestRecord(
        source="otel",
        event_id=deterministic_event_id("otel", span_id, deployment, _timestamp(span).isoformat()),
        timestamp=_timestamp(span),
        provider=provider,
        deployment_name=deployment,
        model_name=str(response_model or request_model),
        service_tier=str(kept.get("gen_ai.response.service_tier") or kept.get("gen_ai.request.service_tier") or "standard"),
        deployment_mode=str(kept.get("tokenlens.deployment_mode") or kept.get("az.deployment.mode") or "unknown"),
        api=operation or None,
        workload=str(kept["tokenlens.workload"]) if kept.get("tokenlens.workload") else None,
        scope=scope or ResourceScope(),
        usage=usage,
        latency_ms=_latency_ms(span),
        status_code=status,
        error_category=category,
    )
    return record, discarded, ignored


def iter_spans(payload: Any) -> Iterator[dict[str, Any]]:
    """Yield spans from OTLP/JSON, a span list, or a JSONL export."""
    if isinstance(payload, dict) and "resourceSpans" in payload:
        for resource in payload.get("resourceSpans") or []:
            for scope in resource.get("scopeSpans") or resource.get("instrumentationLibrarySpans") or []:
                yield from scope.get("spans") or []
        return
    if isinstance(payload, dict) and "spans" in payload:
        yield from payload.get("spans") or []
        return
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(payload, dict):
        yield payload


def import_spans(
    spans: Iterable[dict[str, Any]],
    *,
    scope: ResourceScope | None = None,
    max_spans: int = 1_000_000,
) -> ImportResult:
    result = ImportResult()
    seen: set[str] = set()
    for span in spans:
        if result.spans_seen >= max_spans:
            break
        result.spans_seen += 1
        try:
            record, discarded, ignored = map_span(span, scope=scope)
        except OtelImportError:
            result.spans_skipped += 1
            continue
        result.content_attributes_discarded += discarded
        result.ignored_attribute_names |= ignored
        if record is None:
            result.spans_skipped += 1
            continue
        if record.event_id in seen:
            result.duplicates_skipped += 1
            continue
        seen.add(record.event_id)
        result.records.append(record)
        result.spans_mapped += 1
    return result


def import_file(path: str | Path, *, scope: ResourceScope | None = None, max_spans: int = 1_000_000) -> ImportResult:
    """Import an OTLP/JSON, JSON array, or JSONL span export without network access.

    The format is detected by parsing, not by guessing from the first bytes: a
    JSONL export whose every line is a complete OTLP payload starts with the
    same ``{"resourceSpans"`` prefix as a single pretty-printed OTLP document.
    """
    source = Path(path)
    if not source.is_file():
        raise OtelImportError(f"Input file not found: {source}")
    text = source.read_text(encoding="utf-8")
    if not text.strip():
        return ImportResult()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return import_spans(_iter_jsonl_spans(text), scope=scope, max_spans=max_spans)
    return import_spans(iter_spans(payload), scope=scope, max_spans=max_spans)


def _iter_jsonl_spans(text: str) -> Iterator[dict[str, Any]]:
    """Yield spans from a JSONL export, where each line is its own document."""
    parsed_any = False
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OtelImportError(
                f"Input is not valid OTLP/JSON, JSON, or JSONL (line {line_number})"
            ) from exc
        parsed_any = True
        yield from iter_spans(payload)
    if not parsed_any:
        raise OtelImportError("Input is not valid OTLP/JSON, JSON, or JSONL")


def write_import(result: ImportResult, writer: TelemetryWriter, *, skip_existing: bool = True) -> int:
    """Persist imported records idempotently. Returns the number newly written."""
    return writer.write_all(result.records, skip_existing=skip_existing)


class TokenLensSpanProcessor:
    """Optional span processor that mirrors GenAI spans into canonical JSONL.

    It consumes spans the application already produces and does not replace or
    require any exporter. It is duck-typed against the OpenTelemetry
    ``SpanProcessor`` interface so importing OpenTelemetry stays optional.
    """

    def __init__(
        self,
        output_dir: str | Path | None = None,
        *,
        writer: TelemetryWriter | None = None,
        config: TelemetryConfig | None = None,
        scope: ResourceScope | None = None,
    ) -> None:
        resolved = config or TelemetryConfig.from_env(output_dir=Path(output_dir) if output_dir else None)
        self._writer = writer or TelemetryWriter(resolved)
        self._scope = scope
        self.spans_seen = 0
        self.spans_written = 0
        self.content_attributes_discarded = 0

    # -- SpanProcessor interface ---------------------------------------
    def on_start(self, span: Any, parent_context: Any = None) -> None:  # pragma: no cover - no-op hook
        return None

    def on_end(self, span: Any) -> None:
        self.spans_seen += 1
        payload = self._as_payload(span)
        if payload is None:
            return
        try:
            record, discarded, _ = map_span(payload, scope=self._scope)
        except OtelImportError:
            return
        self.content_attributes_discarded += discarded
        if record is not None and self._writer.write(record):
            self.spans_written += 1

    def shutdown(self) -> None:
        self._writer.close()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        self._writer.flush()
        return True

    # -- helpers -------------------------------------------------------
    @staticmethod
    def _as_payload(span: Any) -> dict[str, Any] | None:
        if isinstance(span, dict):
            return span
        attributes = getattr(span, "attributes", None)
        if attributes is None:
            return None
        context = getattr(span, "context", None) or getattr(span, "get_span_context", lambda: None)()
        span_id = getattr(context, "span_id", "") if context is not None else ""
        status = getattr(span, "status", None)
        status_code = getattr(getattr(status, "status_code", None), "name", None)
        return {
            "name": getattr(span, "name", ""),
            "spanId": str(span_id),
            "startTimeUnixNano": getattr(span, "start_time", None),
            "endTimeUnixNano": getattr(span, "end_time", None),
            "attributes": dict(attributes),
            "status": {"code": status_code or ""},
        }
