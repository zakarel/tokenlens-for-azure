"""Private, bounded collection manifests for requested and returned telemetry."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .schema import BucketMetrics, MetricBucketRecord

MetricState = Literal["complete", "partial", "unavailable"]
CollectionState = Literal["succeeded", "failed", "skipped"]
CollectionSource = Literal["azure_monitor", "sdk", "opentelemetry", "appinsights", "generic_import"]


class MetricCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: MetricState
    points_reported: int = Field(ge=0)
    expected_points: int = Field(ge=0)
    coverage_percent: float = Field(ge=0, le=100)


class GapInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: datetime
    missing_buckets: int = Field(gt=0)


class DeploymentCollectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_ref: str
    model_name: str | None = None
    model_version: str | None = None
    modality: str = "unknown"
    usage_bases: tuple[str, ...] = ()
    capability_catalog_version: str | None = None
    collection_state: CollectionState
    requested_metrics: list[str] = Field(default_factory=list)
    returned_metrics: list[str] = Field(default_factory=list)
    metric_coverage: dict[str, MetricCoverage] = Field(default_factory=dict)
    missing_metrics: list[str] = Field(default_factory=list)
    unsupported_metrics: list[str] = Field(default_factory=list)
    requested_start: datetime
    requested_end: datetime
    returned_start: datetime | None = None
    returned_end: datetime | None = None
    bucket_minutes: int = Field(gt=0)
    expected_buckets: int = Field(ge=0)
    observed_buckets: int = Field(ge=0)
    active_buckets: int = Field(ge=0)
    requested_days: float = Field(ge=0)
    observed_days: int = Field(ge=0)
    complete_days: int = Field(ge=0)
    partial_days: int = Field(ge=0)
    missing_days: int = Field(ge=0)
    gap_intervals: list[GapInterval] = Field(default_factory=list)
    gap_intervals_truncated: int = Field(default=0, ge=0)
    pages_read: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    identity_conflict_count: int = Field(default=0, ge=0)
    error_category: str | None = None


class CollectionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
    run_id: str
    source: CollectionSource = "azure_monitor"
    generated_at: datetime
    requested_start: datetime
    requested_end: datetime
    bucket_minutes: int = Field(gt=0)
    expected_buckets_per_deployment: int = Field(ge=0)
    pages_read: int = Field(default=0, ge=0)
    retries: int = Field(default=0, ge=0)
    deployments: list[DeploymentCollectionManifest] = Field(default_factory=list)
    source_capabilities: dict[str, bool] = Field(default_factory=dict)
    metric_errors: dict[str, str] = Field(default_factory=dict)
    records_seen: int = Field(default=0, ge=0)
    records_mapped: int = Field(default=0, ge=0)
    records_skipped: int = Field(default=0, ge=0)
    duplicates_skipped: int = Field(default=0, ge=0)


def _coverage(points: int, expected: int) -> MetricCoverage:
    percent = round(min(100.0, points / expected * 100), 1) if expected else 0.0
    state: MetricState = (
        "complete" if expected > 0 and points >= expected else "partial" if points else "unavailable"
    )
    return MetricCoverage(
        state=state,
        points_reported=points,
        expected_points=expected,
        coverage_percent=percent,
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _day_counts(
    start: datetime,
    end: datetime,
    observed: set[datetime],
    interval: timedelta,
) -> tuple[int, int, int, int]:
    expected_by_day: dict[date, int] = {}
    observed_by_day: dict[date, int] = {}
    bucket_count = int(max(0.0, (end - start).total_seconds()) // interval.total_seconds())
    for index in range(bucket_count):
        stamp = start + index * interval
        expected_by_day[stamp.date()] = expected_by_day.get(stamp.date(), 0) + 1
    for stamp in observed:
        observed_by_day[stamp.date()] = observed_by_day.get(stamp.date(), 0) + 1
    complete = sum(
        observed_by_day.get(day, 0) >= expected
        for day, expected in expected_by_day.items()
        if expected
    )
    partial = sum(
        0 < observed_by_day.get(day, 0) < expected
        for day, expected in expected_by_day.items()
        if expected
    )
    missing = sum(
        observed_by_day.get(day, 0) == 0
        for day, expected in expected_by_day.items()
        if expected
    )
    return len(expected_by_day), complete, partial, missing


def _gaps(
    start: datetime,
    end: datetime,
    observed: set[datetime],
    interval: timedelta,
    *,
    max_intervals: int = 200,
) -> tuple[list[GapInterval], int]:
    expected = int(max(0.0, (end - start).total_seconds()) // interval.total_seconds())
    observed_indices = sorted(
        {
            int((stamp - start).total_seconds() // interval.total_seconds())
            for stamp in observed
            if start <= stamp < end
        }
    )
    gaps: list[GapInterval] = []
    truncated = 0
    cursor = 0
    for index in observed_indices:
        if index > cursor:
            if len(gaps) < max_intervals:
                gaps.append(
                    GapInterval(
                        start=start + cursor * interval,
                        end=start + index * interval,
                        missing_buckets=index - cursor,
                    )
                )
            else:
                truncated += 1
        cursor = max(cursor, index + 1)
    if cursor < expected:
        if len(gaps) < max_intervals:
            gaps.append(
                GapInterval(
                    start=start + cursor * interval,
                    end=start + expected * interval,
                    missing_buckets=expected - cursor,
                )
            )
        else:
            truncated += 1
    return gaps, truncated


def build_deployment_manifest(
    *,
    run_id: str,
    identity_scope: str,
    deployment_name: str,
    model_name: str | None,
    model_version: str | None,
    modality: str,
    usage_bases: Sequence[str],
    capability_catalog_version: str | None,
    collection_state: CollectionState,
    requested_start: datetime,
    requested_end: datetime,
    bucket_minutes: int,
    records: Sequence[MetricBucketRecord] = (),
    requested_metrics: Sequence[str] = (),
    missing_metrics: Sequence[str] = (),
    unsupported_metrics: Sequence[str] = (),
    pages_read: int = 0,
    retries: int = 0,
    identity_conflict_count: int = 0,
    error_category: str | None = None,
) -> DeploymentCollectionManifest:
    """Build sanitized coverage facts for one deployment without resource IDs."""
    start = _utc(requested_start)
    end = _utc(requested_end)
    interval = timedelta(minutes=bucket_minutes)
    expected = int(max(0.0, (end - start).total_seconds()) // interval.total_seconds())
    timestamps = {
        _utc(record.timestamp)
        for record in records
        if start <= _utc(record.timestamp) < end
    }
    records = [
        record for record in records
        if start <= _utc(record.timestamp) < end
    ]
    returned_start = min(timestamps) if timestamps else None
    returned_end = max(timestamps) + interval if timestamps else None
    record_missing = {
        metric for record in records for metric in record.missing_metrics
    }
    fields = sorted(
        (
            set(missing_metrics)
            | record_missing
            | {
                field
                for record in records
                for field in record.metric_provenance
            }
            | {
                field
                for record in records
                for field in BucketMetrics.model_fields
                if getattr(record.metrics, field) is not None
            }
        )
        & set(BucketMetrics.model_fields)
    )
    metric_coverage = {
        field: _coverage(
            sum(getattr(record.metrics, field) is not None for record in records),
            expected,
        )
        for field in fields
    }
    actual_source_metrics = sorted(
        {
            source
            for record in records
            for source in record.metric_provenance.values()
            if source
        }
    )
    missing = set(missing_metrics) | record_missing
    missing.update(
        field for field, coverage in metric_coverage.items() if coverage.state != "complete"
    )
    active = sum(
        any(
            (getattr(record.metrics, field) or 0) > 0
            for field in ("input_tokens", "output_tokens", "requests")
        )
        for record in records
    )
    _, complete_days, partial_days, missing_days = _day_counts(
        start, end, timestamps, interval
    )
    gaps, truncated = _gaps(start, end, timestamps, interval)
    deployment_ref = sha256(
        f"{run_id}\0{identity_scope}\0{deployment_name}".encode("utf-8")
    ).hexdigest()[:16]
    return DeploymentCollectionManifest(
        deployment_ref=deployment_ref,
        model_name=model_name,
        model_version=model_version,
        modality=modality,
        usage_bases=tuple(usage_bases),
        capability_catalog_version=capability_catalog_version,
        collection_state=collection_state,
        requested_metrics=sorted(set(requested_metrics)),
        returned_metrics=actual_source_metrics,
        metric_coverage=metric_coverage,
        missing_metrics=sorted(missing),
        unsupported_metrics=sorted(set(unsupported_metrics)),
        requested_start=start,
        requested_end=end,
        returned_start=returned_start,
        returned_end=returned_end,
        bucket_minutes=bucket_minutes,
        expected_buckets=expected,
        observed_buckets=len(timestamps),
        active_buckets=active,
        requested_days=round(max(0.0, (end - start).total_seconds() / 86_400), 2),
        observed_days=len({stamp.date() for stamp in timestamps}),
        complete_days=complete_days,
        partial_days=partial_days,
        missing_days=missing_days,
        gap_intervals=gaps,
        gap_intervals_truncated=truncated,
        pages_read=pages_read,
        retries=retries,
        identity_conflict_count=identity_conflict_count,
        error_category=error_category,
    )


def build_request_manifest(
    *,
    run_id: str,
    source: CollectionSource,
    records: Sequence[object],
    records_seen: int,
    records_mapped: int,
    records_skipped: int,
    duplicates_skipped: int = 0,
    source_capabilities: dict[str, bool] | None = None,
    metric_errors: dict[str, str] | None = None,
    requested_start: datetime | None = None,
    requested_end: datetime | None = None,
) -> CollectionManifest:
    """Build a privacy-safe manifest for request-level collection/imports."""
    timestamps = [
        value
        for record in records
        if (value := getattr(record, "timestamp", None)) is not None
    ]
    now = datetime.now(UTC)
    start = requested_start or (min(timestamps) if timestamps else now)
    end = requested_end or (max(timestamps) if timestamps else now)
    return CollectionManifest(
        run_id=run_id,
        source=source,
        generated_at=now,
        requested_start=start,
        requested_end=end,
        bucket_minutes=1,
        expected_buckets_per_deployment=0,
        source_capabilities=dict(source_capabilities or {}),
        metric_errors=dict(metric_errors or {}),
        records_seen=records_seen,
        records_mapped=records_mapped,
        records_skipped=records_skipped,
        duplicates_skipped=duplicates_skipped,
    )


__all__ = [
    "CollectionManifest",
    "DeploymentCollectionManifest",
    "GapInterval",
    "MetricCoverage",
    "build_deployment_manifest",
]
