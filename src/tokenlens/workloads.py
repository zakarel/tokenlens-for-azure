"""Workload identity and workload economics.

Two levels are modelled and never used interchangeably:

* **Workload economics** — cost grouped by an application, agent, API, or
  business process label. Outcomes are optional.
* **Task economics** — cost per attempted, closed, solved, or correctly solved
  business task. Explicit task identity and outcomes are required, and that
  engine lives in :mod:`tokenlens.economics`.

Workload identity is never inferred from prompt content, model names, token
shape, user identity, resource names, or deployment-name heuristics. It comes
only from an explicit request tag, an OpenTelemetry attribute, an explicit
deployment-to-workload mapping, or an explicit import mapping.

Every resolved deployment always owns one *technical* workload so the Workloads
experience is populated even when the user skips business configuration. A
technical workload states "all traffic and cost for this deployment are visible
as one technical workload" — never "this deployment serves one business
workload".
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import date, datetime
from typing import Callable, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .models import TraceRecord
from .pricing import PricingResolution

__all__ = [
    "Allocation",
    "AllocationConfidence",
    "ConfigurationStatus",
    "UNASSIGNED_WORKLOAD_ID",
    "WorkloadCostRollup",
    "WorkloadIdentity",
    "WorkloadMapping",
    "WorkloadMappingError",
    "WorkloadPortfolio",
    "WorkloadScope",
    "WorkloadSource",
    "WorkloadTaskMetrics",
    "WorkloadType",
    "build_portfolio",
    "canonical_workload_id",
    "default_technical_workload",
    "default_workload_id",
    "internal_workload_key",
    "merge_identities",
    "record_workload",
    "safe_account_scope",
    "task_metrics_by_workload",
    "validate_mappings",
]

WorkloadScope = Literal["technical", "business"]
WorkloadType = Literal[
    "ai_deployment",
    "application",
    "agent",
    "business_process",
    "api",
    "batch_job",
    "other",
    "unclassified",
]
WorkloadSource = Literal[
    "system_default",
    "request_tag",
    "otel_attribute",
    "deployment_mapping",
    "import_mapping",
]
ConfigurationStatus = Literal["needs_configuration", "configured", "partially_configured"]
Allocation = Literal["deployment_total", "dedicated", "request_attributed", "unassigned"]
AllocationConfidence = Literal[
    "exact_request_tag",
    "exact_dedicated_deployment",
    "unallocated",
]
Criticality = Literal["unknown", "low", "medium", "high", "mission_critical"]

#: Reserved for traffic that cannot be attributed to a *business* workload.
#: A resolved deployment-level technical workload is never called ``Unassigned``
#: — technical rows always carry their exact deployment name.
UNASSIGNED_WORKLOAD_ID = "unassigned"

#: Runtime allow lists for provenance read from untrusted record metadata.
WORKLOAD_SOURCES = frozenset(
    {"system_default", "request_tag", "otel_attribute", "deployment_mapping", "import_mapping"}
)
ALLOCATION_CONFIDENCES = frozenset(
    {"exact_request_tag", "exact_dedicated_deployment", "unallocated"}
)

_SAFE = re.compile(r"[^a-z0-9]+")


def canonical_workload_id(value: str) -> str:
    """Return a stable, machine-safe identifier fragment.

    The result is derived only from the supplied label, so repeated runs over
    the same deployment inventory produce the same identifier. No random UUID is
    generated and no subscription or tenant value is ever folded in.
    """
    slug = _SAFE.sub("-", str(value).strip().casefold()).strip("-")
    return slug or "unknown"


def default_workload_id(deployment_name: str) -> str:
    """Report-facing identifier for a deployment-backed technical workload."""
    return f"deployment:{canonical_workload_id(deployment_name)}"


def safe_account_scope(account: str | None, resource_group: str | None = None) -> str:
    """Return a display-safe account scope.

    Subscription and tenant identifiers are never included: duplicate
    deployment names across accounts are disambiguated with the account label
    the user already sees in the wizard.
    """
    parts = [canonical_workload_id(part) for part in (resource_group, account) if part]
    return "/".join(parts) or "account"


def internal_workload_key(account_scope: str, deployment_name: str) -> str:
    """Internal reconciliation key; never rendered into HTML."""
    return f"{account_scope}/{canonical_workload_id(deployment_name)}"


class WorkloadIdentity(BaseModel):
    """One workload, technical or business."""

    model_config = ConfigDict(extra="forbid")

    workload_id: str
    workload_name: str
    workload_scope: WorkloadScope
    workload_type: WorkloadType
    source: WorkloadSource
    configuration_status: ConfigurationStatus
    allocation: Allocation
    deployment_names: list[str] = Field(default_factory=list)
    environment: str | None = None
    business_purpose: str | None = None
    owner_label: str | None = None
    cost_center: str | None = None
    criticality: Criticality = "unknown"
    #: Internal reconciliation key. Excluded from report rendering.
    internal_key: str | None = None
    #: ``True`` when the deployment behind a technical workload is no longer in
    #: the discovered inventory. Historical data is retained, never deleted.
    stale: bool = False


class WorkloadMapping(BaseModel):
    """User-configured business workload mapped to one or more deployments."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: WorkloadType = "application"
    environment: str | None = None
    deployments: list[str] = Field(default_factory=list)
    allocation: Literal["dedicated", "shared"] = "dedicated"
    business_purpose: str | None = None
    owner_label: str | None = None
    cost_center: str | None = None
    criticality: Criticality = "unknown"

    @property
    def configuration_status(self) -> ConfigurationStatus:
        if self.environment and self.business_purpose:
            return "configured"
        if self.name:
            return "partially_configured" if not self.environment else "configured"
        return "needs_configuration"


