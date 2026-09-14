"""Generic, declarative import for existing JSONL logs.

This importer maps arbitrary application logs onto the canonical, contentless
telemetry schema using a small YAML mapping file. Two properties make this
safe by construction:

1. **No code execution.** The mapping file is parsed with ``yaml.safe_load``
   only, so it can never construct a Python object, call a function, or run a
   plugin. Every entry is a literal dotted field path (a string) or a nested
   mapping of such paths; there is no expression language.
2. **A closed destination vocabulary.** Mapping values may only be written
   into the fields :class:`~tokenlens.telemetry.schema.ModelRequestRecord`
   already defines. Because that schema has no prompt, response, message,
   tool-argument, or credential field, a mapping file cannot smuggle content
   through even a hostile or careless author: there is nowhere content-shaped
   to put it. As a second, defensive layer, any *source* field path whose
   final path segment looks like content or a credential (see
   :func:`tokenlens.telemetry.privacy.is_content_key`) is rejected before a
   single row is read, so pointing a mapping at ``prompt`` or ``authorization``
   fails loudly at load time instead of silently importing a truncated
   fragment into a string field such as ``model_name``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .privacy import is_content_key
from .schema import (
    ERROR_CATEGORIES,
    ModelRequestRecord,
    ResourceScope,
    TelemetryUsage,
    deterministic_event_id,
)
from .writer import TelemetryWriter

#: Top-level keys the mapping file may define. Anything else is rejected so a
#: typo or an attempt to add an unsupported capability fails at load time.
_TOP_LEVEL_KEYS = frozenset({"version", "fields", "defaults", "timestamp_format"})

#: Scalar destination fields on ModelRequestRecord that a mapping may target.
_SCALAR_FIELDS = frozenset(
    {
        "timestamp",
        "provider",
        "deployment_name",
        "model_name",
        "service_tier",
        "deployment_mode",
        "api",
        "workload",
        "latency_ms",
        "status_code",
        "error_category",
    }
)

#: Nested destination groups. Each maps to a canonical sub-model.
_USAGE_FIELDS = frozenset(
    {"input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cache_write_tokens"}
)
_SCOPE_FIELDS = frozenset({"resource_name", "project_name", "workload", "environment"})

#: Fields that must resolve to a non-empty value for a row to be imported.
_REQUIRED_FIELDS = frozenset({"timestamp", "provider", "deployment_name", "model_name"})

_TIMESTAMP_FORMATS = frozenset({"auto", "iso8601", "epoch_seconds", "epoch_millis"})


class MappingError(ValueError):
    """Raised when a mapping file is invalid or unsafe."""


class GenericImportError(ValueError):
    """Raised when the input log cannot be read as JSON or JSONL."""


@dataclass
class MappingSpec:
    """A validated, declarative field mapping. Immutable after :func:`load_mapping`."""

    fields: dict[str, str] = field(default_factory=dict)
    usage: dict[str, str] = field(default_factory=dict)
    scope: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=dict)
    timestamp_format: str = "auto"


@dataclass
class GenericImportResult:
    """Bounded summary of a generic import."""

    records: list[ModelRequestRecord] = field(default_factory=list)
    rows_seen: int = 0
    rows_mapped: int = 0
    rows_skipped: int = 0
    duplicates_skipped: int = 0

    def summary(self) -> dict[str, int]:
        return {
            "rows_seen": self.rows_seen,
            "rows_mapped": self.rows_mapped,
            "rows_skipped": self.rows_skipped,
            "duplicates_skipped": self.duplicates_skipped,
        }


#: Segment suffixes that name a token *count* rather than content, even
#: though they contain a content-like fragment (for example ``prompt`` inside
#: ``prompt_tokens``). This mirrors the OpenTelemetry importer's own allow
#: list, which keeps ``gen_ai.usage.prompt_tokens`` while still discarding
#: ``gen_ai.prompt``: the destination for these values is always a bounded,
#: non-negative integer counter, so even a mapping that pointed one at real
#: text would coerce to a harmless ``0`` rather than store the text.
_SAFE_COUNT_SUFFIXES = ("_tokens", "_token_count", "tokencount")


def _check_path_is_safe(destination: str, path: str) -> None:
    for segment in path.split("."):
        if not segment:
            raise MappingError(f"mapping field {destination!r} has an empty path segment in {path!r}")
        if segment.casefold().endswith(_SAFE_COUNT_SUFFIXES):
            continue
        if is_content_key(segment):
            raise MappingError(
                f"mapping field {destination!r} points at {path!r}, which looks like content or a "
                "credential; generic import never reads prompt, response, message, tool, or "
                "credential-shaped fields"
            )


def _validate_scalar_group(raw: Any, allowed: frozenset[str], *, group: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise MappingError(f"mapping section {group!r} must be a mapping of field name to source path")
    resolved: dict[str, str] = {}
    for key, value in raw.items():
        if key not in allowed:
            raise MappingError(f"mapping section {group!r} does not support field {key!r}")
        if not isinstance(value, str) or not value:
            raise MappingError(f"mapping field {group}.{key} must be a non-empty dotted source path string")
        _check_path_is_safe(f"{group}.{key}", value)
        resolved[key] = value
    return resolved


def load_mapping(path: str | Path) -> MappingSpec:
    """Load and validate a declarative YAML mapping file.

    Uses ``yaml.safe_load`` exclusively: no Python object, function call, or
    plugin can be constructed from the file. Anything the loader does not
    already reduce to plain ``dict``/``list``/``str``/``int``/``float``/``bool``/``None``
    is impossible to express, by construction of the safe loader.
    """
    import yaml

    source = Path(path)
    if not source.is_file():
        raise MappingError(f"Mapping file not found: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MappingError(f"Mapping file is not valid YAML: {exc}") from exc
    if raw is None:
        raise MappingError("Mapping file is empty")
    if not isinstance(raw, dict):
        raise MappingError("Mapping file must contain a YAML mapping at the top level")
    unknown = set(raw) - _TOP_LEVEL_KEYS
    if unknown:
        raise MappingError(f"Mapping file has unsupported top-level key(s): {', '.join(sorted(unknown))}")

    fields_raw = raw.get("fields") or {}
    if not isinstance(fields_raw, dict):
        raise MappingError("Mapping key 'fields' must be a mapping")
    usage_raw = fields_raw.pop("usage", None) if isinstance(fields_raw, dict) else None
    scope_raw = fields_raw.pop("scope", None) if isinstance(fields_raw, dict) else None

    scalar_fields = _validate_scalar_group(fields_raw, _SCALAR_FIELDS, group="fields")
    usage_fields = _validate_scalar_group(usage_raw, _USAGE_FIELDS, group="fields.usage")
    scope_fields = _validate_scalar_group(scope_raw, _SCOPE_FIELDS, group="fields.scope")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise MappingError("Mapping key 'defaults' must be a mapping")
    unknown_defaults = set(defaults) - _SCALAR_FIELDS
    if unknown_defaults:
        raise MappingError(f"Mapping 'defaults' has unsupported field(s): {', '.join(sorted(unknown_defaults))}")

    timestamp_format = raw.get("timestamp_format", "auto")
    if timestamp_format not in _TIMESTAMP_FORMATS:
        raise MappingError(f"Mapping 'timestamp_format' must be one of {sorted(_TIMESTAMP_FORMATS)}")

    missing_required = [name for name in _REQUIRED_FIELDS if name not in scalar_fields and name not in defaults]
    if missing_required:
        raise MappingError(
            "Mapping must provide a source path or default for required field(s): "
            + ", ".join(sorted(missing_required))
        )

    return MappingSpec(
        fields=scalar_fields,
        usage=usage_fields,
        scope=scope_fields,
        defaults=dict(defaults),
        timestamp_format=str(timestamp_format),
    )


def _resolve_path(row: dict[str, Any], path: str) -> Any:
    """Resolve a dotted path against nested dicts only (no list indexing, no eval)."""
    current: Any = row
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    return current


def _coerce_timestamp(value: Any, fmt: str):
    from datetime import UTC, datetime

    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
        number = float(value)
        if fmt == "epoch_millis" or (fmt == "auto" and number > 10_000_000_000):
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _coerce_int(value: Any) -> int:
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_row(row: dict[str, Any], mapping: MappingSpec) -> ModelRequestRecord | None:
    """Map one already-parsed JSON log line to a canonical record.

    Returns ``None`` when a required field cannot be resolved, so the caller
    can count the row as skipped instead of raising mid-batch.
    """
    if not isinstance(row, dict):
        return None
    values: dict[str, Any] = dict(mapping.defaults)
    for destination, path in mapping.fields.items():
        resolved = _resolve_path(row, path)
        if resolved is not None:
            values[destination] = resolved

    for name in _REQUIRED_FIELDS:
        if values.get(name) in (None, ""):
            return None

    def _usage_value(name: str) -> int:
        path = mapping.usage.get(name)
        return _coerce_int(_resolve_path(row, path)) if path else 0

    usage = TelemetryUsage(
        input_tokens=_usage_value("input_tokens"),
        output_tokens=_usage_value("output_tokens"),
        cached_tokens=_usage_value("cached_tokens"),
        reasoning_tokens=_usage_value("reasoning_tokens"),
        cache_write_tokens=_usage_value("cache_write_tokens"),
    )
    scope_values: dict[str, str] = {}
    for scope_name, scope_path in mapping.scope.items():
        resolved_scope = _resolve_path(row, scope_path)
        if resolved_scope is not None:
            scope_values[scope_name] = str(resolved_scope)

    timestamp = _coerce_timestamp(values.get("timestamp"), mapping.timestamp_format)
    if timestamp is None:
        return None

    error_category = values.get("error_category")
    if error_category is not None and error_category not in ERROR_CATEGORIES:
        error_category = None

    status_code = values.get("status_code")
    latency_ms = values.get("latency_ms")

    # The event id is derived only from already-extracted, non-content
    # dimensions (never the raw row), so re-importing the same file is
    # idempotent without hashing anything that might contain prompt text.
    record = ModelRequestRecord(
        source="import",
        event_id=deterministic_event_id(
            "import",
            str(values["provider"]),
            str(values["deployment_name"]),
            str(values["model_name"]),
            timestamp.isoformat(),
            usage.input_tokens,
            usage.output_tokens,
            usage.cached_tokens,
            status_code,
            latency_ms,
        ),
        timestamp=timestamp,
        provider=str(values["provider"]),
        deployment_name=str(values["deployment_name"]),
        model_name=str(values["model_name"]),
        service_tier=str(values.get("service_tier") or "standard"),
        deployment_mode=str(values.get("deployment_mode") or "unknown"),
        api=str(values["api"]) if values.get("api") else None,
        workload=str(values["workload"]) if values.get("workload") else None,
        scope=ResourceScope(**scope_values),
        usage=usage,
        latency_ms=_coerce_float(latency_ms) if latency_ms is not None else None,
        status_code=_coerce_int(status_code) if status_code is not None else None,
        error_category=error_category,
    )
    return record


def import_rows(rows: Iterable[dict[str, Any]], mapping: MappingSpec, *, max_rows: int = 1_000_000) -> GenericImportResult:
    result = GenericImportResult()
    seen: set[str] = set()
    for row in rows:
        if result.rows_seen >= max_rows:
            break
        result.rows_seen += 1
        record = map_row(row, mapping)
        if record is None:
            result.rows_skipped += 1
            continue
        if record.event_id in seen:
            result.duplicates_skipped += 1
            continue
        seen.add(record.event_id)
        result.records.append(record)
        result.rows_mapped += 1
    return result


def _iter_json_rows(text: str) -> Iterator[dict[str, Any]]:
    """Yield rows from a JSON array, a single JSON object, or JSONL text."""
    stripped = text.strip()
    if not stripped:
        return
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        parsed_any = False
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise GenericImportError(f"Input is not valid JSON or JSONL (line {line_number})") from exc
            parsed_any = True
            if isinstance(row, dict):
                yield row
        if not parsed_any:
            raise GenericImportError("Input is not valid JSON or JSONL")
        return
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
        return
    if isinstance(payload, dict):
        yield payload


def import_file(input_path: str | Path, mapping_path: str | Path, *, max_rows: int = 1_000_000) -> GenericImportResult:
    """Import an existing JSONL/JSON log using a validated YAML mapping. Offline only."""
    mapping = load_mapping(mapping_path)
    source = Path(input_path)
    if not source.is_file():
        raise GenericImportError(f"Input file not found: {source}")
    text = source.read_text(encoding="utf-8")
    return import_rows(_iter_json_rows(text), mapping, max_rows=max_rows)


def write_import(result: GenericImportResult, writer: TelemetryWriter, *, skip_existing: bool = True) -> int:
    """Persist imported records idempotently. Returns the number newly written."""
    return writer.write_all(result.records, skip_existing=skip_existing)


__all__ = [
    "GenericImportError",
    "GenericImportResult",
    "MappingError",
    "MappingSpec",
    "import_file",
    "import_rows",
    "load_mapping",
    "map_row",
    "write_import",
]
