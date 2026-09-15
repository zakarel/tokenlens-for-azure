"""Azure Monitor metric collection for PTU analysis.

This module is explicit about two things:

1. **Network access.** Nothing here contacts Azure until a caller constructs a
   live client. Every function accepts an injected client, so tests and dry runs
   never reach the network.
2. **Capability.** Azure Monitor metric names and dimensions differ between
   Azure OpenAI, Claude, and partner model deployments. The mapping below is an
   explicit, reviewable table rather than an assumption, and any metric the
   resource does not expose is reported as missing instead of zero.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Protocol, Sequence

from ..telemetry.schema import (
    BucketMetrics,
    MetricBucketRecord,
    ResourceScope,
    deterministic_event_id,
)

#: Canonical field -> candidate Azure Monitor metric names, most specific first.
#: Confirm against the resource's own metric-definitions API before relying on a
#: candidate: availability varies by resource kind, model publisher, and region.
METRIC_MAP: dict[str, tuple[str, ...]] = {
    "input_tokens": ("ProcessedPromptTokens", "PromptTokenCount", "InputTokens"),
    "output_tokens": ("GeneratedTokens", "CompletionTokenCount", "OutputTokens"),
    "cached_tokens": ("ProcessedCachedPromptTokens", "CachedPromptTokens"),
    "requests": ("AzureOpenAIRequests", "ModelRequests", "TotalCalls"),
    "successful_requests": ("SuccessfulCalls",),
    "throttled_requests": ("ThrottledCalls", "AzureOpenAIThrottledRequests"),
    "failed_requests": ("TotalErrors", "ClientErrors", "ServerErrors"),
    # Azure Monitor exposes an average latency metric for these resources, not
    # percentiles. TokenLens records it as an average and leaves the percentile
    # fields empty rather than presenting an average as a P50/P95.
    "average_latency_ms": ("NormalizedTimeToFirstByte", "Latency"),
}

#: Which canonical fields each supported deployment family is expected to
#: provide. Anything absent is surfaced in ``missing_metrics``.
CAPABILITY_MATRIX: dict[str, frozenset[str]] = {
    "azure_openai": frozenset(
        {
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "requests",
            "successful_requests",
            "throttled_requests",
            "failed_requests",
            "average_latency_ms",
        }
    ),
    "claude_foundry": frozenset({"input_tokens", "output_tokens", "requests", "throttled_requests"}),
    "partner_model": frozenset({"requests"}),
    "unknown": frozenset(),
}

AGGREGATIONS: dict[str, str] = {
    "input_tokens": "Total",
    "output_tokens": "Total",
    "cached_tokens": "Total",
    "requests": "Total",
    "successful_requests": "Total",
    "throttled_requests": "Total",
    "failed_requests": "Total",
    "average_latency_ms": "Average",
}

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 0.5


class CollectorError(RuntimeError):
    """Raised when collection cannot proceed safely."""


class AuthorizationError(CollectorError):
    """Raised when the signed-in principal cannot read the resource's metrics."""


class MetricsClient(Protocol):
    """The minimal Azure Monitor surface TokenLens depends on.

    Implemented by the real ``azure-monitor-querymetrics`` client and by test
    doubles. Keeping it narrow is what makes offline testing possible.
    """

    def query_resource(
        self,
        resource_uri: str,
        metric_names: Sequence[str],
        *,
        timespan: Any,
        granularity: Any,
        aggregations: Sequence[str],
        filter: str | None = None,
    ) -> Any: ...


class ResourceClient(Protocol):
    """Discovery surface: accounts and deployments in one explicit subscription."""

    def list_accounts(self, subscription_id: str, resource_group: str | None = None) -> Iterable[dict[str, Any]]: ...

    def list_deployments(self, subscription_id: str, resource_group: str, account: str) -> Iterable[dict[str, Any]]: ...

    def list_metric_definitions(self, resource_uri: str) -> Iterable[str]: ...


@dataclass
class CollectionWindow:
    start: datetime
    end: datetime
    granularity_minutes: int = 5

    @classmethod
    def for_days(cls, days: int, *, now: datetime | None = None, granularity_minutes: int = 5) -> "CollectionWindow":
        if days <= 0:
            raise CollectorError("lookback days must be positive")
        end = (now or datetime.now(UTC)).astimezone(UTC).replace(second=0, microsecond=0)
        return cls(start=end - timedelta(days=days), end=end, granularity_minutes=granularity_minutes)


@dataclass
class CollectionResult:
    records: list[MetricBucketRecord] = field(default_factory=list)
    missing_metrics: list[str] = field(default_factory=list)
    unsupported_metrics: list[str] = field(default_factory=list)
    requested_metrics: list[str] = field(default_factory=list)
    pages_read: int = 0
    retries: int = 0
    duplicate_buckets_skipped: int = 0
    window_start: datetime | None = None
    window_end: datetime | None = None
    collected_at: datetime | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "buckets": len(self.records),
            "pages_read": self.pages_read,
            "retries": self.retries,
            "duplicate_buckets_skipped": self.duplicate_buckets_skipped,
            "missing_metrics": list(self.missing_metrics),
            "unsupported_metrics": list(self.unsupported_metrics),
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
        }


