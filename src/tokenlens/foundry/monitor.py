"""Azure Monitor metric collection for PTU analysis.

This module is explicit about three things:

1. **Network access.** Nothing here contacts Azure until a caller constructs a
   live client. Every function accepts an injected client, so tests and dry runs
   never reach the network.
2. **Capability.** Metric names *and dimensions* differ between Azure OpenAI,
   Claude, and partner deployments. Queries are built from the resource's own
   metric definitions (see :mod:`tokenlens.foundry.metrics_catalog`), so an
   incompatible filter is excluded before the request rather than swallowed as a
   generic missing metric.
3. **Identity.** ``QueryMetrics`` returns ``metadata_values`` as a dictionary in
   the shipping SDK and as a sequence in older builds. Both are normalized, and
   a missing dimension is resolved from the explicitly requested deployment and
   the deployment inventory before anything is ever labelled ``unknown``.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Iterable, Protocol, Sequence

from ..telemetry.schema import (
    BucketMetrics,
    MetricBucketRecord,
    ResourceScope,
    deterministic_event_id,
)
from .dimensions import extra_dimensions, normalize_dimensions
from .metrics_catalog import (
    AGGREGATIONS,
    CANONICAL_FIELDS,
    CAPABILITY_MATRIX,
    METRIC_MAP,
    MetricPlan,
    MetricSelection,
    select_metrics,
)

__all__ = [
    "AGGREGATIONS",
    "CAPABILITY_MATRIX",
    "METRIC_MAP",
    "AuthorizationError",
    "CollectionResult",
    "CollectionWindow",
    "CollectorError",
    "IdentityError",
    "MetricsClient",
    "ResourceClient",
    "collect_metrics",
    "dedupe",
    "normalize_dimensions",
    "reconcile_inventory",
    "resource_uri",
    "select_metrics",
    "supported_metrics",
]

RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4
BASE_BACKOFF_SECONDS = 0.5


class CollectorError(RuntimeError):
    """Raised when collection cannot proceed safely."""


class AuthorizationError(CollectorError):
    """Raised when the signed-in principal cannot read the resource's metrics."""