class WorkloadMappingError(ValueError):
    """Raised when workload mappings cannot be applied safely."""


def validate_mappings(
    mappings: Sequence[WorkloadMapping],
    *,
    known_deployments: Sequence[str] | None = None,
) -> list[str]:
    """Return human-readable problems; an empty list means the mapping is safe.

    A deployment may be *dedicated* to one workload only. Overlapping dedicated
    mappings are rejected before any calculation so cost is never divided
    arbitrarily between workloads.
    """
    problems: list[str] = []
    seen_ids: set[str] = set()
    dedicated: dict[str, str] = {}
    shared: dict[str, str] = {}
    known = {canonical_workload_id(name) for name in (known_deployments or [])}
    for mapping in mappings:
        identifier = canonical_workload_id(mapping.id)
        if identifier in seen_ids:
            problems.append(f"duplicate workload id: {mapping.id}")
        seen_ids.add(identifier)
        if not mapping.deployments:
            problems.append(f"{mapping.id}: no deployment is mapped")
        for deployment in mapping.deployments:
            key = canonical_workload_id(deployment)
            if known and key not in known:
                problems.append(
                    f"{mapping.id}: deployment {deployment} is not in the discovered inventory"
                )
            if mapping.allocation != "dedicated":
                owner = shared.get(key)
                if owner is None:
                    shared[key] = mapping.id
                elif owner != mapping.id:
                    pass
                if key in dedicated and dedicated[key] != mapping.id:
                    problems.append(
                        f"{deployment} is mapped as both dedicated and shared "
                        f"({dedicated[key]} and {mapping.id})"
                    )
                continue
            owner = dedicated.get(key)
            if owner is not None and owner != mapping.id:
                problems.append(
                    f"{deployment} is marked dedicated to both {owner} and {mapping.id}"
                )
            dedicated[key] = mapping.id
            if key in shared and shared[key] != mapping.id:
                problems.append(
                    f"{deployment} is mapped as both dedicated and shared "
                    f"({mapping.id} and {shared[key]})"
                )
    return problems


def default_technical_workload(
    deployment_name: str,
    *,
    account_scope: str = "account",
    environment: str | None = None,
) -> WorkloadIdentity:
    """Create the deterministic deployment-backed technical workload."""
    return WorkloadIdentity(
        workload_id=default_workload_id(deployment_name),
        workload_name=deployment_name,
        workload_scope="technical",
        workload_type="ai_deployment",
        source="system_default",
        configuration_status="needs_configuration",
        allocation="deployment_total",
        deployment_names=[deployment_name],
        environment=environment,
        internal_key=internal_workload_key(account_scope, deployment_name),
    )


