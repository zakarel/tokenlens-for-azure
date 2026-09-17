"""Canonical, privacy-first telemetry records (schema version 3).

Every collection adapter (SDK wrapper, OpenTelemetry importer, Azure Monitor
collector, generic import) emits one of these two records so provider-specific
payloads never leak into analysis or report logic.

The records are deliberately contentless: there is no field for prompts,
responses, system messages, tool arguments, retrieved text, headers, or
credentials. :mod:`tokenlens.telemetry.privacy` enforces that at runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = (1, 2, 3)

TelemetrySource = Literal["sdk_wrapper", "otel", "azure_monitor", "import"]
RecordType = Literal["model_request", "foundry_metric_bucket"]

#: Normalized, allow-listed error categories. Provider exception strings are
#: never serialized because they can contain request content or endpoints.
ERROR_CATEGORIES = (
    "rate_limited",
    "authentication",
    "authorization",
    "bad_request",
    "not_found",
    "content_filtered",
    "timeout",
    "connection",
    "server_error",
    "cancelled",
    "unknown",
)

ErrorCategory = Literal[
    "rate_limited",
    "authentication",
    "authorization",
    "bad_request",
    "not_found",
    "content_filtered",
    "timeout",
    "connection",
    "server_error",
    "cancelled",
    "unknown",
]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


class _Canonical(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TelemetryUsage(_Canonical):
    """Aggregate token usage. Counters are nonnegative and never estimated."""

    input_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class ContentFeatures(_Canonical):
    """Structural measurements of a request. Never the content itself."""

    system_prompt_tokens: int | None = Field(default=None, ge=0)
    message_count: int | None = Field(default=None, ge=0)
    tool_definition_tokens: int | None = Field(default=None, ge=0)
    tool_count: int | None = Field(default=None, ge=0)
    retrieval_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)


class Fingerprints(_Canonical):
    """Keyed HMAC fingerprints used for repeated-prefix analysis."""

    system_prompt: str | None = None
    tool_schema: str | None = None
    retrieval_context: str | None = None

    @field_validator("system_prompt", "tool_schema", "retrieval_context")
    @classmethod
    def _must_be_keyed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("hmac-sha256:"):
            raise ValueError("fingerprints must be keyed HMAC-SHA256 values")
        return value


class ResourceScope(_Canonical):
    """Local, analyst-chosen labels. Never Azure resource or tenant identifiers."""

    resource_name: str | None = None
    project_name: str | None = None
    workload: str | None = None
    environment: str | None = None


class ModelRequestRecord(_Canonical):
    """One canonical request-level telemetry record."""

    schema_version: Literal[3] = SCHEMA_VERSION
    record_type: Literal["model_request"] = "model_request"
    source: TelemetrySource
    event_id: str
    timestamp: datetime
    provider: str
    deployment_name: str
    model_name: str
    service_tier: str = "standard"
    deployment_mode: str = "unknown"
    api: str | None = None
    workload: str | None = None
    scope: ResourceScope = Field(default_factory=ResourceScope)
    usage: TelemetryUsage = Field(default_factory=TelemetryUsage)
    latency_ms: float | None = Field(default=None, ge=0)
    status_code: int | None = Field(default=None, ge=0)
    error_category: ErrorCategory | None = None
    retry_count: int = Field(default=0, ge=0)
    retry_of: str | None = None
    streamed: bool = False
    sampled: bool = True
    sample_rate: float = Field(default=1.0, gt=0, le=1)
    content_features: ContentFeatures = Field(default_factory=ContentFeatures)
    fingerprints: Fingerprints = Field(default_factory=Fingerprints)

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @field_validator("deployment_mode")
    @classmethod
    def _no_implicit_global(cls, value: str) -> str:
        # An unknown deployment mode must stay unknown: PTU sizing and pricing
        # differ between Global and Regional, so guessing is never acceptable.
        return value or "unknown"


class BucketMetrics(_Canonical):
    """Aggregate metrics for one collection bucket.

    ``None`` means the source did not provide the metric. Zero means the source
    provided the metric and it was genuinely zero.
    """

    input_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    requests: int | None = Field(default=None, ge=0)
    successful_requests: int | None = Field(default=None, ge=0)
    throttled_requests: int | None = Field(default=None, ge=0)
    failed_requests: int | None = Field(default=None, ge=0)
    average_latency_ms: float | None = Field(default=None, ge=0)
    p50_latency_ms: float | None = Field(default=None, ge=0)
    p95_latency_ms: float | None = Field(default=None, ge=0)
    p99_latency_ms: float | None = Field(default=None, ge=0)


class MetricBucketRecord(_Canonical):
    """One canonical aggregate telemetry bucket."""

    schema_version: Literal[3] = SCHEMA_VERSION
    record_type: Literal["foundry_metric_bucket"] = "foundry_metric_bucket"
    source: TelemetrySource = "azure_monitor"
    event_id: str
    timestamp: datetime
    bucket_minutes: int = Field(default=5, gt=0)
    provider: str = "azure_foundry"
    deployment_name: str
    model_name: str
    #: Model version reported as a separate dimension. It is kept separate
    #: rather than concatenated so the exact model identity is preserved
    #: without inventing a combined model ID that no catalog contains.
    model_version: str | None = None
    service_tier: str = "standard"
    deployment_mode: str = "unknown"
    scope: ResourceScope = Field(default_factory=ResourceScope)
    metrics: BucketMetrics = Field(default_factory=BucketMetrics)
    missing_metrics: list[str] = Field(default_factory=list)
    #: Canonical field -> the exact source metric that populated it. Provenance
    #: is recorded per field because one bucket is coalesced from several metric
    #: series that expose different dimensions.
    metric_provenance: dict[str, str] = Field(default_factory=dict)
    #: Per-status request counts when the source exposed a status dimension.
    #: ``None`` means outcomes were not reported; an empty mapping is never
    #: written, so a genuine zero 429 stays distinguishable from unavailable.
    status_codes: dict[str, int] | None = None
    #: ``complete`` only when every status-code series for the bucket was read,
    #: which is what makes a zero rate-limit count trustworthy.
    outcome_coverage: Literal["complete", "partial", "unavailable"] = "unavailable"
    collected_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    #: Elapsed buckets expected in the collection window. It lets analysis
    #: reconstruct the full timeline without storing an idle record per bucket.
    expected_buckets: int | None = Field(default=None, ge=0)

    @field_validator("timestamp", "collected_at", "window_start", "window_end")
    @classmethod
    def _utc(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value) if value is not None else None


CanonicalRecord = ModelRequestRecord | MetricBucketRecord


def record_json(record: CanonicalRecord) -> dict[str, Any]:
    """Serialize a canonical record with UTC ``Z`` timestamps and no ``None`` noise."""
    payload = record.model_dump(mode="json", exclude_none=True)
    return payload


def parse_record(raw: dict[str, Any]) -> CanonicalRecord:
    """Parse a canonical record, rejecting unknown or content-bearing fields."""
    record_type = raw.get("record_type")
    if record_type == "foundry_metric_bucket":
        return MetricBucketRecord.model_validate(raw)
    if record_type == "model_request":
        return ModelRequestRecord.model_validate(raw)
    raise ValueError("record_type must be model_request or foundry_metric_bucket")


def is_canonical(raw: object) -> bool:
    return (
        isinstance(raw, dict)
        and raw.get("record_type") in {"model_request", "foundry_metric_bucket"}
        and raw.get("schema_version") == SCHEMA_VERSION
    )


def deterministic_event_id(*parts: object) -> str:
    """Stable local identifier used for bucket idempotency and span dedupe.

    The identifier is derived only from analytical dimensions already present in
    the record, so it never introduces a new identifier into telemetry.
    """
    import hashlib

    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8"))
    return digest.hexdigest()[:32]