class IdentityError(CollectorError):
    """Raised when a metric response cannot be attributed to one deployment."""


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

    @property
    def expected_buckets(self) -> int:
        """Elapsed buckets in the selected window, independent of activity."""
        seconds = max(0.0, (self.end - self.start).total_seconds())
        return int(seconds // (self.granularity_minutes * 60))


@dataclass
class CollectionResult:
    records: list[MetricBucketRecord] = field(default_factory=list)
    missing_metrics: list[str] = field(default_factory=list)
    unsupported_metrics: list[str] = field(default_factory=list)
    requested_metrics: list[str] = field(default_factory=list)
    #: Candidate metrics excluded before any request, with the exact reason.
    excluded_metrics: dict[str, str] = field(default_factory=dict)
    #: Azure dimensions returned that TokenLens does not model, kept for local
    #: diagnostics only. Never written into a telemetry record.
    observed_dimensions: dict[str, str] = field(default_factory=dict)
    #: Model/version disagreements between sources. Surfaced, never silently
    #: overwritten.
    identity_conflicts: list[str] = field(default_factory=list)
    pages_read: int = 0
    retries: int = 0
    duplicate_buckets_skipped: int = 0
    coalesced_series: int = 0
    active_buckets: int = 0
    status_codes: dict[str, int] = field(default_factory=dict)
    outcome_coverage_complete: bool = False
    window_start: datetime | None = None
    window_end: datetime | None = None
    granularity_minutes: int = 5
    collected_at: datetime | None = None

    @property
    def outcome_coverage(self) -> str:
        """Three states, never two: a source that reported no status series is
        ``unavailable``, not ``partial``."""
        states = {record.outcome_coverage for record in self.records}
        if not states:
            return "unavailable"
        if states == {"complete"}:
            return "complete"
        if "complete" in states:
            return "partial"
        return "unavailable"

    @property
    def expected_buckets(self) -> int:
        if self.window_start is None or self.window_end is None:
            return 0
        seconds = max(0.0, (self.window_end - self.window_start).total_seconds())
        return int(seconds // (self.granularity_minutes * 60))

    def summary(self) -> dict[str, Any]:
        return {
            "buckets": len(self.records),
            "active_buckets": self.active_buckets,
            "expected_buckets": self.expected_buckets,
            "pages_read": self.pages_read,
            "retries": self.retries,
            "duplicate_buckets_skipped": self.duplicate_buckets_skipped,
            "coalesced_series": self.coalesced_series,
            "missing_metrics": list(self.missing_metrics),
            "unsupported_metrics": list(self.unsupported_metrics),
            "excluded_metrics": dict(self.excluded_metrics),
            "identity_conflicts": list(self.identity_conflicts),
            "status_codes": dict(self.status_codes),
            "outcome_coverage": self.outcome_coverage,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
        }


def resource_uri(subscription_id: str, resource_group: str, account: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account}"
    )


def supported_metrics(
    available: Iterable[Any],
    *,
    family: str = "azure_openai",
    deployment_filtered: bool = False,
) -> tuple[dict[str, str], list[str]]:
    """Resolve canonical fields to concrete metric names for this resource.

    Returns the resolved mapping and the canonical fields the resource cannot
    provide. Nothing is guessed: a field is only resolved when the resource's own
    metric definitions contain one of its documented candidates.
    """
    selection = select_metrics(available, family=family, deployment_filtered=deployment_filtered)
    return selection.resolved, list(selection.missing_fields)


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
    if isinstance(entry, Mapping):
        value = entry.get(key)
    else:
        value = getattr(entry, key, None)
    return float(value) if value is not None else None


def _entry_timestamp(entry: Any) -> datetime | None:
    value = (
        entry.get("timeStamp")
        if isinstance(entry, Mapping)
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
    """Yield ``(metric_name, normalized dimensions, entry)`` from a metrics response."""
    metrics = response.get("metrics") if isinstance(response, Mapping) else getattr(response, "metrics", [])
    for metric in metrics or []:
        name = metric.get("name") if isinstance(metric, Mapping) else getattr(metric, "name", "")
        if isinstance(name, Mapping):
            name = name.get("value", "")
        elif name is not None and not isinstance(name, str):
            name = getattr(name, "value", name)
        series = metric.get("timeseries") if isinstance(metric, Mapping) else getattr(metric, "timeseries", [])
        for item in series or []:
            if isinstance(item, Mapping):
                raw_dimensions = item.get("metadata_values")
                if raw_dimensions is None:
                    raw_dimensions = item.get("metadatavalues")
            else:
                raw_dimensions = getattr(item, "metadata_values", None)
                if raw_dimensions is None:
                    raw_dimensions = getattr(item, "metadatavalues", None)
            dimensions = normalize_dimensions(raw_dimensions)
            entries = item.get("data") if isinstance(item, Mapping) else getattr(item, "data", [])
            for entry in entries or []:
                yield str(name), dimensions, entry


def _as_int(value: float | None) -> int | None:
    return None if value is None else max(0, int(round(value)))


def _status_class(code: str) -> str:
    try:
        numeric = int(str(code).strip())
    except ValueError:
        return "other"
    if numeric == 429:
        return "rate_limited"
    if 200 <= numeric < 400:
        return "success"
    return "other"


class _BucketState:
    """One canonical bucket keyed by ``(timestamp, resolved deployment)``."""

    __slots__ = (
        "values",
        "provenance",
        "series_seen",
        "model_name",
        "model_version",
        "status_totals",
        "status_observed",
    )

    def __init__(self) -> None:
        self.values: dict[str, float] = {}
        self.provenance: dict[str, str] = {}
        self.series_seen: set[tuple[str, str]] = set()
        self.model_name: str | None = None
        self.model_version: str | None = None
        self.status_totals: dict[str, float] = {}
        self.status_observed = False


def _deployment_inventory(raw: Iterable[Mapping[str, Any]] | None) -> dict[str, dict[str, str]]:
    inventory: dict[str, dict[str, str]] = {}
    for item in raw or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        inventory[name.casefold()] = {
            "model": str(item.get("model") or "").strip(),
            "model_version": str(item.get("model_version") or "").strip(),
            "sku": str(item.get("sku") or "").strip(),
        }
    return inventory


def _series_signature(dimensions: Mapping[str, str]) -> str:
    return "|".join(f"{key}={dimensions[key]}" for key in sorted(dimensions))


def _resolve_deployment(dimensions: Mapping[str, str], requested: Sequence[str]) -> str:
    """Resolve the deployment before anything else is merged into a bucket."""
    explicit = dimensions.get("ModelDeploymentName")
    if explicit:
        return explicit
    if len(requested) == 1:
        return requested[0]
    if len(requested) > 1:
        raise IdentityError(
            "Azure Monitor returned a metric series without a ModelDeploymentName dimension while "
            f"{len(requested)} deployments were requested. Collect one deployment at a time so usage is "
            "never attributed to the wrong route."
        )
    raise IdentityError(
        "Azure Monitor returned a metric series without a ModelDeploymentName dimension for an "
        "account-wide collection. Pass --deployment to collect one explicitly named deployment."
    )


def _merge_identity(
    bucket: _BucketState,
    dimensions: Mapping[str, str],
    deployment: str,
    result: CollectionResult,
) -> None:
    """Join model identity while reporting, never hiding, a disagreement."""
    model = dimensions.get("ModelName")
    version = dimensions.get("ModelVersion")
    if model:
        if bucket.model_name and bucket.model_name != model:
            conflict = f"{deployment}: metric series report models {bucket.model_name} and {model}"
            if conflict not in result.identity_conflicts:
                result.identity_conflicts.append(conflict)
        else:
            bucket.model_name = model
    if version:
        if bucket.model_version and bucket.model_version != version:
            conflict = f"{deployment}: metric series report versions {bucket.model_version} and {version}"
            if conflict not in result.identity_conflicts:
                result.identity_conflicts.append(conflict)
        else:
            bucket.model_version = version


def collect_metrics(
    *,
    metrics_client: MetricsClient,
    subscription_id: str,
    resource_group: str,
    account: str,
    window: CollectionWindow,
    available_metrics: Iterable[Any],
    deployments: Sequence[str] | None = None,
    deployment_inventory: Iterable[Mapping[str, Any]] | None = None,
    family: str = "azure_openai",
    scope: ResourceScope | None = None,
    deployment_modes: dict[str, str] | None = None,
    default_deployment_mode: str = "unknown",
    workload_assignments: Mapping[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CollectionResult:
    """Collect aggregate buckets for one explicitly selected account.

    Only aggregate counters are requested. No prompt, response, header, user,
    or IP data is collected, and no resource identifier is written into the
    resulting telemetry records.

    ``available_metrics`` accepts metric names or full metric definitions. When
    definitions carry their dimensions, queries are built from them, so a metric
    that cannot be filtered by ``ModelDeploymentName`` is never sent.

    ``deployment_inventory`` is the output of
    ``AzureResourceClient.list_deployments``. It supplies exact model and version
    identity when a metric response omits those dimensions; a disagreement is
    reported in ``identity_conflicts`` rather than silently overwritten.

    ``default_deployment_mode`` applies to every discovered deployment that has
    no explicit entry in ``deployment_modes``. It stays ``unknown`` unless the
    caller supplies one: PTU sizing and pricing differ between Global and
    Regional deployments, so the mode is never inferred.

    ``workload_assignments`` maps a deployment name to a business workload and
    may contain *dedicated* deployments only. A shared deployment's aggregate
    cannot be allocated between workloads, so it is never tagged here.
    """
    result = CollectionResult(
        window_start=window.start,
        window_end=window.end,
        granularity_minutes=window.granularity_minutes,
        collected_at=now(),
    )
    selection: MetricSelection = select_metrics(available_metrics, family=family, deployment_filtered=True)
    if not selection.plans:
        raise CollectorError(
            "None of the documented token or request metrics can be queried for this resource with a "
            "deployment filter. Confirm the resource kind and its metric definitions before collecting."
        )
    result.missing_metrics = list(selection.missing_fields)
    result.excluded_metrics = dict(selection.excluded)
    result.unsupported_metrics = sorted(set(CANONICAL_FIELDS) - set(selection.plans))
    result.requested_metrics = sorted({plan.metric_name for plan in selection.plans.values()})

    uri = resource_uri(subscription_id, resource_group, account)
    requested = [str(name) for name in (deployments or [])]
    inventory = _deployment_inventory(deployment_inventory)
    deployment_filter = (
        " or ".join(f"ModelDeploymentName eq '{name}'" for name in requested)
        if requested
        else "ModelDeploymentName eq '*'"
    )

    # Metrics that expose ``StatusCode`` are requested with a status wildcard so
    # request outcomes are derived from the real distribution instead of being
    # inferred from a total.
    groups: dict[str, list[MetricPlan]] = {}
    for plan in selection.plans.values():
        query_filter = deployment_filter
        if plan.field == "requests" and plan.status_dimensioned:
            query_filter = f"{deployment_filter} and StatusCode eq '*'"
        groups.setdefault(query_filter, []).append(plan)

    buckets: dict[tuple[datetime, str], _BucketState] = {}
    status_provenance: str | None = None

    for query_filter, plans in groups.items():
        metric_names = sorted({plan.metric_name for plan in plans})
        by_metric = {plan.metric_name.casefold(): plan for plan in plans}
        aggregations = sorted({plan.aggregation for plan in plans})
        status_query = "StatusCode eq '*'" in query_filter

        def fetch(page_token: str | None, names=metric_names, aggs=aggregations, flt=query_filter) -> Any:
            kwargs: dict[str, Any] = {
                "timespan": (window.start, window.end),
                "granularity": timedelta(minutes=window.granularity_minutes),
                "aggregations": aggs,
                "filter": flt,
            }
            if page_token:
                kwargs["page_token"] = page_token
            return metrics_client.query_resource(uri, names, **kwargs)

        page_token: str | None = None
        while True:
            response = _call_with_retries(lambda token=page_token: fetch(token), result, sleep=sleep)
            result.pages_read += 1
            for metric_name, dimensions, entry in _iter_series(response):
                plan = by_metric.get(metric_name.casefold())
                if plan is None:
                    continue
                stamp = _entry_timestamp(entry)
                if stamp is None:
                    continue
                value = _metric_value(entry, plan.aggregation)
                if value is None:
                    # A null point means Azure Monitor reported no data for that
                    # bucket. It is skipped, never recorded as a zero.
                    continue
                deployment = _resolve_deployment(dimensions, requested)
                key = (stamp, deployment)
                bucket = buckets.get(key)
                if bucket is None:
                    bucket = buckets[key] = _BucketState()
                result.observed_dimensions.update(extra_dimensions(dimensions))
                _merge_identity(bucket, dimensions, deployment, result)

                status = dimensions.get("StatusCode")
                if status_query and plan.field == "requests" and status is not None:
                    bucket.status_totals[status] = bucket.status_totals.get(status, 0.0) + value
                    bucket.status_observed = True
                    bucket.provenance["requests"] = plan.metric_name
                    status_provenance = plan.metric_name
                    continue

                signature = (plan.field, _series_signature(dimensions))
                if signature in bucket.series_seen:
                    result.duplicate_buckets_skipped += 1
                    continue
                bucket.series_seen.add(signature)
                if plan.field in bucket.values:
                    if plan.aggregation == "Average":
                        # Averages of different populations cannot be summed and
                        # must not be silently blended.
                        result.duplicate_buckets_skipped += 1
                        continue
                    # Two series of the same counter for one interval (for
                    # example cached tokens split by context length) are summed,
                    # not dropped, and the coalescing is reported.
                    bucket.values[plan.field] += value
                    result.coalesced_series += 1
                else:
                    bucket.values[plan.field] = value
                bucket.provenance[plan.field] = plan.metric_name
            page_token = (
                response.get("nextPageToken")
                if isinstance(response, Mapping)
                else getattr(response, "next_page_token", None)
            )
            if not page_token:
                break

    result.outcome_coverage_complete = bool(buckets) and all(state.status_observed for state in buckets.values())

    for (stamp, deployment), state in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1])):
        values = dict(state.values)
        status_counts: dict[str, int] = {}
        if state.status_observed:
            for code, amount in state.status_totals.items():
                status_counts[str(code)] = int(round(amount))
                result.status_codes[str(code)] = result.status_codes.get(str(code), 0) + int(round(amount))
            values["requests"] = float(sum(state.status_totals.values()))
            values["successful_requests"] = float(
                sum(amount for code, amount in state.status_totals.items() if _status_class(code) == "success")
            )
            values["throttled_requests"] = float(
                sum(amount for code, amount in state.status_totals.items() if _status_class(code) == "rate_limited")
            )
            values["failed_requests"] = float(
                sum(amount for code, amount in state.status_totals.items() if _status_class(code) == "other")
            )
            for derived in ("successful_requests", "throttled_requests", "failed_requests"):
                state.provenance[derived] = status_provenance or state.provenance.get("requests", "")

        entry_identity = inventory.get(deployment.casefold(), {})
        inventory_model = entry_identity.get("model") or ""
        inventory_version = entry_identity.get("model_version") or ""
        if state.model_name and inventory_model and state.model_name != inventory_model:
            conflict = (
                f"{deployment}: inventory model {inventory_model} does not match metric model {state.model_name}"
            )
            if conflict not in result.identity_conflicts:
                result.identity_conflicts.append(conflict)
        if state.model_version and inventory_version and state.model_version != inventory_version:
            conflict = (
                f"{deployment}: inventory version {inventory_version} does not match metric version "
                f"{state.model_version}"
            )
            if conflict not in result.identity_conflicts:
                result.identity_conflicts.append(conflict)
        model_name = state.model_name or inventory_model or "unknown"
        model_version = state.model_version or inventory_version or None

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
        bucket_missing = sorted(
            set(selection.missing_fields) | {name for name in selection.plans if name not in values}
        )
        if metrics.input_tokens or metrics.output_tokens or metrics.requests:
            result.active_buckets += 1
        assigned_workload = (workload_assignments or {}).get(deployment)
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
                workload=assigned_workload,
                workload_source="deployment_mapping" if assigned_workload else None,
                allocation_confidence="exact_dedicated_deployment" if assigned_workload else None,
                missing_metrics=bucket_missing,
                metric_provenance=dict(state.provenance),
                status_codes=status_counts or None,
                outcome_coverage="complete" if state.status_observed else "unavailable",
                collected_at=result.collected_at,
                window_start=window.start,
                window_end=window.end,
                expected_buckets=window.expected_buckets or None,
            )
        )
    return result


def reconcile_inventory(
    records: Sequence[MetricBucketRecord],
    deployment_inventory: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Report model/version disagreements between collected metrics and the inventory."""
    inventory = _deployment_inventory(deployment_inventory)
    conflicts: list[str] = []
    for record in records:
        entry = inventory.get(record.deployment_name.casefold())
        if not entry:
            continue
        if entry.get("model") and record.model_name not in {"unknown", entry["model"]}:
            message = (
                f"{record.deployment_name}: inventory model {entry['model']} does not match metric model "
                f"{record.model_name}"
            )
            if message not in conflicts:
                conflicts.append(message)
        if entry.get("model_version") and record.model_version and record.model_version != entry["model_version"]:
            message = (
                f"{record.deployment_name}: inventory version {entry['model_version']} does not match metric "
                f"version {record.model_version}"
            )
            if message not in conflicts:
                conflicts.append(message)
    return conflicts


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