def merge_identities(
    deployments: Sequence[str],
    *,
    mappings: Sequence[WorkloadMapping] = (),
    existing: Sequence[WorkloadIdentity] = (),
    account_scope: str = "account",
) -> list[WorkloadIdentity]:
    """Regenerate technical workloads and apply configured business workloads.

    The operation is idempotent: repeated refreshes over the same inventory
    produce identical identities, user enrichment on a technical workload is
    preserved, new deployments arrive as ``needs_configuration``, and removed
    deployments are marked stale rather than deleted.
    """
    previous = {item.workload_id: item for item in existing}
    mapped: dict[str, str] = {}
    for mapping in mappings:
        for deployment in mapping.deployments:
            mapped[canonical_workload_id(deployment)] = mapping.allocation
    identities: list[WorkloadIdentity] = []
    seen: set[str] = set()
    for deployment in deployments:
        workload_id = default_workload_id(deployment)
        seen.add(workload_id)
        prior = previous.get(workload_id)
        technical = default_technical_workload(deployment, account_scope=account_scope)
        allocation = mapped.get(canonical_workload_id(deployment))
        if allocation is not None:
            # A business mapping covers this deployment, so the technical
            # workload is configured. It remains the reconciliation layer and is
            # never renamed into the business workload.
            technical = technical.model_copy(
                update={
                    "configuration_status": "configured"
                    if allocation == "dedicated"
                    else "partially_configured"
                }
            )
        if prior is not None and prior.workload_scope == "technical":
            # Preserve enrichment the user supplied earlier; never overwrite it
            # during refresh.
            technical = technical.model_copy(
                update={
                    "environment": prior.environment or technical.environment,
                    "business_purpose": prior.business_purpose,
                    "owner_label": prior.owner_label,
                    "cost_center": prior.cost_center,
                    "criticality": prior.criticality,
                    "configuration_status": (
                        technical.configuration_status
                        if allocation is not None
                        else prior.configuration_status
                    ),
                }
            )
        identities.append(technical)
    for prior in existing:
        if prior.workload_scope != "technical" or prior.workload_id in seen:
            continue
        # A deployment disappeared from discovery. Historical workload data is
        # preserved and the row is marked stale.
        identities.append(prior.model_copy(update={"stale": True}))
    for mapping in mappings:
        identities.append(
            WorkloadIdentity(
                workload_id=canonical_workload_id(mapping.id),
                workload_name=mapping.name,
                workload_scope="business",
                workload_type=mapping.type,
                source="deployment_mapping",
                configuration_status=mapping.configuration_status,
                allocation="dedicated" if mapping.allocation == "dedicated" else "request_attributed",
                deployment_names=list(mapping.deployments),
                environment=mapping.environment,
                business_purpose=mapping.business_purpose,
                owner_label=mapping.owner_label,
                cost_center=mapping.cost_center,
                criticality=mapping.criticality,
            )
        )
    return identities


class WorkloadTaskMetrics(BaseModel):
    """Task economics for one workload. Absent evidence stays ``None``."""

    model_config = ConfigDict(extra="forbid")

    attempted_tasks: int | None = None
    closed_tasks: int | None = None
    solved_tasks: int | None = None
    cost_per_attempted_task: float | None = None
    cost_per_closed_task: float | None = None
    cost_per_solved_task: float | None = None
    p50_task_cost: float | None = None
    p90_task_cost: float | None = None
    failed_trajectory_cost: float | None = None
    retries_per_task: float | None = None
    model_calls_per_task: float | None = None
    cost_per_correct_task: float | None = None
    observed_cleanup_cost: float | None = None

    @property
    def measured(self) -> bool:
        return bool(self.attempted_tasks)


class WorkloadCostRollup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workload_id: str
    workload_name: str
    workload_type: str
    workload_scope: WorkloadScope = "technical"
    configuration_status: str = "needs_configuration"
    environment: str | None = None
    identity_source: str
    allocation_confidence: AllocationConfidence
    deployments: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    requests: int | None = None
    input_tokens: int | None = None
    cached_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: float | None = None
    pricing_currency: str = "USD"
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    pricing_coverage_percent: float = 0.0
    cost_per_request: float | None = None
    active_days: int = 0
    average_daily_cost: float | None = None
    p50_daily_cost: float | None = None
    p90_daily_cost: float | None = None
    business_identity_coverage_percent: float = 0.0
    identity_unresolved_tokens: int = 0
    daily_costs: list[tuple[str, float]] = Field(default_factory=list)
    partial_days: list[str] = Field(default_factory=list)
    tasks: WorkloadTaskMetrics | None = None
    stale: bool = False

    @property
    def priced(self) -> bool:
        return self.estimated_cost is not None


