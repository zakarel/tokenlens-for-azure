"""First-class aggregate telemetry semantics.

Azure Monitor returns *aggregate buckets*, not requests. A bucket is an interval,
so the number of buckets is a property of the collection window rather than of
the traffic. Treating one bucket as one request is how a 14-day idle window
becomes "4,032 requests".

This module keeps four counts distinct:

``elapsed``
    Intervals expected inside the collection window.
``observed``
    Intervals for which the source returned a data point.
``active``
    Observed intervals with nonzero token or request volume.
``requests``
    The sum of the request metric — never a bucket count.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .models import AggregateAnalysisSummary, TraceRecord

TelemetryKind = Literal["request", "aggregate"]

#: Canonical fields whose availability is reported per analysis.
COVERAGE_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "requests",
    "request_outcomes",
    "latency",
    "retries",
)


class MixedTelemetryError(ValueError):
    """Raised when request-level and aggregate telemetry are analyzed together.

    Azure Monitor buckets and SDK request records usually describe the *same*
    traffic. Summing them double counts tokens, requests, and cost, so the
    default is to reject the combination rather than publish a number that
    cannot be reconciled with an invoice.
    """


def telemetry_kind(record: TraceRecord) -> TelemetryKind:
    metadata = record.metadata or {}
    return "aggregate" if metadata.get("record_type") == "foundry_metric_bucket" else "request"


def source_kinds(records: Iterable[TraceRecord]) -> set[TelemetryKind]:
    return {telemetry_kind(record) for record in records}


def bucket_metrics(record: TraceRecord) -> dict[str, Any]:
    metadata = record.metadata or {}
    metrics = metadata.get("metrics")
    return dict(metrics) if isinstance(metrics, Mapping) else {}


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _bucket_minutes(records: list[TraceRecord]) -> int:
    for record in records:
        value = (record.metadata or {}).get("bucket_minutes")
        if isinstance(value, int) and value > 0:
            return value
    return 5


def _window(records: list[TraceRecord]) -> tuple[datetime | None, datetime | None]:
    starts = [
        stamp
        for record in records
        if (stamp := _timestamp((record.metadata or {}).get("window_start"))) is not None
    ]
    ends = [
        stamp
        for record in records
        if (stamp := _timestamp((record.metadata or {}).get("window_end"))) is not None
    ]
    return (min(starts) if starts else None, max(ends) if ends else None)


def _sum_optional(values: list[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def aggregate_summary(records: list[TraceRecord]) -> AggregateAnalysisSummary:
    """Summarize aggregate buckets without ever treating a bucket as a request."""
    buckets = [record for record in records if telemetry_kind(record) == "aggregate"]
    bucket_minutes = _bucket_minutes(buckets)
    window_start, window_end = _window(buckets)
    stamps = [stamp for record in buckets if (stamp := _timestamp(record.timestamp)) is not None]
    observed_keys: set[datetime] = set()
    active_keys: set[datetime] = set()
    active_days: set[Any] = set()
    inputs: list[int | None] = []
    cached: list[int | None] = []
    outputs: list[int | None] = []
    requests: list[int | None] = []
    successful: list[int | None] = []
    rate_limited: list[int | None] = []
    failed: list[int | None] = []
    latency_reported = 0
    coverage_states: set[str] = set()
    missing: set[str] = set()
    status_codes: dict[str, int] = {}

    for record in buckets:
        stamp = _timestamp(record.timestamp)
        metrics = bucket_metrics(record)
        metadata = record.metadata or {}
        for item in metadata.get("missing_metrics") or []:
            missing.add(str(item))
        coverage_states.add(str(metadata.get("outcome_coverage") or "unavailable"))
        for code, count in (metadata.get("status_codes") or {}).items():
            status_codes[str(code)] = status_codes.get(str(code), 0) + int(count)
        inputs.append(metrics.get("input_tokens"))
        cached.append(metrics.get("cached_tokens"))
        outputs.append(metrics.get("output_tokens"))
        requests.append(metrics.get("requests"))
        successful.append(metrics.get("successful_requests"))
        rate_limited.append(metrics.get("throttled_requests"))
        failed.append(metrics.get("failed_requests"))
        if metrics.get("average_latency_ms") is not None or metrics.get("p50_latency_ms") is not None:
            latency_reported += 1
        if stamp is not None:
            observed_keys.add(stamp)
            volume = (metrics.get("input_tokens") or 0) + (metrics.get("output_tokens") or 0)
            if volume > 0 or (metrics.get("requests") or 0) > 0:
                active_keys.add(stamp)
                active_days.add(stamp.date())

    expected = max(
        (int(value) for record in buckets if isinstance(value := (record.metadata or {}).get("expected_buckets"), int)),
        default=0,
    )
    if not expected and window_start and window_end:
        expected = int(max(0.0, (window_end - window_start).total_seconds()) // (bucket_minutes * 60))
    if not expected and stamps:
        span = (max(stamps) - min(stamps)).total_seconds()
        expected = int(span // (bucket_minutes * 60)) + 1

    if window_start and window_end:
        observed_days = max(0.0, (window_end - window_start).total_seconds() / 86_400)
    elif stamps:
        observed_days = max(
            bucket_minutes / 1440,
            ((max(stamps) - min(stamps)) + timedelta(minutes=bucket_minutes)).total_seconds() / 86_400,
        )
    else:
        observed_days = 0.0

    outcome_coverage: Literal["complete", "partial", "unavailable"]
    if coverage_states == {"complete"} and buckets:
        outcome_coverage = "complete"
    elif "complete" in coverage_states or "partial" in coverage_states:
        outcome_coverage = "partial"
    elif any(value is not None for value in successful) or any(value is not None for value in rate_limited):
        outcome_coverage = "partial"
    else:
        outcome_coverage = "unavailable"

    requests_observed = _sum_optional(requests)
    return AggregateAnalysisSummary(
        metric_buckets_read=len(buckets),
        elapsed_buckets=expected,
        observed_buckets=len(observed_keys),
        active_buckets=len(active_keys),
        idle_buckets=max(0, len(observed_keys) - len(active_keys)),
        requests_observed=requests_observed,
        successful_requests=_sum_optional(successful) if outcome_coverage != "unavailable" else None,
        rate_limited_requests=_sum_optional(rate_limited) if outcome_coverage != "unavailable" else None,
        failed_requests=_sum_optional(failed) if outcome_coverage != "unavailable" else None,
        outcome_coverage=outcome_coverage,
        status_codes=dict(sorted(status_codes.items())),
        input_tokens=_sum_optional(inputs),
        cached_tokens=_sum_optional(cached),
        output_tokens=_sum_optional(outputs),
        latency_coverage_percent=round(latency_reported / len(buckets) * 100, 1) if buckets else 0.0,
        bucket_minutes=bucket_minutes,
        window_start=window_start or (min(stamps) if stamps else None),
        window_end=window_end or ((max(stamps) + timedelta(minutes=bucket_minutes)) if stamps else None),
        observed_days=round(observed_days, 2),
        active_days=len(active_days),
        missing_metrics=sorted(missing),
    )


def field_coverage(summary: AggregateAnalysisSummary) -> dict[str, bool]:
    """Report which canonical fields the aggregate source actually provided."""
    return {
        "input_tokens": summary.input_tokens is not None,
        "cached_tokens": summary.cached_tokens is not None,
        "output_tokens": summary.output_tokens is not None,
        "requests": summary.requests_observed is not None,
        "request_outcomes": summary.outcome_coverage != "unavailable",
        "latency": summary.latency_coverage_percent > 0,
        # Azure Monitor exposes no retry metric, so retries are unavailable for
        # aggregate telemetry rather than zero.
        "retries": False,
    }


__all__ = [
    "COVERAGE_FIELDS",
    "MixedTelemetryError",
    "TelemetryKind",
    "aggregate_summary",
    "bucket_metrics",
    "field_coverage",
    "source_kinds",
    "telemetry_kind",
]