def resource_uri(subscription_id: str, resource_group: str, account: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account}"
    )


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def _call_with_retries(
    call: Callable[[], Any],
    result: CollectionResult,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Retry only retryable responses, with bounded exponential backoff."""
    for attempt in range(MAX_ATTEMPTS):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - re-classified and re-raised below
            status = _status_code(exc)
            if status in {401, 403}:
                raise AuthorizationError(
                    "Azure Monitor rejected the request for this resource. Confirm the signed-in principal "
                    "has Monitoring Reader on the selected account."
                ) from exc
            if status not in RETRYABLE_STATUS or attempt == MAX_ATTEMPTS - 1:
                raise CollectorError(f"Azure Monitor request failed (status {status or 'unknown'})") from exc
            result.retries += 1
            sleep(BASE_BACKOFF_SECONDS * (2**attempt))
    raise CollectorError("Azure Monitor request failed after retries")


def _metric_value(entry: Any, aggregation: str) -> float | None:
    key = {"Total": "total", "Average": "average", "Count": "count", "Maximum": "maximum", "Minimum": "minimum"}[aggregation]
    if isinstance(entry, dict):
        value = entry.get(key)
    else:
        value = getattr(entry, key, None)
    return float(value) if value is not None else None


def _entry_timestamp(entry: Any) -> datetime | None:
    value = (
        entry.get("timeStamp")
        if isinstance(entry, dict)
        else getattr(entry, "timestamp", None) or getattr(entry, "time_stamp", None)
    )
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _iter_series(response: Any) -> Iterable[tuple[str, dict[str, str], Any]]:
    """Yield ``(metric_name, dimensions, entry)`` from a metrics response."""
    metrics = response.get("metrics") if isinstance(response, dict) else getattr(response, "metrics", [])
    for metric in metrics or []:
        name = metric.get("name") if isinstance(metric, dict) else getattr(metric, "name", "")
        if isinstance(name, dict):
            name = name.get("value", "")
        series = metric.get("timeseries") if isinstance(metric, dict) else getattr(metric, "timeseries", [])
        for item in series or []:
            raw_dimensions = (
                item.get("metadata_values")
                if isinstance(item, dict)
                else getattr(item, "metadata_values", None) or getattr(item, "metadatavalues", None)
            ) or []
            dimensions: dict[str, str] = {}
            for dimension in raw_dimensions:
                if isinstance(dimension, dict):
                    key = dimension.get("name")
                    if isinstance(key, dict):
                        key = key.get("value")
                    dimensions[str(key)] = str(dimension.get("value"))
                else:
                    key = getattr(getattr(dimension, "name", None), "value", None) or getattr(dimension, "name", None)
                    dimensions[str(key)] = str(getattr(dimension, "value", ""))
            entries = item.get("data") if isinstance(item, dict) else getattr(item, "data", [])
            for entry in entries or []:
                yield str(name), dimensions, entry


def supported_metrics(
    available: Iterable[str],
    *,
    family: str = "azure_openai",
) -> tuple[dict[str, str], list[str]]:
    """Resolve canonical fields to concrete metric names for this resource.

    Returns the resolved mapping and the canonical fields the resource cannot
    provide. Nothing is guessed: a field is only resolved when the resource's own
    metric definitions contain one of its documented candidates.
    """
    names = {str(item) for item in available}
    expected = CAPABILITY_MATRIX.get(family, CAPABILITY_MATRIX["unknown"])
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for field_name, candidates in METRIC_MAP.items():
        match = next((candidate for candidate in candidates if candidate in names), None)
        if match is not None:
            resolved[field_name] = match
        elif field_name in expected:
            missing.append(field_name)
    return resolved, missing


def collect_metrics(
    *,
    metrics_client: MetricsClient,
    subscription_id: str,
    resource_group: str,
    account: str,
    window: CollectionWindow,
    available_metrics: Iterable[str],
    deployments: Sequence[str] | None = None,
    family: str = "azure_openai",
    scope: ResourceScope | None = None,
    deployment_modes: dict[str, str] | None = None,
    default_deployment_mode: str = "unknown",
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CollectionResult:
    """Collect five-minute aggregate buckets for one explicitly selected account.

    Only aggregate counters are requested. No prompt, response, header, user,
    or IP data is collected, and no resource identifier is written into the
    resulting telemetry records.

    ``default_deployment_mode`` applies to every discovered deployment that has
    no explicit entry in ``deployment_modes``, so an operator who collects an
    entire account still records the mode they confirmed. It stays ``unknown``
    unless the caller supplies one: PTU sizing and pricing differ between Global
    and Regional deployments, so the mode is never inferred.
    """
    result = CollectionResult(window_start=window.start, window_end=window.end, collected_at=now())
    resolved, missing = supported_metrics(available_metrics, family=family)
    if not resolved:
        raise CollectorError(
            "None of the documented token or request metrics are available for this resource. "
            "Confirm the resource kind and its metric definitions before collecting."
        )
    result.missing_metrics = missing
    result.unsupported_metrics = sorted(set(METRIC_MAP) - set(resolved))
    result.requested_metrics = sorted(set(resolved.values()))

    uri = resource_uri(subscription_id, resource_group, account)
    dimension_filter = (
        " or ".join(f"ModelDeploymentName eq '{name}'" for name in deployments) if deployments else "ModelDeploymentName eq '*'"
    )
    buckets: dict[tuple[datetime, str, str, str], dict[str, float]] = defaultdict(dict)
    dimension_values: dict[tuple[datetime, str, str, str], dict[str, str]] = {}
    inverse = {value: key for key, value in resolved.items()}

    def fetch(page_token: str | None) -> Any:
        kwargs: dict[str, Any] = {
            "timespan": (window.start, window.end),
            "granularity": timedelta(minutes=window.granularity_minutes),
            "aggregations": sorted({AGGREGATIONS[field_name] for field_name in resolved}),
            "filter": dimension_filter,
        }
        if page_token:
            kwargs["page_token"] = page_token
        return metrics_client.query_resource(uri, result.requested_metrics, **kwargs)

    page_token: str | None = None
    while True:
        response = _call_with_retries(lambda token=page_token: fetch(token), result, sleep=sleep)
        result.pages_read += 1
        for metric_name, dimensions, entry in _iter_series(response):
            field_name = inverse.get(metric_name)
            if field_name is None:
                continue
            stamp = _entry_timestamp(entry)
            if stamp is None:
                continue
            value = _metric_value(entry, AGGREGATIONS[field_name])
            if value is None:
                # A null point means Azure Monitor reported no data for that
                # bucket. It is skipped, never recorded as a zero.
                continue
            deployment = dimensions.get("ModelDeploymentName") or dimensions.get("DeploymentName") or "unknown"
            model = dimensions.get("ModelName") or dimensions.get("Model") or "unknown"
            version = dimensions.get("ModelVersion") or None
            model_name = model
            key = (stamp, deployment, model_name, version or "")
            dimension_values[key] = dimensions
            if field_name in buckets[key]:
                result.duplicate_buckets_skipped += 1
                continue
            buckets[key][field_name] = value
        page_token = (
            response.get("nextPageToken") if isinstance(response, dict) else getattr(response, "next_page_token", None)
        )
        if not page_token:
            break

    for (stamp, deployment, model_name, model_version), values in sorted(buckets.items()):
        metrics = BucketMetrics(
            input_tokens=_as_int(values.get("input_tokens")),
            cached_tokens=_as_int(values.get("cached_tokens")),
            output_tokens=_as_int(values.get("output_tokens")),
            requests=_as_int(values.get("requests")),
            successful_requests=_as_int(values.get("successful_requests")),
            throttled_requests=_as_int(values.get("throttled_requests")),
            failed_requests=_as_int(values.get("failed_requests")),
            average_latency_ms=values.get("average_latency_ms"),
        )
        bucket_missing = sorted(set(missing) | {name for name in METRIC_MAP if name in resolved and name not in values})
        result.records.append(
            MetricBucketRecord(
                source="azure_monitor",
                # Deterministic per (bucket, deployment, model): re-running the
                # same window produces identical identifiers, so an import can
                # detect and drop duplicates.
                event_id=deterministic_event_id(
                    "azure_monitor", stamp.isoformat(), deployment, model_name, model_version
                ),
                timestamp=stamp,
                bucket_minutes=window.granularity_minutes,
                deployment_name=deployment,
                model_name=model_name,
                model_version=model_version or None,
                deployment_mode=(deployment_modes or {}).get(deployment, default_deployment_mode),
                scope=scope or ResourceScope(),
                metrics=metrics,
                missing_metrics=bucket_missing,
                collected_at=result.collected_at,
                window_start=window.start,
                window_end=window.end,
            )
        )
    return result


def _as_int(value: float | None) -> int | None:
    return None if value is None else max(0, int(round(value)))


def dedupe(records: Sequence[MetricBucketRecord]) -> list[MetricBucketRecord]:
    """Drop repeated buckets by their deterministic identifier."""
    seen: set[str] = set()
    unique: list[MetricBucketRecord] = []
    for record in records:
        if record.event_id in seen:
            continue
        seen.add(record.event_id)
        unique.append(record)
    return unique