class WorkloadPortfolio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workloads: list[WorkloadCostRollup] = Field(default_factory=list)
    business_workloads: list[WorkloadCostRollup] = Field(default_factory=list)
    total_estimated_cost: float | None = None
    priced_cost: float | None = None
    unallocated_cost: float | None = None
    pricing_coverage_percent: float = 0.0
    technical_workload_coverage_percent: float = 0.0
    workload_identity_coverage_percent: float = 0.0
    reporting_currency: str = "USD"
    total_tokens: int = 0
    unpriced_tokens: int = 0
    identity_unresolved_tokens: int = 0
    shared_deployments_without_tags: list[str] = Field(default_factory=list)
    stale_deployments: list[str] = Field(default_factory=list)
    mapping_problems: list[str] = Field(default_factory=list)

    @property
    def has_business_identity(self) -> bool:
        return any(item.workload_scope == "business" for item in self.business_workloads)

    @property
    def default_scope(self) -> WorkloadScope:
        return "business" if self.workload_identity_coverage_percent > 0 else "technical"


def record_workload(record: TraceRecord) -> tuple[str | None, str | None, str | None]:
    """Return ``(workload, source, allocation_confidence)`` for one record.

    Only explicit provenance is honoured. Nothing is derived from the model
    name, the deployment name, prompt content, or resource labels.
    """
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    workload = metadata.get("workload")
    if not isinstance(workload, str) or not workload.strip():
        return None, None, None
    source = metadata.get("workload_source")
    confidence = metadata.get("allocation_confidence")
    telemetry_source = metadata.get("telemetry_source")
    # Caller-supplied metadata is arbitrary, so provenance is accepted only when
    # it is one of the modelled values. Anything else falls back to the derived
    # default rather than reaching a typed rollup and failing the whole run.
    if source not in WORKLOAD_SOURCES:
        source = "otel_attribute" if telemetry_source == "otel" else "request_tag"
    if confidence not in ALLOCATION_CONFIDENCES:
        confidence = (
            "exact_dedicated_deployment"
            if source == "deployment_mapping"
            else "exact_request_tag"
        )
    return workload.strip(), source, confidence


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _record_requests(record: TraceRecord) -> int | None:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    if metadata.get("record_type") == "foundry_metric_bucket":
        metrics = metadata.get("metrics")
        if isinstance(metrics, Mapping):
            value = metrics.get("requests")
            return int(value) if value is not None else None
        return None
    return 1


def _record_date(record: TraceRecord) -> date | None:
    stamp = record.timestamp
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _identity_unresolved(record: TraceRecord) -> bool:
    return (record.model_name or "unknown").strip().casefold() in {"", "unknown", "none"}


class _Bucket:
    """Mutable accumulator for one workload rollup."""

    def __init__(self) -> None:
        self.deployments: OrderedDict[str, None] = OrderedDict()
        self.models: OrderedDict[str, None] = OrderedDict()
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0
        self.priced_tokens = 0
        self.unpriced_tokens = 0
        self.identity_unresolved_tokens = 0
        self.cost = 0.0
        self.priced_records = 0
        self.total_records = 0
        self.requests = 0
        self.requests_available = False
        self.request_metric_missing = False
        self.daily: dict[date, float] = {}
        self.business_tokens = 0
        self.sources: OrderedDict[str, None] = OrderedDict()
        self.confidences: OrderedDict[str, None] = OrderedDict()

    def add(
        self,
        record: TraceRecord,
        resolution: PricingResolution,
        *,
        business_tagged: bool = False,
    ) -> None:
        usage = record.usage
        tokens = usage.input_tokens + usage.output_tokens
        self.deployments.setdefault(record.deployment_name or "unknown", None)
        self.models.setdefault(record.model_name or "unknown", None)
        self.input_tokens += usage.input_tokens
        self.cached_tokens += usage.cached_tokens
        self.output_tokens += usage.output_tokens
        self.total_records += 1
        if business_tagged:
            self.business_tokens += tokens
        if _identity_unresolved(record):
            self.identity_unresolved_tokens += tokens
        requests = _record_requests(record)
        if requests is None:
            self.request_metric_missing = True
        else:
            self.requests += requests
            self.requests_available = True
        if resolution.resolved and resolution.cost_usd is not None:
            self.cost += resolution.cost_usd
            self.priced_tokens += tokens
            self.priced_records += 1
            day = _record_date(record)
            if day is not None:
                self.daily[day] = self.daily.get(day, 0.0) + resolution.cost_usd
        else:
            self.unpriced_tokens += tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _rollup(
    identity: WorkloadIdentity | None,
    bucket: _Bucket,
    *,
    workload_id: str,
    workload_name: str,
    scope: WorkloadScope,
    identity_source: str,
    allocation_confidence: AllocationConfidence,
    currency: str,
    tasks: WorkloadTaskMetrics | None,
    window_days: Sequence[date] = (),
) -> WorkloadCostRollup:
    total_tokens = bucket.total_tokens
    coverage = round(bucket.priced_tokens / total_tokens * 100, 1) if total_tokens else 0.0
    priced_complete = bucket.unpriced_tokens == 0 and bucket.priced_tokens > 0
    requests = bucket.requests if bucket.requests_available and not bucket.request_metric_missing else None
    estimated_cost = round(bucket.cost, 6) if bucket.priced_records else None
    # A cost per request is only meaningful when both the denominator and the
    # price coverage are complete. Anything else is withheld.
    cost_per_request = (
        round(bucket.cost / requests, 6)
        if estimated_cost is not None and priced_complete and requests
        else None
    )
    daily_values = [round(value, 6) for _, value in sorted(bucket.daily.items())]
    partial = [day.isoformat() for day in window_days if day not in bucket.daily]
    return WorkloadCostRollup(
        workload_id=workload_id,
        workload_name=workload_name,
        workload_type=identity.workload_type if identity else "unclassified",
        workload_scope=scope,
        configuration_status=identity.configuration_status if identity else "needs_configuration",
        environment=identity.environment if identity else None,
        identity_source=identity_source,
        allocation_confidence=allocation_confidence,
        deployments=list(bucket.deployments),
        models=list(bucket.models),
        requests=requests,
        input_tokens=bucket.input_tokens,
        cached_tokens=bucket.cached_tokens,
        output_tokens=bucket.output_tokens,
        total_tokens=total_tokens,
        estimated_cost=estimated_cost,
        pricing_currency=currency,
        priced_tokens=bucket.priced_tokens,
        unpriced_tokens=bucket.unpriced_tokens,
        pricing_coverage_percent=coverage,
        cost_per_request=cost_per_request,
        active_days=len(bucket.daily),
        average_daily_cost=round(sum(daily_values) / len(daily_values), 6) if daily_values else None,
        p50_daily_cost=(
            round(value, 6) if (value := _percentile(daily_values, 0.5)) is not None else None
        ),
        p90_daily_cost=(
            round(value, 6) if (value := _percentile(daily_values, 0.9)) is not None else None
        ),
        business_identity_coverage_percent=(
            round(bucket.business_tokens / total_tokens * 100, 1) if total_tokens else 0.0
        ),
        identity_unresolved_tokens=bucket.identity_unresolved_tokens,
        daily_costs=[(day.isoformat(), round(value, 6)) for day, value in sorted(bucket.daily.items())],
        partial_days=partial,
        tasks=tasks,
        stale=bool(identity.stale) if identity else False,
    )


def build_portfolio(
    records: Sequence[TraceRecord],
    *,
    cost_resolver: Callable[[TraceRecord], PricingResolution],
    identities: Sequence[WorkloadIdentity] = (),
    mappings: Sequence[WorkloadMapping] = (),
    reporting_currency: str = "USD",
    task_metrics: Mapping[str, WorkloadTaskMetrics] | None = None,
) -> WorkloadPortfolio:
    """Build technical and business workload rollups from analyzed records.

    Rules enforced here:

    * only exact pricing resolutions are summed; unresolved cost is never zero;
    * one reporting currency per rollup and no implicit conversion;
    * workload rollups reconcile exactly with deployment totals;
    * shared deployment traffic without a request tag stays ``Unassigned``;
    * ``cost_per_request`` is withheld unless its denominator and price
      coverage are complete.
    """
    problems = list(validate_mappings(mappings)) if mappings else []
    if problems:
        raise WorkloadMappingError("; ".join(problems))

    dedicated_by_deployment: dict[str, WorkloadMapping] = {}
    shared_by_deployment: dict[str, list[WorkloadMapping]] = {}
    for mapping in mappings:
        for deployment in mapping.deployments:
            key = canonical_workload_id(deployment)
            if mapping.allocation == "dedicated":
                dedicated_by_deployment[key] = mapping
            else:
                shared_by_deployment.setdefault(key, []).append(mapping)

    identity_by_id = {item.workload_id: item for item in identities}
    # A request tag that names a configured workload resolves to that workload's
    # stable id rather than creating a second row for the same business identity.
    mapping_by_label: dict[str, WorkloadMapping] = {}
    for mapping in mappings:
        mapping_by_label.setdefault(canonical_workload_id(mapping.id), mapping)
        mapping_by_label.setdefault(canonical_workload_id(mapping.name), mapping)
    technical: OrderedDict[str, _Bucket] = OrderedDict()
    technical_names: dict[str, str] = {}
    business: OrderedDict[str, _Bucket] = OrderedDict()
    business_names: dict[str, str] = {}
    unassigned = _Bucket()
    unassigned_used = False
    observed_days: set[date] = set()
    resolutions: list[tuple[TraceRecord, PricingResolution]] = []
    for record in records:
        resolutions.append((record, cost_resolver(record)))
        day = _record_date(record)
        if day is not None:
            observed_days.add(day)
    window_days = sorted(observed_days)

    # Business allocation only exists at all when something explicitly declares
    # it. When nothing does, the report stays a technical-workload view and no
    # Unassigned row is invented.
    business_scope_exists = bool(dedicated_by_deployment) or bool(shared_by_deployment) or any(
        record_workload(record)[0] for record, _ in resolutions
    )

    for record, resolution in resolutions:
        deployment = record.deployment_name or "unknown"
        deployment_key = canonical_workload_id(deployment)
        workload, source, confidence = record_workload(record)
        mapping = dedicated_by_deployment.get(deployment_key)
        if workload is None and mapping is not None:
            workload = mapping.name
            source = "deployment_mapping"
            confidence = "exact_dedicated_deployment"
        business_tagged = workload is not None
        technical_id = default_workload_id(deployment)
        bucket = technical.setdefault(technical_id, _Bucket())
        technical_names[technical_id] = deployment
        bucket.add(record, resolution, business_tagged=business_tagged)
        if workload is not None:
            configured = mapping_by_label.get(canonical_workload_id(workload))
            if mapping is not None and source == "deployment_mapping":
                configured = mapping
            workload_id = canonical_workload_id(configured.id if configured else workload)
            target = business.setdefault(workload_id, _Bucket())
            business_names[workload_id] = configured.name if configured else workload
            if source:
                target.sources.setdefault(source, None)
            if confidence:
                target.confidences.setdefault(confidence, None)
            target.add(record, resolution, business_tagged=True)
        elif business_scope_exists:
            # Never hide unattributed traffic: business rollups plus Unassigned
            # always reconcile exactly with the technical workload total.
            unassigned_used = True
            unassigned.add(record, resolution, business_tagged=False)

    tasks_by_label = {
        canonical_workload_id(label): metrics for label, metrics in (task_metrics or {}).items()
    }
    matched_task_labels: set[str] = set()

    def _tasks_for(name: str) -> WorkloadTaskMetrics | None:
        key = canonical_workload_id(name)
        metrics = tasks_by_label.get(key)
        if metrics is not None:
            matched_task_labels.add(key)
        return metrics

    technical_rollups = [
        _rollup(
            identity_by_id.get(workload_id),
            bucket,
            workload_id=workload_id,
            workload_name=technical_names[workload_id],
            scope="technical",
            identity_source="system_default",
            allocation_confidence="exact_dedicated_deployment",
            currency=reporting_currency,
            tasks=None,
            window_days=window_days,
        )
        for workload_id, bucket in technical.items()
    ]
    for identity in identities:
        if identity.workload_scope != "technical" or identity.workload_id in technical:
            continue
        # A configured deployment with no observed traffic still appears so the
        # Workloads tab is never silently incomplete.
        technical_rollups.append(
            _rollup(
                identity,
                _Bucket(),
                workload_id=identity.workload_id,
                workload_name=identity.workload_name,
                scope="technical",
                identity_source="system_default",
                allocation_confidence="exact_dedicated_deployment",
                currency=reporting_currency,
                tasks=None,
                window_days=window_days,
            )
        )
    business_rollups = [
        _rollup(
            identity_by_id.get(workload_id),
            bucket,
            workload_id=workload_id,
            workload_name=business_names[workload_id],
            scope="business",
            identity_source=next(iter(bucket.sources), "request_tag"),
            allocation_confidence=next(iter(bucket.confidences), "exact_request_tag"),  # type: ignore[arg-type]
            currency=reporting_currency,
            tasks=_tasks_for(business_names[workload_id]) or _tasks_for(workload_id),
            window_days=window_days,
        )
        for workload_id, bucket in business.items()
    ]
    if unassigned_used and unassigned.total_records:
        business_rollups.append(
            _rollup(
                None,
                unassigned,
                workload_id=UNASSIGNED_WORKLOAD_ID,
                workload_name="Unassigned",
                scope="business",
                identity_source="unassigned",
                allocation_confidence="unallocated",
                currency=reporting_currency,
                tasks=None,
                window_days=window_days,
            )
        )

    # Task evidence can name a workload whose deployment traffic is not yet
    # attributed. The tasks stay visible; the missing attribution is explicit.
    for label, metrics in (task_metrics or {}).items():
        key = canonical_workload_id(label)
        if key in matched_task_labels:
            continue
        technical_match = next(
            (item for item in technical_rollups if canonical_workload_id(item.workload_name) == key),
            None,
        )
        if technical_match is not None:
            matched_task_labels.add(key)
            technical_rollups[technical_rollups.index(technical_match)] = technical_match.model_copy(
                update={"tasks": metrics}
            )
            continue
        business_rollups.append(
            _rollup(
                identity_by_id.get(key),
                _Bucket(),
                workload_id=key,
                workload_name=label,
                scope="business",
                identity_source="task_event",
                allocation_confidence="exact_request_tag",
                currency=reporting_currency,
                tasks=metrics,
                window_days=window_days,
            )
        )

    technical_rollups.sort(key=lambda item: (-(item.total_tokens or 0), item.workload_name.casefold()))
    business_rollups.sort(
        key=lambda item: (
            item.workload_id == UNASSIGNED_WORKLOAD_ID,
            -(item.total_tokens or 0),
            item.workload_name.casefold(),
        )
    )

    total_tokens = sum(item.total_tokens or 0 for item in technical_rollups)
    priced_tokens = sum(item.priced_tokens for item in technical_rollups)
    unpriced_tokens = sum(item.unpriced_tokens for item in technical_rollups)
    identity_unresolved = sum(item.identity_unresolved_tokens for item in technical_rollups)
    priced_cost = sum(item.estimated_cost or 0.0 for item in technical_rollups)
    any_priced = any(item.estimated_cost is not None for item in technical_rollups)
    business_tokens = sum(
        item.total_tokens or 0
        for item in business_rollups
        if item.workload_id != UNASSIGNED_WORKLOAD_ID
    )
    unallocated_cost = next(
        (
            item.estimated_cost
            for item in business_rollups
            if item.workload_id == UNASSIGNED_WORKLOAD_ID
        ),
        None,
    )
    tagged_deployments = {
        canonical_workload_id(name)
        for rollup in business_rollups
        if rollup.workload_id != UNASSIGNED_WORKLOAD_ID
        for name in rollup.deployments
    }
    shared_without_tags = sorted(
        {
            deployment
            for mapping in mappings
            if mapping.allocation == "shared"
            for deployment in mapping.deployments
            if canonical_workload_id(deployment) not in tagged_deployments
        }
    )
    return WorkloadPortfolio(
        workloads=technical_rollups,
        business_workloads=business_rollups,
        total_estimated_cost=round(priced_cost, 6) if any_priced else None,
        priced_cost=round(priced_cost, 6) if any_priced else None,
        unallocated_cost=unallocated_cost,
        pricing_coverage_percent=round(priced_tokens / total_tokens * 100, 1) if total_tokens else 0.0,
        technical_workload_coverage_percent=(
            round((total_tokens - identity_unresolved) / total_tokens * 100, 1) if total_tokens else 0.0
        ),
        workload_identity_coverage_percent=(
            round(business_tokens / total_tokens * 100, 1) if total_tokens else 0.0
        ),
        reporting_currency=reporting_currency,
        total_tokens=total_tokens,
        unpriced_tokens=unpriced_tokens,
        identity_unresolved_tokens=identity_unresolved,
        shared_deployments_without_tags=shared_without_tags,
        stale_deployments=[item.workload_name for item in technical_rollups if item.stale],
        mapping_problems=problems,
    )


def task_metrics_by_workload(tasks: Iterable[object]) -> dict[str, WorkloadTaskMetrics]:
    """Summarize reconstructed task trajectories per explicit workload label.

    Tasks without an explicit ``workload`` are excluded: task identity is never
    inferred from a model, deployment, or prompt.
    """
    grouped: dict[str, list[object]] = {}
    for task in tasks:
        label = getattr(task, "workload", None)
        if not isinstance(label, str) or not label.strip():
            continue
        grouped.setdefault(label.strip(), []).append(task)
    metrics: dict[str, WorkloadTaskMetrics] = {}
    for label, cohort in grouped.items():
        closed = [task for task in cohort if getattr(task, "closed", False)]
        resolved = [task for task in cohort if getattr(task, "resolved", False)]
        resolved_costs = [
            cost for task in resolved if (cost := getattr(task, "cost_usd", None)) is not None
        ]
        closed_costs = [
            cost
            for task in closed
            if getattr(task, "resolved", False) and (cost := getattr(task, "cost_usd", None)) is not None
        ]
        solved = [task for task in closed if getattr(task, "outcome", None) == "solved"]
        solved_costs = [
            cost
            for task in solved
            if getattr(task, "resolved", False) and (cost := getattr(task, "cost_usd", None)) is not None
        ]
        failed = [task for task in closed if getattr(task, "outcome", None) in {"failed", "abandoned"}]
        failed_costs = [
            cost
            for task in failed
            if getattr(task, "resolved", False) and (cost := getattr(task, "cost_usd", None)) is not None
        ]
        cleanup = [
            cost
            for task in cohort
            if (cost := getattr(task, "cleanup_cost_usd", None)) is not None
        ]
        corrected = [
            (getattr(task, "cost_usd", 0) or 0) + (getattr(task, "cleanup_cost_usd", None) or 0)
            for task in cohort
            if getattr(task, "reviews", None)
            and getattr(task, "resolved", False)
            and any(
                getattr(review.event, "review_outcome", None) == "correct"
                for review in getattr(task, "reviews", [])
            )
        ]
        metrics[label] = WorkloadTaskMetrics(
            attempted_tasks=len(cohort),
            closed_tasks=len(closed),
            solved_tasks=len(solved),
            # A cohort with any unpriced task reports no cost per task: a partial
            # sum would understate the real figure. This mirrors
            # :mod:`tokenlens.economics` exactly so the two views never disagree.
            cost_per_attempted_task=(
                round(sum(resolved_costs) / len(cohort), 6)
                if cohort and len(resolved_costs) == len(cohort)
                else None
            ),
            cost_per_closed_task=(
                round(sum(closed_costs) / len(closed), 6)
                if closed and len(closed_costs) == len(closed)
                else None
            ),
            cost_per_solved_task=(
                round(sum(solved_costs) / len(solved), 6)
                if solved and len(solved_costs) == len(solved)
                else None
            ),
            p50_task_cost=(
                round(value, 6) if (value := _percentile(resolved_costs, 0.5)) is not None else None
            ),
            p90_task_cost=(
                round(value, 6) if (value := _percentile(resolved_costs, 0.9)) is not None else None
            ),
            failed_trajectory_cost=(
                round(sum(failed_costs), 6) if failed_costs and len(failed_costs) == len(failed) else None
            ),
            retries_per_task=(
                round(sum(getattr(task, "retry_count", 0) for task in cohort) / len(cohort), 3)
                if cohort
                else None
            ),
            model_calls_per_task=(
                round(sum(getattr(task, "model_calls", 0) for task in cohort) / len(cohort), 3)
                if cohort
                else None
            ),
            cost_per_correct_task=round(sum(corrected) / len(corrected), 6) if corrected else None,
            observed_cleanup_cost=round(sum(cleanup), 6) if cleanup else None,
        )
    return metrics
