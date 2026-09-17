"""Offline PTU suitability and cost analysis.

The decision shape, thresholds, capacity table, and cost formulas are adapted
from the MIT-licensed https://github.com/msftse-org/ptu-advisor project at
revision eb0558cd4c6d3794be76d9caa2e87129d1f8221c.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from math import ceil, sqrt
from statistics import mean, pstdev
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import AggregateAnalysisSummary, DeploymentAnalysis, TraceRecord
from .pricing import PricingResolution, canonical_model_name, infer_publisher

#: Resolves one analysis event to the same pricing decision the cost engine
#: used. The PTU dashboard never recomputes rates of its own.
CostResolver = Callable[[TraceRecord], PricingResolution]


class PtuDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    verdict: Literal["positive", "neutral", "negative", "insufficient"]
    summary: str
    metric: str


EligibilityStatus = Literal[
    "eligible_sufficient_evidence",
    "eligible_insufficient_evidence",
    "model_capacity_unavailable",
    "ptu_not_applicable",
    "pricing_unavailable",
    "deployment_mode_unavailable",
    "collection_identity_error",
]

ELIGIBILITY_STATUS_LABELS: dict[str, str] = {
    "eligible_sufficient_evidence": "Eligible and sufficient evidence",
    "eligible_insufficient_evidence": "Eligible but insufficient evidence",
    "model_capacity_unavailable": "Model capacity unavailable",
    "ptu_not_applicable": "PTU not applicable",
    "pricing_unavailable": "Pricing unavailable",
    "deployment_mode_unavailable": "Deployment mode unavailable",
    "collection_identity_error": "Collection identity error",
}

MINIMUM_ACTIVE_BUCKETS = 100

#: Provenance for every row of the PTU capacity table. Capacity is matched on the
#: exact canonical model key only: an unlisted model reports "capacity
#: unavailable" rather than borrowing a related model's numbers.
CAPACITY_SOURCE = "msftse-org/ptu-advisor @ eb0558cd4c6d3794be76d9caa2e87129d1f8221c"


class PtuThroughputPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_index: int
    minutes_from_start: int
    tpm: float


class PtuThroughputSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_minutes: int
    points: list[PtuThroughputPoint] = Field(default_factory=list)
    average_tpm: float = Field(ge=0)
    reference_tpm: float = Field(ge=0)
    reference_label: str = "P95"
    ptu_capacity_tpm: float | None = Field(default=None, ge=0)


class PtuCostCurve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sustained_tpm: list[float] = Field(default_factory=list)
    payg_monthly: list[float] = Field(default_factory=list)
    hybrid_monthly: list[float] = Field(default_factory=list)
    lower_break_even_tpm: float | None = Field(default=None, ge=0)
    upper_break_even_tpm: float | None = Field(default=None, ge=0)
    selected_ptu: int = Field(ge=0)
    ptu_capacity_tpm: float = Field(ge=0)
    observed_average_tpm: float = Field(ge=0)
    payg_at_observed_average: float | None = Field(default=None, ge=0)
    hybrid_at_observed_average: float | None = Field(default=None, ge=0)


DashboardState = Literal[
    "ptu_recommended",
    "borderline",
    "payg_recommended",
    "collection_identity_error",
    "insufficient_evidence",
    "pricing_unavailable",
    "ptu_not_applicable",
    "capacity_unavailable",
]

DASHBOARD_STATE_LABELS: dict[str, str] = {
    "ptu_recommended": "PTU Recommended",
    "borderline": "Borderline — validate before committing",
    "payg_recommended": "PAYG Recommended",
    "collection_identity_error": "Collection Identity Error",
    "insufficient_evidence": "Insufficient Evidence",
    "pricing_unavailable": "Pricing Required",
    "ptu_not_applicable": "PTU Not Applicable",
    "capacity_unavailable": "Capacity Data Required",
}

#: States where a classification was actually produced. Confidence describes
#: confidence in that classification, so it is withheld everywhere else.
STATES_WITH_CONFIDENCE = frozenset({"ptu_recommended", "borderline", "payg_recommended"})


class PtuEvidencePoint(BaseModel):
    """One observed time bucket. ``None`` means the source did not report it."""

    model_config = ConfigDict(extra="forbid")

    timestamp: datetime
    input_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    successful_requests: int | None = Field(default=None, ge=0)
    rate_limited_requests: int | None = Field(default=None, ge=0)
    failed_requests: int | None = Field(default=None, ge=0)
    total_requests: int | None = Field(default=None, ge=0)


class PtuDailyCostPoint(BaseModel):
    """One observed day of cost, produced by the shared pricing engine."""

    model_config = ConfigDict(extra="forbid")

    date: date
    input_cost: float | None = Field(default=None, ge=0)
    cached_input_cost: float | None = Field(default=None, ge=0)
    output_cost: float | None = Field(default=None, ge=0)
    total_cost: float | None = Field(default=None, ge=0)
    pricing_coverage_tokens_percent: float = Field(default=0, ge=0, le=100)
    total_tokens: int = Field(default=0, ge=0)
    unpriced_tokens: int = Field(default=0, ge=0)
    partial_day: bool = False


class PtuDashboardSummary(BaseModel):
    """Decision header and At-a-Glance metrics for one selected deployment."""

    model_config = ConfigDict(extra="forbid")

    deployment_name: str
    model_name: str
    model_version: str | None = None
    deployment_mode: str
    state: DashboardState
    state_label: str
    recommendation: str
    #: Blockers that exist but are not the primary state, rendered as badges.
    secondary_blockers: list[str] = Field(default_factory=list)
    confidence_percent: float | None = Field(default=None, ge=0, le=100)
    confidence_label: str | None = None
    summary: str
    average_weighted_tpm: float | None = Field(default=None, ge=0)
    p95_weighted_tpm: float | None = Field(default=None, ge=0)
    #: Throughput across *active* buckets, so a busy period is not diluted by a
    #: mostly idle window.
    active_average_weighted_tpm: float | None = Field(default=None, ge=0)
    active_p95_weighted_tpm: float | None = Field(default=None, ge=0)
    busy_hour_available: bool = False
    weighted_basis: str
    total_input_tokens: int | None = Field(default=None, ge=0)
    total_cached_tokens: int | None = Field(default=None, ge=0)
    total_output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    daily_average_tokens: float | None = Field(default=None, ge=0)
    rate_limited_requests: int | None = Field(default=None, ge=0)
    rate_limit_percent: float | None = Field(default=None, ge=0, le=100)
    successful_requests: int | None = Field(default=None, ge=0)
    other_failed_requests: int | None = Field(default=None, ge=0)
    outcome_coverage: Literal["complete", "partial", "unavailable"] = "unavailable"
    total_requests: int | None = Field(default=None, ge=0)
    requests_available: bool = True
    retries_available: bool = True
    observed_days: float = Field(ge=0)
    active_days: int = Field(default=0, ge=0)
    complete_days: int = Field(default=0, ge=0)
    partial_days: int = Field(default=0, ge=0)
    window_start: datetime | None = None
    window_end: datetime | None = None
    active_buckets: int = Field(default=0, ge=0)
    observed_buckets: int = Field(default=0, ge=0)
    elapsed_buckets: int = Field(default=0, ge=0)
    collection_completeness_percent: float = Field(default=0, ge=0, le=100)
    bucket_minutes: int = Field(default=5, gt=0)
    identity_resolved: bool = True
    pricing_status: str = "priced"
    pricing_coverage_tokens_percent: float = Field(default=0, ge=0, le=100)
    pricing_currency: str = "USD"
    total_cost: float | None = Field(default=None, ge=0)
    average_daily_cost: float | None = Field(default=None, ge=0)
    data_quality: str
    smoke_test_window: bool = False


class PtuConfidenceComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    weight: float = Field(ge=0, le=1)
    score: float = Field(ge=0, le=1)
    detail: str


class PtuDashboardData(BaseModel):
    """The only payload the PTU report renderer consumes."""

    model_config = ConfigDict(extra="forbid")

    summary: PtuDashboardSummary
    evidence: list[PtuEvidencePoint] = Field(default_factory=list)
    daily_cost: list[PtuDailyCostPoint] = Field(default_factory=list)
    throughput: PtuThroughputSeries | None = None
    cost_curve: PtuCostCurve | None = None
    confidence_components: list[PtuConfidenceComponent] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_metrics: list[str] = Field(default_factory=list)
    recommendation_reasons: list[str] = Field(default_factory=list)
    what_would_change: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    data_quality_notes: list[str] = Field(default_factory=list)


class PtuDeploymentAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_name: str
    model_name: str
    deployment_mode: str
    eligible: bool
    eligibility_status: EligibilityStatus = "model_capacity_unavailable"
    recommendation: Literal[
        "PTU recommended",
        "Borderline",
        "PAYG recommended",
        "Insufficient evidence",
        "Model not supported",
        "PTU not applicable",
    ]
    economic_result: Literal["PTU lower", "PAYG lower", "Unavailable"]
    data_points: int = Field(ge=0)
    observed_buckets: int = Field(ge=0)
    active_buckets: int = Field(default=0, ge=0)
    elapsed_buckets: int = Field(default=0, ge=0)
    identity_resolved: bool = True
    observed_days: float = Field(ge=0)
    average_tpm: float = Field(ge=0)
    p95_tpm: float = Field(ge=0)
    throttling_rate_percent: float = Field(ge=0, le=100)
    p50_latency_ms: float | None = Field(default=None, ge=0)
    p95_latency_ms: float | None = Field(default=None, ge=0)
    p99_latency_ms: float | None = Field(default=None, ge=0)
    suggested_ptu: int | None = Field(default=None, ge=0)
    ptu_capacity_tpm: float | None = Field(default=None, ge=0)
    payg_monthly_usd: float | None = Field(default=None, ge=0)
    ptu_hourly_monthly_usd: float | None = Field(default=None, ge=0)
    ptu_reserved_monthly_usd: float | None = Field(default=None, ge=0)
    hybrid_monthly_usd: float | None = Field(default=None, ge=0)
    break_even_tpm: float | None = Field(default=None, ge=0)
    spillover_percent: float | None = Field(default=None, ge=0, le=100)
    dimensions: list[PtuDimension] = Field(default_factory=list)
    throughput_series: PtuThroughputSeries | None = None
    cost_curve: PtuCostCurve | None = None
    dashboard: PtuDashboardData | None = None
    note: str


class PtuPortfolioAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_repository: str = "https://github.com/msftse-org/ptu-advisor"
    source_revision: str = "eb0558cd4c6d3794be76d9caa2e87129d1f8221c"
    bucket_minutes: int = 5
    recommended_deployments: int = 0
    borderline_deployments: int = 0
    payg_deployments: int = 0
    insufficient_deployments: int = 0
    deployments: list[PtuDeploymentAssessment] = Field(default_factory=list)


class _Capacity(BaseModel):
    payg_tpm_quota: int
    input_tpm_per_ptu: int
    output_ratio: int
    global_min_ptu: int
    global_increment: int
    regional_min_ptu: int
    regional_increment: int
    #: Where this row came from. Capacity is only ever used for the exact
    #: canonical model key; a related family member is never substituted.
    source: str = CAPACITY_SOURCE
    confidence: Literal["documented_reference", "customer_override"] = "documented_reference"


_CAPACITIES = {
    "gpt-5.5": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=1200, output_ratio=6, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-5.4-mini": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=7900, output_ratio=6, global_min_ptu=15, global_increment=5, regional_min_ptu=25, regional_increment=25),
    "gpt-5.4": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=2400, output_ratio=6, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-5.3-codex": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=3400, output_ratio=8, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-5.2-codex": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=3400, output_ratio=8, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-5.2": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=3400, output_ratio=8, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-5.1-codex": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=4750, output_ratio=8, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-5.1": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=4750, output_ratio=8, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-5-mini": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=23750, output_ratio=8, global_min_ptu=15, global_increment=5, regional_min_ptu=25, regional_increment=25),
    "gpt-5": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=4750, output_ratio=8, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-4.1": _Capacity(payg_tpm_quota=720_000, input_tpm_per_ptu=3000, output_ratio=4, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-4.1-mini": _Capacity(payg_tpm_quota=2_000_000, input_tpm_per_ptu=14900, output_ratio=4, global_min_ptu=15, global_increment=5, regional_min_ptu=25, regional_increment=25),
    "gpt-4.1-nano": _Capacity(payg_tpm_quota=4_000_000, input_tpm_per_ptu=59400, output_ratio=4, global_min_ptu=15, global_increment=5, regional_min_ptu=25, regional_increment=25),
    "gpt-4o": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=2500, output_ratio=4, global_min_ptu=15, global_increment=5, regional_min_ptu=50, regional_increment=50),
    "gpt-4o-mini": _Capacity(payg_tpm_quota=2_000_000, input_tpm_per_ptu=37000, output_ratio=4, global_min_ptu=15, global_increment=5, regional_min_ptu=25, regional_increment=25),
    "o1": _Capacity(payg_tpm_quota=450_000, input_tpm_per_ptu=230, output_ratio=4, global_min_ptu=15, global_increment=5, regional_min_ptu=25, regional_increment=50),
    "o3-mini": _Capacity(payg_tpm_quota=2_000_000, input_tpm_per_ptu=2500, output_ratio=4, global_min_ptu=15, global_increment=5, regional_min_ptu=25, regional_increment=25),
    "o4-mini": _Capacity(payg_tpm_quota=2_000_000, input_tpm_per_ptu=5400, output_ratio=4, global_min_ptu=15, global_increment=5, regional_min_ptu=25, regional_increment=25),
}


def _capacity_for(model_name: str) -> _Capacity | None:
    canonical = canonical_model_name(model_name)
    return _CAPACITIES.get(canonical)


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


#: A busy-hour statistic needs at least an hour of active five-minute buckets
#: before it describes a busy period rather than a single spike.
MINIMUM_BUSY_HOUR_BUCKETS = 12


@dataclass
class _Evidence:
    """Elapsed, observed, and active evidence kept strictly separate."""

    input_tpm: list[float]
    output_tpm: list[float]
    records: list[TraceRecord]
    observed_days: float
    elapsed_buckets: int
    observed_buckets: int
    active_buckets: int
    active_input_tpm: list[float]
    active_output_tpm: list[float]

    @property
    def tpm(self) -> list[float]:
        return [value + other for value, other in zip(self.input_tpm, self.output_tpm)]

    @property
    def active_tpm(self) -> list[float]:
        return [value + other for value, other in zip(self.active_input_tpm, self.active_output_tpm)]

    @property
    def collection_completeness_percent(self) -> float:
        if not self.elapsed_buckets:
            return 0.0
        return round(min(100.0, self.observed_buckets / self.elapsed_buckets * 100), 1)

    def weighted(self, output_ratio: int | None) -> list[float]:
        ratio = output_ratio or 1
        return [value + other * ratio for value, other in zip(self.input_tpm, self.output_tpm)]

    def active_weighted(self, output_ratio: int | None) -> list[float]:
        ratio = output_ratio or 1
        return [value + other * ratio for value, other in zip(self.active_input_tpm, self.active_output_tpm)]


def _correlation(values: list[float], lag: int) -> float | None:
    if len(values) < lag * 2:
        return None
    left, right = values[:-lag], values[lag:]
    left_mean, right_mean = mean(left), mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right))
    left_sum = sum((value - left_mean) ** 2 for value in left)
    right_sum = sum((value - right_mean) ** 2 for value in right)
    denominator = sqrt(left_sum * right_sum)
    return max(0.0, numerator / denominator) if denominator else 0.5


def _series(
    records: list[TraceRecord],
    bucket_minutes: int = 5,
    *,
    aggregate: "AggregateAnalysisSummary | None" = None,
) -> "_Evidence":
    """Build the elapsed timeline and count active intervals separately.

    Three counts are produced and never interchanged:

    ``elapsed``   intervals the collection window covers;
    ``observed``  intervals the source returned a data point for;
    ``active``    observed intervals with nonzero token or request volume.

    A zero-filled idle window therefore cannot earn sample-size credit, and a
    sparse window cannot masquerade as perfect continuity.
    """
    parsed = [(stamp, record) for record in records if (stamp := _timestamp(record.timestamp)) is not None]
    if not parsed:
        return _Evidence(
            input_tpm=[],
            output_tpm=[],
            records=[],
            observed_days=aggregate.observed_days if aggregate else 0.0,
            elapsed_buckets=aggregate.elapsed_buckets if aggregate else 0,
            observed_buckets=0,
            active_buckets=0,
            active_input_tpm=[],
            active_output_tpm=[],
        )
    parsed.sort(key=lambda item: item[0])
    end = parsed[-1][0]
    start_limit = end - timedelta(days=90)
    parsed = [item for item in parsed if item[0] >= start_limit]
    bucket_seconds = bucket_minutes * 60
    buckets: dict[int, tuple[float, float, float]] = defaultdict(lambda: (0.0, 0.0, 0.0))
    for stamp, record in parsed:
        key = int(stamp.timestamp()) // bucket_seconds
        input_tokens, output_tokens, requests = buckets[key]
        bucket_requests = 0.0
        metrics = _aggregate_metrics(record)
        if metrics is not None:
            bucket_requests = float(metrics.get("requests") or 0)
        else:
            bucket_requests = 1.0
        buckets[key] = (
            input_tokens + record.usage.input_tokens,
            output_tokens + record.usage.output_tokens,
            requests + bucket_requests,
        )
    first, last = min(buckets), max(buckets)
    span = list(range(first, last + 1))
    inputs = [buckets.get(key, (0.0, 0.0, 0.0))[0] / bucket_minutes for key in span]
    outputs = [buckets.get(key, (0.0, 0.0, 0.0))[1] / bucket_minutes for key in span]
    active_keys = [
        key
        for key in span
        if (buckets.get(key, (0.0, 0.0, 0.0))[0] + buckets.get(key, (0.0, 0.0, 0.0))[1]) > 0
        or buckets.get(key, (0.0, 0.0, 0.0))[2] > 0
    ]
    active_inputs = [buckets[key][0] / bucket_minutes for key in active_keys]
    active_outputs = [buckets[key][1] / bucket_minutes for key in active_keys]
    observed_days = max(bucket_minutes / 1440, (last - first + 1) * bucket_minutes / 1440)
    elapsed = len(span)
    if aggregate is not None and aggregate.elapsed_buckets:
        elapsed = max(elapsed, aggregate.elapsed_buckets)
        observed_days = max(observed_days, aggregate.observed_days)
    return _Evidence(
        input_tpm=inputs,
        output_tpm=outputs,
        records=[record for _, record in parsed],
        observed_days=observed_days,
        elapsed_buckets=elapsed,
        observed_buckets=len(buckets),
        active_buckets=len(active_keys),
        active_input_tpm=active_inputs,
        active_output_tpm=active_outputs,
    )


def _dimension(name: str, verdict: str, summary: str, metric: str) -> PtuDimension:
    return PtuDimension(name=name, verdict=verdict, summary=summary, metric=metric)


def _dimensions(
    records: list[TraceRecord],
    tpm: list[float],
    active_buckets: int,
    capacity: _Capacity | None,
    *,
    outcomes: dict[str, int | None] | None = None,
) -> tuple[list[PtuDimension], dict[str, float | None]]:
    """Score workload dimensions only when there is distribution evidence.

    Below the active-bucket threshold every distribution-dependent dimension is
    ``insufficient``. A mostly idle window is not a "stable", "sparse", or
    "predictable" workload — it is an unmeasured one.
    """
    outcomes = outcomes or {}
    aggregate_source = bool(outcomes)
    total_requests = outcomes.get("total_requests")
    throttled = outcomes.get("rate_limited_requests")
    if not aggregate_source:
        total_requests = len(records) or None
        throttled = sum(record.status_code == 429 for record in records) if records else None
    # The rate needs a numerator and a denominator from the same source. A
    # partial or missing denominator produces no rate at all, never a rate above
    # 100%.
    throttle_rate = (
        throttled / total_requests
        if (throttled is not None and total_requests and throttled <= total_requests)
        else None
    )

    latencies = [record.latency_ms for record in records if record.latency_ms is not None]
    p50_latency = _percentile(latencies, 50) if latencies else None
    p95_latency = _percentile(latencies, 95) if latencies else None
    p99_latency = _percentile(latencies, 99) if latencies else None
    metrics: dict[str, float | None] = {
        "throttle_rate": throttle_rate,
        "p50_latency": p50_latency,
        "p95_latency": p95_latency,
        "p99_latency": p99_latency,
    }

    if active_buckets < MINIMUM_ACTIVE_BUCKETS:
        metric = f"{active_buckets:,} active · {len(tpm):,} elapsed buckets"
        need = f"Needs activity in at least {MINIMUM_ACTIVE_BUCKETS} five-minute buckets."
        observed = (
            f"{throttled:,} of {total_requests:,} requests rate limited"
            if throttle_rate is not None and total_requests
            else "Request-outcome evidence unavailable"
        )
        return (
            [
                _dimension("Workload shape", "insufficient", need, metric),
                _dimension("Capacity pressure", "insufficient", f"{need} Observed: {observed}.", metric),
                _dimension(
                    "Latency sensitivity",
                    "insufficient",
                    "No representative latency distribution was observed." if not latencies else need,
                    "Latency unavailable" if not latencies else metric,
                ),
                _dimension("Load predictability", "insufficient", need, metric),
            ],
            metrics,
        )

    average = mean(tpm) if tpm else 0
    cv = pstdev(tpm) / average if average else 0
    if cv < 0.3:
        workload = _dimension("Workload shape", "positive", "Stable throughput favors reserved capacity.", f"TPM CV {cv:.2f}")
    elif cv > 0.7:
        workload = _dimension("Workload shape", "negative", "Spiky throughput risks unused PTU capacity.", f"TPM CV {cv:.2f}")
    else:
        workload = _dimension("Workload shape", "neutral", "Throughput variability is moderate.", f"TPM CV {cv:.2f}")

    peak = max(tpm, default=0)
    sustained = sum(value >= peak * 0.3 for value in tpm) / len(tpm) if peak else 0
    daily = _correlation(tpm, 288)
    weekly = _correlation(tpm, 2016)
    similarity = max(value for value in (daily, weekly, 0.5) if value is not None)
    if sustained >= 0.6 and similarity >= 0.7:
        predictability = _dimension("Load predictability", "positive", "Sustained, repeating demand favors PTU.", f"{sustained:.0%} sustained · {similarity:.0%} similarity")
    elif sustained < 0.3:
        predictability = _dimension("Load predictability", "negative", "Sparse demand favors PAYG.", f"{sustained:.0%} sustained · {similarity:.0%} similarity")
    else:
        predictability = _dimension("Load predictability", "neutral", "Demand is only moderately predictable.", f"{sustained:.0%} sustained · {similarity:.0%} similarity")

    p95_tpm = _percentile(tpm, 95)
    utilization = p95_tpm / capacity.payg_tpm_quota if capacity and capacity.payg_tpm_quota else 0
    if throttle_rate is None:
        pressure = _dimension(
            "Capacity pressure",
            "insufficient",
            "Request-outcome telemetry is required before quota pressure can be judged.",
            f"Outcomes unavailable · {utilization:.1%} quota",
        )
    elif throttle_rate > 0.05 or utilization > 0.85:
        pressure = _dimension("Capacity pressure", "positive", "PAYG capacity pressure favors dedicated throughput.", f"{throttle_rate:.1%} throttled · {utilization:.1%} quota")
    elif throttle_rate < 0.01 and utilization < 0.5:
        pressure = _dimension("Capacity pressure", "negative", "PAYG is handling observed demand.", f"{throttle_rate:.1%} throttled · {utilization:.1%} quota")
    else:
        pressure = _dimension("Capacity pressure", "neutral", "Some capacity pressure is present.", f"{throttle_rate:.1%} throttled · {utilization:.1%} quota")

    if p50_latency is None:
        latency = _dimension("Latency sensitivity", "insufficient", "No request-latency evidence was supplied.", "Latency unavailable")
    else:
        ratio = p99_latency / p50_latency if p50_latency else 0
        if (p95_latency or 0) > 5000 or ratio > 4:
            latency = _dimension("Latency sensitivity", "positive", "High or unstable request latency can favor dedicated capacity.", f"P95 {p95_latency:.0f} ms · P99/P50 {ratio:.1f}x")
        elif (p95_latency or 0) < 2000 and ratio < 2:
            latency = _dimension("Latency sensitivity", "negative", "Observed request latency is stable.", f"P95 {p95_latency:.0f} ms · P99/P50 {ratio:.1f}x")
        else:
            latency = _dimension("Latency sensitivity", "neutral", "Request latency is moderate.", f"P95 {p95_latency:.0f} ms · P99/P50 {ratio:.1f}x")
    return [workload, pressure, latency, predictability], metrics


def _recommendation(dimensions: list[PtuDimension], eligible: bool, publisher: str | None) -> str:
    if not eligible:
        if publisher and publisher != "microsoft":
            return "PTU not applicable"
        return "Model not supported"
    if sum(item.verdict == "insufficient" for item in dimensions) >= 2:
        return "Insufficient evidence"
    positives = sum(item.verdict == "positive" for item in dimensions)
    if positives >= 3:
        return "PTU recommended"
    if positives >= 2:
        return "Borderline"
    return "PAYG recommended"


def _throughput_series(tpm: list[float], *, bucket_minutes: int, ptu_capacity_tpm: float | None) -> PtuThroughputSeries:
    return PtuThroughputSeries(
        bucket_minutes=bucket_minutes,
        points=[
            PtuThroughputPoint(bucket_index=index, minutes_from_start=index * bucket_minutes, tpm=round(value, 2))
            for index, value in enumerate(tpm)
        ],
        average_tpm=round(mean(tpm), 2) if tpm else 0.0,
        reference_tpm=round(_percentile(tpm, 95), 2) if tpm else 0.0,
        ptu_capacity_tpm=ptu_capacity_tpm,
    )


def _cost_curve(
    *,
    weighted_tpm: list[float],
    average_tpm: float,
    selected_ptu: int,
    capacity_tpm: float,
    reserved_monthly: float,
    blended_rate: float,
    payg_monthly_usd: float,
    hybrid_monthly_usd: float,
) -> PtuCostCurve:
    """Build deterministic curve points from the exact functions the engine used to pick the baseline.

    Both lines are the idealized "sustained constant TPM for a month" model
    the engine already uses for ``payg_monthly_usd``/``hybrid_monthly_usd``
    (mean(tpm) * 43_200 / 1_000_000 * blended_rate, and reserved cost plus
    overflow at the same blended rate). The "current" markers use the
    engine's own scalars rather than re-deriving them from the line, so they
    always agree exactly with the rest of the report even though the real
    observed distribution is peakier than a constant-TPM line.
    """
    rate_per_tpm_month = 43_200 / 1_000_000 * blended_rate
    peak = max(weighted_tpm, default=0.0)
    max_x = max(peak, capacity_tpm, average_tpm, 1.0) * 1.2
    steps = 60
    sustained = [max_x * index / steps for index in range(steps + 1)]
    payg_line = [value * rate_per_tpm_month for value in sustained]
    hybrid_line = [reserved_monthly + max(0.0, value - capacity_tpm) * rate_per_tpm_month for value in sustained]
    diff = [hybrid_value - payg_value for hybrid_value, payg_value in zip(hybrid_line, payg_line)]
    crossings: list[float] = []
    for index in range(1, len(diff)):
        previous, current = diff[index - 1], diff[index]
        if previous == 0:
            crossings.append(sustained[index - 1])
        elif (previous < 0) != (current < 0):
            fraction = abs(previous) / (abs(previous) + abs(current))
            crossings.append(sustained[index - 1] + fraction * (sustained[index] - sustained[index - 1]))
    lower_break_even = round(crossings[0], 2) if crossings else None
    upper_break_even = round(crossings[-1], 2) if len(crossings) > 1 else None
    return PtuCostCurve(
        sustained_tpm=[round(value, 2) for value in sustained],
        payg_monthly=[round(value, 4) for value in payg_line],
        hybrid_monthly=[round(value, 4) for value in hybrid_line],
        lower_break_even_tpm=lower_break_even,
        upper_break_even_tpm=upper_break_even,
        selected_ptu=selected_ptu,
        ptu_capacity_tpm=capacity_tpm,
        observed_average_tpm=round(average_tpm, 2),
        payg_at_observed_average=round(payg_monthly_usd, 4),
        hybrid_at_observed_average=round(hybrid_monthly_usd, 4),
    )


BUCKET_MINUTES = 5


def _aggregate_metrics(record: TraceRecord) -> dict[str, object] | None:
    """Return collector-provided aggregate metrics, when the record is a bucket."""
    metadata = record.metadata or {}
    if metadata.get("record_type") != "foundry_metric_bucket":
        return None
    metrics = metadata.get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def _bucket_key(stamp: datetime, bucket_minutes: int = BUCKET_MINUTES) -> datetime:
    seconds = bucket_minutes * 60
    return datetime.fromtimestamp(int(stamp.timestamp()) // seconds * seconds, tz=UTC)


class _BucketAggregate:
    __slots__ = (
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "successful",
        "rate_limited",
        "failed",
        "requests",
        "cached_known",
        "outcome_known",
        "requests_known",
    )

    def __init__(self) -> None:
        self.input_tokens = 0
        self.cached_tokens = 0
        self.output_tokens = 0
        self.successful = 0
        self.rate_limited = 0
        self.failed = 0
        self.requests = 0
        self.cached_known = False
        self.outcome_known = False
        self.requests_known = False


def _evidence_table(records: list[TraceRecord]) -> tuple[dict[datetime, _BucketAggregate], set[str]]:
    """Aggregate request or bucket telemetry into aligned five-minute buckets.

    Missing metrics are reported explicitly instead of being zero-filled: a
    source that never reported request outcomes is not evidence that no request
    ever failed.
    """
    table: dict[datetime, _BucketAggregate] = {}
    missing: set[str] = set()
    for record in records:
        stamp = _timestamp(record.timestamp)
        if stamp is None:
            missing.add("timestamp")
            continue
        bucket = table.setdefault(_bucket_key(stamp), _BucketAggregate())
        metrics = _aggregate_metrics(record)
        if metrics is not None:
            bucket.input_tokens += int(metrics.get("input_tokens") or 0)
            bucket.output_tokens += int(metrics.get("output_tokens") or 0)
            if metrics.get("cached_tokens") is not None:
                bucket.cached_tokens += int(metrics["cached_tokens"])
                bucket.cached_known = True
            else:
                missing.add("cached_tokens")
            requests = metrics.get("requests")
            successful = metrics.get("successful_requests")
            throttled = metrics.get("throttled_requests")
            failed = metrics.get("failed_requests")
            if requests is not None:
                bucket.requests += int(requests)
                bucket.requests_known = True
            else:
                missing.add("request_totals")
            if successful is None and throttled is None:
                missing.add("request_outcomes")
            else:
                bucket.outcome_known = True
                bucket.successful += int(successful or 0)
                bucket.rate_limited += int(throttled or 0)
                if failed is not None:
                    bucket.failed += int(failed)
                elif successful is not None and throttled is not None and requests is not None:
                    bucket.failed += max(0, int(requests) - int(successful) - int(throttled))
                else:
                    missing.add("failed_requests")
            continue
        bucket.input_tokens += record.usage.input_tokens
        bucket.output_tokens += record.usage.output_tokens
        bucket.cached_tokens += record.usage.cached_tokens
        # Request-level telemetry always knows its own cached count, including
        # a genuine zero.
        bucket.cached_known = True
        bucket.requests += 1
        bucket.requests_known = True
        status = record.status_code
        if status is None:
            missing.add("request_outcomes")
            continue
        bucket.outcome_known = True
        if status == 429:
            bucket.rate_limited += 1
        elif 200 <= status < 400:
            bucket.successful += 1
        else:
            bucket.failed += 1
    return table, missing


def _confidence(
    *,
    active_buckets: int,
    observed_buckets: int,
    elapsed_buckets: int,
    observed_days: float,
    outcome_coverage: float,
    latency_coverage: float,
    pricing_coverage: float,
    capacity_known: bool,
    mode_known: bool,
    identity_known: bool,
) -> tuple[float, list[PtuConfidenceComponent]]:
    """Score confidence in the *evidence*, never in how favourable PTU looks.

    Collection completeness and active sample size are separate components.
    Observed-versus-expected buckets measure whether the collection worked;
    active buckets measure whether there was traffic to analyse. A silent
    window scores full completeness and no sample size, which is exactly what
    the evidence says.
    """
    components = [
        PtuConfidenceComponent(
            name="Active sample size",
            weight=0.30,
            score=min(1.0, active_buckets / MINIMUM_ACTIVE_BUCKETS),
            detail=f"{active_buckets:,} of {MINIMUM_ACTIVE_BUCKETS} required active buckets",
        ),
        PtuConfidenceComponent(
            name="Lookback duration",
            weight=0.18,
            score=min(1.0, observed_days / 7),
            detail=f"{observed_days:.2f} of 7 reference days observed",
        ),
        PtuConfidenceComponent(
            name="Collection completeness",
            weight=0.15,
            score=min(1.0, observed_buckets / elapsed_buckets) if elapsed_buckets else 0.0,
            detail=f"{observed_buckets:,} observed of {elapsed_buckets:,} elapsed buckets",
        ),
        PtuConfidenceComponent(
            name="Request-outcome coverage",
            weight=0.10,
            score=max(0.0, min(1.0, outcome_coverage)),
            detail=f"{outcome_coverage * 100:.1f}% of buckets report outcomes",
        ),
        PtuConfidenceComponent(
            name="Latency coverage",
            weight=0.10,
            score=max(0.0, min(1.0, latency_coverage)),
            detail=f"{latency_coverage * 100:.1f}% of events report latency",
        ),
        PtuConfidenceComponent(
            name="Pricing coverage",
            weight=0.10,
            score=max(0.0, min(1.0, pricing_coverage)),
            detail=f"{pricing_coverage * 100:.1f}% of tokens priced exactly",
        ),
        PtuConfidenceComponent(
            name="Model identity",
            weight=0.02,
            score=1.0 if identity_known else 0.0,
            detail="Exact model and version resolved" if identity_known else "Model identity unresolved",
        ),
        PtuConfidenceComponent(
            name="Model capacity",
            weight=0.03,
            score=1.0 if capacity_known else 0.0,
            detail="Exact PTU capacity available" if capacity_known else "PTU capacity unavailable",
        ),
        PtuConfidenceComponent(
            name="Deployment mode",
            weight=0.02,
            score=1.0 if mode_known else 0.0,
            detail="Global or Regional confirmed" if mode_known else "Deployment mode unknown",
        ),
    ]
    total = sum(item.weight * item.score for item in components)
    return round(total * 100, 1), components


def _confidence_label(value: float) -> str:
    if value >= 90:
        return "High confidence"
    if value >= 70:
        return "Moderate confidence"
    if value >= 50:
        return "Low confidence"
    return "Insufficient evidence"


def _dashboard_state(
    *,
    eligibility_status: EligibilityStatus,
    recommendation: str,
    publisher: str | None,
    identity_resolved: bool = True,
    sufficient_evidence: bool = True,
) -> DashboardState:
    """Deterministic precedence: the *primary* blocker is shown first.

    Capacity and pricing are real blockers, but they are secondary to identity
    and evidence: sizing a model you cannot identify, from two active buckets,
    is not a capacity problem.
    """
    if not identity_resolved:
        return "collection_identity_error"
    # PTU applicability is categorical: no amount of extra evidence makes an
    # Azure PTU purchase possible for a consumption-billed partner model, so it
    # is reported ahead of sample-size problems.
    if eligibility_status == "ptu_not_applicable":
        return "ptu_not_applicable"
    if not sufficient_evidence:
        return "insufficient_evidence"
    if eligibility_status == "model_capacity_unavailable":
        return "capacity_unavailable"
    if eligibility_status == "pricing_unavailable":
        return "pricing_unavailable"
    if eligibility_status in {"eligible_insufficient_evidence", "deployment_mode_unavailable"}:
        return "insufficient_evidence"
    if recommendation == "PTU recommended":
        return "ptu_recommended"
    if recommendation == "Borderline":
        return "borderline"
    if recommendation == "PAYG recommended":
        return "payg_recommended"
    return "insufficient_evidence"


_STATE_SUMMARIES: dict[str, str] = {
    "ptu_recommended": "Observed throughput, capacity pressure, and modeled economics all support committing to dedicated capacity.",
    "borderline": "PTU and PAYG are currently close. Validate with additional data before committing.",
    "payg_recommended": "Pay-as-you-go remains the safer or cheaper strategy for the observed demand.",
    "collection_identity_error": "The collected telemetry does not identify the deployment's exact model, so no capacity, pricing, or workload conclusion can be drawn.",
    "insufficient_evidence": "The observed window cannot support a PTU recommendation yet.",
    "pricing_unavailable": "Workload evidence exists, but exact PAYG pricing is required before the economics can be calculated.",
    "ptu_not_applicable": "This model is billed through Foundry's partner/consumption offer; Azure PTU capacity purchasing does not apply.",
    "capacity_unavailable": "Exact PTU capacity for this model and version is unavailable, so dedicated capacity cannot be sized.",
}


def _daily_cost(
    records: list[TraceRecord],
    cost_resolver: CostResolver | None,
    *,
    window_start: datetime | None,
    window_end: datetime | None,
) -> list[PtuDailyCostPoint]:
    """Build daily cost points from the shared pricing engine's own resolutions."""
    if cost_resolver is None:
        return []
    buckets: dict[date, dict[str, float | int | bool]] = {}
    for record in records:
        stamp = _timestamp(record.timestamp)
        if stamp is None:
            continue
        day = stamp.date()
        entry = buckets.setdefault(
            day,
            {"input": 0.0, "cached": 0.0, "output": 0.0, "total": 0.0, "tokens": 0, "priced_tokens": 0, "components": True, "priced": False},
        )
        tokens = record.usage.input_tokens + record.usage.output_tokens
        entry["tokens"] = int(entry["tokens"]) + tokens
        resolution = cost_resolver(record)
        if not resolution.resolved or resolution.cost_usd is None:
            continue
        entry["priced"] = True
        entry["priced_tokens"] = int(entry["priced_tokens"]) + tokens
        entry["total"] = float(entry["total"]) + resolution.cost_usd
        if (
            resolution.fresh_input_cost_usd is None
            or resolution.cached_input_cost_usd is None
            or resolution.output_cost_usd is None
        ):
            entry["components"] = False
            continue
        entry["input"] = float(entry["input"]) + resolution.fresh_input_cost_usd
        entry["cached"] = float(entry["cached"]) + resolution.cached_input_cost_usd
        entry["output"] = float(entry["output"]) + resolution.output_cost_usd
    points: list[PtuDailyCostPoint] = []
    for day in sorted(buckets):
        entry = buckets[day]
        tokens = int(entry["tokens"])
        priced_tokens = int(entry["priced_tokens"])
        has_components = bool(entry["components"]) and bool(entry["priced"])
        points.append(
            PtuDailyCostPoint(
                date=day,
                input_cost=round(float(entry["input"]), 10) if has_components else None,
                cached_input_cost=round(float(entry["cached"]), 10) if has_components else None,
                output_cost=round(float(entry["output"]), 10) if has_components else None,
                total_cost=round(float(entry["total"]), 10) if entry["priced"] else None,
                pricing_coverage_tokens_percent=round(priced_tokens / max(1, tokens) * 100, 1),
                total_tokens=tokens,
                unpriced_tokens=max(0, tokens - priced_tokens),
                partial_day=_is_partial_day(day, window_start, window_end),
            )
        )
    return points


def _is_partial_day(day: date, window_start: datetime | None, window_end: datetime | None) -> bool:
    if window_start is None or window_end is None:
        return True
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    return window_start > day_start or window_end < day_end - timedelta(minutes=BUCKET_MINUTES)


def _dashboard(
    *,
    records: list[TraceRecord],
    deployment: DeploymentAnalysis,
    capacity: _Capacity | None,
    publisher: str | None,
    eligibility_status: EligibilityStatus,
    recommendation: str,
    evidence_series: _Evidence,
    weighted_tpm: list[float],
    active_weighted_tpm: list[float],
    identity_resolved: bool,
    sufficient_evidence: bool,
    secondary_blockers: list[str],
    throughput_series: PtuThroughputSeries | None,
    cost_curve: PtuCostCurve | None,
    suggested_ptu: int | None,
    payg_monthly_usd: float | None,
    hybrid_monthly_usd: float | None,
    spillover_percent: float | None,
    dimensions: list[PtuDimension],
    cost_resolver: CostResolver | None,
) -> PtuDashboardData:
    summary = deployment.summary
    observed_days = evidence_series.observed_days
    active_buckets = evidence_series.active_buckets
    aggregate = summary.aggregate
    table, missing_metrics = _evidence_table(records)
    ordered = sorted(table.items())
    # The collection window, when the source reported one, is what "elapsed"
    # means. Falling back to first/last observation would silently shrink an
    # idle window to the few minutes that happened to carry traffic.
    window_start = (aggregate.window_start if aggregate is not None else None) or (ordered[0][0] if ordered else None)
    window_end = (aggregate.window_end if aggregate is not None else None) or (
        ordered[-1][0] + timedelta(minutes=BUCKET_MINUTES) if ordered else None
    )
    cached_known = any(bucket.cached_known for _, bucket in ordered)
    outcome_known = any(bucket.outcome_known for _, bucket in ordered)
    evidence = [
        PtuEvidencePoint(
            timestamp=stamp,
            input_tokens=bucket.input_tokens,
            cached_tokens=bucket.cached_tokens if bucket.cached_known else None,
            output_tokens=bucket.output_tokens,
            total_tokens=bucket.input_tokens + bucket.output_tokens,
            successful_requests=bucket.successful if bucket.outcome_known else None,
            rate_limited_requests=bucket.rate_limited if bucket.outcome_known else None,
            failed_requests=bucket.failed if bucket.outcome_known else None,
            total_requests=bucket.requests if bucket.requests_known else None,
        )
        for stamp, bucket in ordered
    ]

    total_input = sum(record.usage.input_tokens for record in records)
    total_output = sum(record.usage.output_tokens for record in records)
    total_cached = sum(bucket.cached_tokens for _, bucket in ordered) if cached_known else None
    total_tokens = total_input + total_output
    requests_known = any(bucket.requests_known for _, bucket in ordered)
    total_requests = sum(bucket.requests for _, bucket in ordered) if requests_known else None
    rate_limited = sum(bucket.rate_limited for _, bucket in ordered) if outcome_known else None
    # The 429 rate needs both a numerator and a denominator from the *same*
    # buckets. A source that reported throttled counts without request totals
    # gives a numerator with nothing to divide by, and a partial denominator
    # would produce a rate above 100%. Either way the rate is withheld and the
    # missing metric is named, rather than presenting an impossible percentage.
    denominator = sum(
        bucket.requests for _, bucket in ordered if bucket.outcome_known and bucket.requests_known
    )
    rate_limit_percent: float | None = None
    if rate_limited is not None and denominator > 0 and rate_limited <= denominator:
        rate_limit_percent = round(min(100.0, rate_limited / denominator * 100), 2)
    elif rate_limited is not None:
        missing_metrics.add("request_totals")

    daily_totals: dict[date, int] = defaultdict(int)
    for stamp, bucket in ordered:
        daily_totals[stamp.date()] += bucket.input_tokens + bucket.output_tokens
    partial_days = sum(_is_partial_day(day, window_start, window_end) for day in daily_totals)
    complete_days = len(daily_totals) - partial_days
    daily_average_tokens = round(sum(daily_totals.values()) / len(daily_totals), 1) if daily_totals else None

    # Elapsed is the window, observed is what the source returned, active is
    # where traffic happened. They are reported separately, never merged.
    elapsed_buckets = max(evidence_series.elapsed_buckets, len(weighted_tpm))
    observed_buckets = evidence_series.observed_buckets
    weighted_basis = (
        f"input + output x {capacity.output_ratio}"
        if capacity
        else "input + output (model output weighting unavailable)"
    )
    # The elapsed-window average divides by every interval in the collection
    # window. Dividing by the handful of intervals that happened to carry
    # traffic would overstate sustained demand by orders of magnitude.
    elapsed_weighted = weighted_tpm + [0.0] * max(0, elapsed_buckets - len(weighted_tpm))
    average_weighted = round(mean(elapsed_weighted), 4) if elapsed_weighted else None
    p95_weighted = round(_percentile(elapsed_weighted, 95), 4) if elapsed_weighted else None
    active_average_weighted = round(mean(active_weighted_tpm), 4) if active_weighted_tpm else None
    active_p95_weighted = round(_percentile(active_weighted_tpm, 95), 4) if active_weighted_tpm else None
    busy_hour_available = len(active_weighted_tpm) >= MINIMUM_BUSY_HOUR_BUCKETS

    latency_coverage = (
        sum(record.latency_ms is not None for record in records) / len(records) if records else 0.0
    )
    outcome_coverage = (
        sum(1 for _, bucket in ordered if bucket.outcome_known) / len(ordered) if ordered else 0.0
    )
    mode_known = summary.deployment_mode.casefold() in {"global", "regional"}
    confidence_percent, confidence_components = _confidence(
        active_buckets=active_buckets,
        observed_buckets=observed_buckets,
        elapsed_buckets=elapsed_buckets,
        observed_days=observed_days,
        outcome_coverage=outcome_coverage,
        latency_coverage=latency_coverage,
        pricing_coverage=summary.pricing_coverage_tokens_percent / 100,
        capacity_known=capacity is not None,
        mode_known=mode_known,
        identity_known=identity_resolved,
    )
    state = _dashboard_state(
        eligibility_status=eligibility_status,
        recommendation=recommendation,
        publisher=publisher,
        identity_resolved=identity_resolved,
        sufficient_evidence=sufficient_evidence,
    )
    show_confidence = state in STATES_WITH_CONFIDENCE
    daily_cost = _daily_cost(records, cost_resolver, window_start=window_start, window_end=window_end)
    priced_days = [point for point in daily_cost if point.total_cost is not None]
    total_cost = round(sum(point.total_cost or 0 for point in priced_days), 10) if priced_days else None
    average_daily_cost = round(total_cost / len(priced_days), 10) if total_cost is not None and priced_days else None

    if not cached_known:
        missing_metrics.add("cached_tokens")
    if not outcome_known:
        missing_metrics.add("request_outcomes")
    if latency_coverage == 0:
        missing_metrics.add("latency")
    if summary.pricing_coverage_tokens_percent < 100:
        missing_metrics.add("pricing")

    smoke_test = active_buckets < 5 and (total_requests or len(records)) <= 10
    outcome_state = (
        aggregate.outcome_coverage
        if aggregate is not None
        else ("complete" if outcome_known and outcome_coverage == 1 else "partial" if outcome_known else "unavailable")
    )
    successful = sum(bucket.successful for _, bucket in ordered) if outcome_known else None
    other_failed = sum(bucket.failed for _, bucket in ordered) if outcome_known else None
    active_days = len({stamp.date() for stamp, bucket in ordered if bucket.input_tokens or bucket.output_tokens or bucket.requests})
    dashboard_summary = PtuDashboardSummary(
        deployment_name=summary.deployment_name,
        model_name=summary.model_name,
        model_version=str((records[0].metadata or {}).get("model_version")) if records and (records[0].metadata or {}).get("model_version") else None,
        deployment_mode=summary.deployment_mode,
        state=state,
        state_label=DASHBOARD_STATE_LABELS[state],
        recommendation=DASHBOARD_STATE_LABELS[state],
        secondary_blockers=list(secondary_blockers),
        confidence_percent=confidence_percent if show_confidence else None,
        confidence_label=_confidence_label(confidence_percent) if show_confidence else None,
        summary=_STATE_SUMMARIES[state],
        average_weighted_tpm=average_weighted,
        p95_weighted_tpm=p95_weighted,
        active_average_weighted_tpm=active_average_weighted,
        active_p95_weighted_tpm=active_p95_weighted,
        busy_hour_available=busy_hour_available,
        weighted_basis=weighted_basis,
        total_input_tokens=total_input or None if records else None,
        total_cached_tokens=total_cached,
        total_output_tokens=total_output or None if records else None,
        total_tokens=total_tokens or None if records else None,
        daily_average_tokens=daily_average_tokens,
        rate_limited_requests=rate_limited,
        rate_limit_percent=rate_limit_percent,
        successful_requests=successful,
        other_failed_requests=other_failed,
        outcome_coverage=outcome_state,
        total_requests=total_requests,
        requests_available=total_requests is not None,
        retries_available=aggregate is None,
        observed_days=round(observed_days, 2),
        active_days=active_days,
        complete_days=max(0, complete_days),
        partial_days=partial_days,
        window_start=window_start,
        window_end=window_end,
        active_buckets=active_buckets,
        observed_buckets=observed_buckets,
        elapsed_buckets=elapsed_buckets,
        collection_completeness_percent=(
            round(min(100.0, observed_buckets / elapsed_buckets * 100), 1) if elapsed_buckets else 0.0
        ),
        bucket_minutes=aggregate.bucket_minutes if aggregate is not None else BUCKET_MINUTES,
        identity_resolved=identity_resolved,
        pricing_status=summary.pricing_status,
        pricing_coverage_tokens_percent=summary.pricing_coverage_tokens_percent,
        pricing_currency=summary.pricing_currency,
        total_cost=total_cost,
        average_daily_cost=average_daily_cost,
        data_quality=_confidence_label(confidence_percent),
        smoke_test_window=smoke_test,
    )
    return PtuDashboardData(
        summary=dashboard_summary,
        evidence=evidence,
        daily_cost=daily_cost,
        throughput=throughput_series,
        cost_curve=cost_curve,
        confidence_components=confidence_components,
        assumptions=_assumptions(
            summary=dashboard_summary,
            capacity=capacity,
            suggested_ptu=suggested_ptu,
            cost_curve=cost_curve,
            deployment=deployment,
        ),
        missing_metrics=sorted(missing_metrics),
        recommendation_reasons=_reasons(
            state=state,
            dimensions=dimensions,
            payg_monthly_usd=payg_monthly_usd,
            hybrid_monthly_usd=hybrid_monthly_usd,
            spillover_percent=spillover_percent,
            summary=dashboard_summary,
        ),
        what_would_change=_what_would_change(state=state, summary=dashboard_summary, capacity=capacity),
        next_steps=_next_steps(state=state, summary=dashboard_summary, suggested_ptu=suggested_ptu),
        data_quality_notes=_data_quality_notes(
            summary=dashboard_summary,
            missing=sorted(missing_metrics),
        ),
    )


def _money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "unavailable"
    return f"{currency} {value:,.2f}"


def _assumptions(
    *,
    summary: PtuDashboardSummary,
    capacity: _Capacity | None,
    suggested_ptu: int | None,
    cost_curve: PtuCostCurve | None,
    deployment: DeploymentAnalysis,
) -> list[str]:
    items = [
        f"Throughput is aggregated into {BUCKET_MINUTES}-minute buckets from the observed telemetry window.",
        f"Weighted TPM is calculated as {summary.weighted_basis}.",
        "Monthly figures are 30-day (720-hour) run-rate estimates, not invoices.",
    ]
    if capacity is not None:
        items.append(
            f"PTU sizing uses {capacity.input_tpm_per_ptu:,} input TPM per PTU and a {capacity.output_ratio}x output weighting."
        )
        items.append(
            f"Capacity for this exact model comes from {capacity.source}; no related model's capacity is substituted."
        )
    if suggested_ptu is not None:
        items.append(f"Reserved pricing assumes a one-year commitment discount applied to {suggested_ptu:,} PTU.")
    if cost_curve is not None:
        items.append("The cost explorer models a constant sustained TPM; real traffic is peakier than the plotted line.")
    price = deployment.summary
    if price.input_price_per_million is not None and price.output_price_per_million is not None:
        items.append(
            f"Prices come from {price.pricing_catalog_name or price.pricing_source} "
            f"({price.pricing_currency} {price.input_price_per_million:,.2f} input / "
            f"{price.output_price_per_million:,.2f} output per 1M tokens)."
        )
    if summary.partial_days:
        items.append(f"{summary.partial_days} observed day(s) are partial, so daily averages are marked partial.")
    return items


def _reasons(
    *,
    state: DashboardState,
    dimensions: list[PtuDimension],
    payg_monthly_usd: float | None,
    hybrid_monthly_usd: float | None,
    spillover_percent: float | None,
    summary: PtuDashboardSummary,
) -> list[str]:
    reasons = [f"{item.name}: {item.verdict} — {item.summary} ({item.metric})" for item in dimensions]
    if payg_monthly_usd is not None and hybrid_monthly_usd is not None:
        difference = payg_monthly_usd - hybrid_monthly_usd
        direction = "cheaper" if difference > 0 else "more expensive"
        reasons.append(
            f"Modeled economics: PAYG {_money(payg_monthly_usd, summary.pricing_currency)} per month versus "
            f"PTU + spillover {_money(hybrid_monthly_usd, summary.pricing_currency)} per month "
            f"({_money(abs(difference), summary.pricing_currency)} {direction} on PTU)."
        )
    if spillover_percent is not None:
        reasons.append(f"{spillover_percent:.1f}% of observed buckets would spill over the selected PTU capacity.")
    if state == "insufficient_evidence":
        reasons.append(
            f"Only {summary.active_buckets:,} active five-minute buckets were observed; "
            f"{MINIMUM_ACTIVE_BUCKETS} are required before a recommendation is issued."
        )
    return reasons


def _what_would_change(*, state: DashboardState, summary: PtuDashboardSummary, capacity: _Capacity | None) -> list[str]:
    items: list[str] = []
    if state in {"ptu_recommended", "borderline", "payg_recommended"}:
        items.append("Sustained throughput moving above or below the plotted break-even changes the economic result.")
        items.append("A materially different peak-to-average ratio changes the PTU size and the spillover share.")
    if state == "insufficient_evidence":
        items.append(
            f"Collecting at least {MINIMUM_ACTIVE_BUCKETS} active five-minute buckets across a representative window "
            "enables sizing and a cost curve."
        )
    if state == "pricing_unavailable":
        items.append("Supplying exact PAYG prices for this model and deployment mode enables break-even and hybrid cost.")
    if state == "capacity_unavailable":
        items.append("Publishing or configuring exact PTU capacity for this model and version enables sizing.")
    if state == "ptu_not_applicable":
        items.append("If this model later gains an Azure PTU offer, the same evidence can be re-scored.")
    if summary.rate_limit_percent is None:
        items.append("Request-outcome telemetry would confirm whether PAYG quota pressure is present.")
    elif summary.rate_limit_percent > 1:
        items.append("Reducing HTTP 429 pressure through quota increases would weaken the capacity-pressure signal.")
    if capacity is not None and summary.deployment_mode.casefold() not in {"global", "regional"}:
        items.append("Recording the deployment mode as Global or Regional enables sizing and pricing.")
    return items


def _next_steps(*, state: DashboardState, summary: PtuDashboardSummary, suggested_ptu: int | None) -> list[str]:
    if state == "ptu_recommended" and suggested_ptu is not None:
        return [
            f"Validate {suggested_ptu:,} PTU against a production-representative load test before committing.",
            "Confirm regional capacity availability and quota for the selected deployment mode.",
            "Plan PAYG spillover for peaks above the reserved capacity.",
        ]
    if state == "borderline":
        return [
            "Collect a longer window that includes at least one full business cycle.",
            "Compare the modeled PAYG and PTU + spillover run rates against your actual invoice.",
            "Re-run this report before committing to a reservation.",
        ]
    if state == "payg_recommended":
        return [
            "Stay on pay-as-you-go and re-evaluate when sustained throughput grows.",
            "Track HTTP 429 pressure as an early signal that dedicated capacity is needed.",
        ]
    if state == "pricing_unavailable":
        return [
            "Add an exact customer price for this model and deployment mode in `.tokenlens.yml`.",
            "Re-run the report to unlock break-even and hybrid economics.",
        ]
    if state == "capacity_unavailable":
        return ["Confirm the exact model version and its PTU capacity with Azure before sizing."]
    if state == "ptu_not_applicable":
        return ["Use the applicable consumption or marketplace purchasing model for this publisher."]
    return [
        "Instrument the application once with the TokenLens SDK wrapper, or collect Azure Monitor metrics.",
        f"Collect at least {MINIMUM_ACTIVE_BUCKETS} active five-minute buckets across a representative window.",
        "Re-run the report once a representative window is available.",
    ]


def _data_quality_notes(*, summary: PtuDashboardSummary, missing: list[str]) -> list[str]:
    labels = {
        "cached_tokens": "Cached-token metric unavailable for this source.",
        "request_outcomes": "Request-outcome metric unavailable; success, 429, and failure counts cannot be shown.",
        "request_totals": (
            "Request-count metric unavailable for some buckets, so the HTTP 429 rate is withheld: its "
            "denominator is incomplete. The observed 429 count is still shown."
        ),
        "failed_requests": "Other-failure counts are unavailable; success is never derived as total minus 429.",
        "latency": "No latency evidence was supplied for this deployment.",
        "pricing": f"Pricing covers {summary.pricing_coverage_tokens_percent:.1f}% of observed tokens.",
        "timestamp": "Some events had no usable timestamp and were excluded from the time series.",
    }
    notes = [labels[item] for item in missing if item in labels]
    if summary.smoke_test_window:
        notes.append("This window looks like a manual smoke test rather than representative production traffic.")
    if summary.partial_days:
        notes.append(f"{summary.partial_days} of {summary.complete_days + summary.partial_days} observed day(s) are partial.")
    return notes


def _assessment(records: list[TraceRecord], deployment: DeploymentAnalysis, cost_resolver: CostResolver | None = None) -> PtuDeploymentAssessment:
    summary = deployment.summary
    aggregate = summary.aggregate
    evidence = _series(records, aggregate=aggregate)
    input_tpm, output_tpm = evidence.input_tpm, evidence.output_tpm
    filtered_records = evidence.records
    observed_days = evidence.observed_days
    active_buckets = evidence.active_buckets
    tpm = evidence.tpm
    if evidence.elapsed_buckets > len(tpm):
        tpm = tpm + [0.0] * (evidence.elapsed_buckets - len(tpm))
    capacity = _capacity_for(summary.model_name)
    publisher = infer_publisher(summary.model_name)
    identity_resolved = summary.model_name.strip().casefold() not in {"", "unknown", "none"}
    outcomes = {
        "total_requests": aggregate.requests_observed if aggregate is not None else None,
        "rate_limited_requests": aggregate.rate_limited_requests if aggregate is not None else None,
    }
    dimensions, metrics = _dimensions(
        filtered_records,
        tpm,
        active_buckets,
        capacity,
        outcomes=outcomes if aggregate is not None else None,
    )
    recommendation = _recommendation(dimensions, capacity is not None, publisher)
    average_tpm = mean(tpm) if tpm else 0
    p95_tpm = _percentile(tpm, 95)
    total_tokens = sum(record.usage.input_tokens + record.usage.output_tokens for record in filtered_records)
    completion_ratio = sum(record.usage.output_tokens for record in filtered_records) / max(1, total_tokens)

    suggested_ptu = None
    ptu_capacity_tpm = None
    hourly_monthly = None
    reserved_monthly = None
    payg_monthly = None
    hybrid_monthly = None
    break_even = None
    spillover_percent = None
    throughput_series = None
    cost_curve = None
    economic_result: Literal["PTU lower", "PAYG lower", "Unavailable"] = "Unavailable"
    note = "Exact supported model capacity is required for PTU sizing."

    mode = summary.deployment_mode.casefold()
    # Sufficiency is measured in *active* buckets. A window padded with silent
    # intervals is not evidence of sustained demand.
    sufficient_evidence = active_buckets >= MINIMUM_ACTIVE_BUCKETS
    secondary_blockers: list[str] = []
    if not identity_resolved:
        eligibility_status: EligibilityStatus = "collection_identity_error"
    elif capacity is None:
        eligibility_status = "ptu_not_applicable" if publisher and publisher != "microsoft" else "model_capacity_unavailable"
    elif mode not in {"global", "regional"}:
        eligibility_status = "deployment_mode_unavailable"
    elif not sufficient_evidence:
        eligibility_status = "eligible_insufficient_evidence"
    else:
        eligibility_status = "eligible_sufficient_evidence"  # may be downgraded to pricing_unavailable below
    # Capacity and pricing remain real blockers, but they are badges behind the
    # primary state rather than a headline that hides the actual problem.
    if capacity is None and identity_resolved:
        secondary_blockers.append("Capacity data required")
    if mode not in {"global", "regional"}:
        secondary_blockers.append("Deployment mode unconfirmed")
    if summary.pricing_status != "priced":
        secondary_blockers.append("Pricing required")

    if capacity and mode in {"global", "regional"}:
        regional = mode == "regional"
        minimum = capacity.regional_min_ptu if regional else capacity.global_min_ptu
        increment = capacity.regional_increment if regional else capacity.global_increment
        hourly_rate = 2.0 if regional else 1.0
        # Economics must use the same elapsed-window basis as the dashboard
        # headline. Otherwise a short burst is incorrectly annualised as a
        # sustained month-long workload.
        weighted_tpm = evidence.weighted(capacity.output_ratio)
        if evidence.elapsed_buckets > len(weighted_tpm):
            weighted_tpm = weighted_tpm + [0.0] * (evidence.elapsed_buckets - len(weighted_tpm))
        raw_ptu = _percentile(weighted_tpm, 95) * 1.2 / capacity.input_tpm_per_ptu
        p95_sized_ptu = max(minimum, ceil(raw_ptu / increment) * increment)
        effective_capacity_per_ptu = capacity.input_tpm_per_ptu

        cached_rate = summary.cached_input_price_per_million
        cached_tokens = summary.cached_tokens
        fresh_tokens = max(0, summary.input_tokens - cached_tokens)
        pricing_ready = (
            # Economics are a recommendation too: they are withheld until the
            # active sample can support one.
            sufficient_evidence
            and summary.input_price_per_million is not None
            and summary.output_price_per_million is not None
            and (not cached_tokens or cached_rate is not None)
            and total_tokens
            and summary.pricing_currency == "USD"
            and summary.pricing_complete
            and bool(filtered_records)
            and all(record.observed_cost_usd is None for record in filtered_records)
        )
        if pricing_ready:
            blended = (
                sum(max(0, record.usage.input_tokens - record.usage.cached_tokens) for record in filtered_records) * summary.input_price_per_million
                + sum(record.usage.cached_tokens for record in filtered_records) * (cached_rate or 0)
                + sum(record.usage.output_tokens for record in filtered_records) * summary.output_price_per_million
            ) / max(1, total_tokens)
            payg_monthly = mean(tpm) * 43_200 / 1_000_000 * blended
            peak_raw_ptu = max(weighted_tpm, default=0) * 1.2 / capacity.input_tpm_per_ptu
            search_upper_ptu = max(
                p95_sized_ptu,
                minimum,
                ceil(peak_raw_ptu / increment) * increment,
            )
            best_candidate = None
            for candidate_ptu in range(minimum, search_upper_ptu + increment, increment):
                candidate_capacity = candidate_ptu * effective_capacity_per_ptu
                candidate_reserved = candidate_ptu * hourly_rate * 720 * 0.35
                overflow_total = sum(max(0.0, value - candidate_capacity) for value in weighted_tpm)
                overflow_buckets = sum(value > candidate_capacity for value in weighted_tpm)
                average_overflow = overflow_total / len(weighted_tpm) if weighted_tpm else 0
                candidate_hybrid = (
                    candidate_reserved
                    + average_overflow * 43_200 / 1_000_000 * blended
                )
                candidate = (
                    candidate_hybrid,
                    candidate_ptu,
                    candidate_capacity,
                    candidate_reserved,
                    overflow_buckets,
                )
                if best_candidate is None or candidate[:2] < best_candidate[:2]:
                    best_candidate = candidate
            assert best_candidate is not None
            hybrid_monthly, suggested_ptu, ptu_capacity_tpm, reserved_monthly, overflow_buckets = best_candidate
            hourly_monthly = suggested_ptu * hourly_rate * 720
            spillover_percent = overflow_buckets / len(weighted_tpm) * 100 if weighted_tpm else 0
            candidate = reserved_monthly * 1_000_000 / (43_200 * blended) if blended > 0 else None
            break_even = candidate if candidate is not None and candidate <= ptu_capacity_tpm else None
            economic_result = "PTU lower" if hybrid_monthly < payg_monthly else "PAYG lower"
            note = "Monthly values are 30-day run-rate estimates from the observed five-minute distribution."
            if recommendation == "PTU recommended" and economic_result == "PAYG lower":
                recommendation = "Borderline"
                note = "Operational signals favor PTU, but the observed PAYG run rate is lower; validate the non-cost value before committing."
            if eligibility_status == "eligible_sufficient_evidence":
                throughput_series = _throughput_series(tpm, bucket_minutes=5, ptu_capacity_tpm=ptu_capacity_tpm)
                cost_curve = _cost_curve(
                    weighted_tpm=weighted_tpm,
                    average_tpm=average_tpm,
                    selected_ptu=suggested_ptu,
                    capacity_tpm=ptu_capacity_tpm,
                    reserved_monthly=reserved_monthly,
                    blended_rate=blended,
                    payg_monthly_usd=payg_monthly,
                    hybrid_monthly_usd=hybrid_monthly,
                )
        else:
            # A PTU amount is a recommendation. It is withheld until the active
            # sample is large enough to size against.
            suggested_ptu = p95_sized_ptu if filtered_records and sufficient_evidence else None
            if suggested_ptu is not None:
                ptu_capacity_tpm = suggested_ptu * effective_capacity_per_ptu
                hourly_monthly = suggested_ptu * hourly_rate * 720
                reserved_monthly = hourly_monthly * 0.35
            note = "Timestamped request evidence is required for PTU economics." if not filtered_records else "Workload fit is available; USD PAYG model pricing is required for break-even and hybrid cost."
            if eligibility_status == "eligible_sufficient_evidence":
                eligibility_status = "pricing_unavailable"
                if suggested_ptu is not None:
                    throughput_series = _throughput_series(tpm, bucket_minutes=5, ptu_capacity_tpm=ptu_capacity_tpm)
    elif capacity:
        note = "Workload fit is available; deployment mode must be Global or Regional before PTU sizing and economics."

    if eligibility_status == "collection_identity_error":
        note = (
            "Collection did not resolve this deployment's exact model, so capacity, pricing, and workload "
            "conclusions are withheld. Re-run collection with an explicit deployment so the model dimension or "
            "deployment inventory can supply the exact model and version."
        )
    elif eligibility_status == "ptu_not_applicable":
        note = (
            f"{publisher.title() if publisher else 'This publisher'}'s models are billed through Foundry's "
            "consumption/marketplace offer; Azure PTU capacity purchasing does not apply to this model."
        )
    elif eligibility_status == "eligible_insufficient_evidence" or not sufficient_evidence:
        note = (
            f"{active_buckets} of the required {MINIMUM_ACTIVE_BUCKETS} active five-minute buckets were observed "
            f"across {evidence.elapsed_buckets:,} elapsed buckets; TokenLens shows the observed workload evidence "
            "but withholds a PTU recommendation and cost curve until there is enough sustained activity to size "
            "and cost dedicated capacity."
        )

    # Secondary blockers are appended to the note so the primary state stays the
    # real blocker while nothing that also blocks a decision is hidden.
    secondary_notes: list[str] = []
    if capacity is None and identity_resolved and "PTU capacity" not in note:
        secondary_notes.append("Exact PTU capacity for this model and version is unavailable, so sizing stays blocked.")
    if mode not in {"global", "regional"} and "deployment mode" not in note:
        secondary_notes.append("The deployment mode must be Global or Regional before PTU sizing and economics.")
    if summary.pricing_status != "priced" and "pricing" not in note.casefold():
        secondary_notes.append("Exact pricing is required before break-even and hybrid economics can be shown.")
    if secondary_notes:
        note = " ".join([note, *secondary_notes])

    dashboard = _dashboard(
        records=filtered_records,
        deployment=deployment,
        capacity=capacity,
        publisher=publisher,
        eligibility_status=eligibility_status,
        recommendation=recommendation,
        evidence_series=evidence,
        weighted_tpm=evidence.weighted(capacity.output_ratio if capacity else None),
        active_weighted_tpm=evidence.active_weighted(capacity.output_ratio if capacity else None),
        identity_resolved=identity_resolved,
        sufficient_evidence=sufficient_evidence,
        secondary_blockers=secondary_blockers,
        throughput_series=throughput_series,
        cost_curve=cost_curve,
        suggested_ptu=suggested_ptu,
        payg_monthly_usd=payg_monthly,
        hybrid_monthly_usd=hybrid_monthly,
        spillover_percent=spillover_percent,
        dimensions=dimensions,
        cost_resolver=cost_resolver,
    )

    return PtuDeploymentAssessment(
        deployment_name=summary.deployment_name,
        model_name=summary.model_name,
        deployment_mode=summary.deployment_mode,
        eligible=capacity is not None,
        eligibility_status=eligibility_status,
        recommendation=recommendation,
        economic_result=economic_result,
        data_points=len(tpm),
        observed_buckets=active_buckets,
        observed_days=round(observed_days, 2),
        average_tpm=round(average_tpm, 2),
        p95_tpm=round(p95_tpm, 2),
        throttling_rate_percent=round(float(metrics["throttle_rate"] or 0) * 100, 2),
        active_buckets=active_buckets,
        elapsed_buckets=evidence.elapsed_buckets,
        identity_resolved=identity_resolved,
        p50_latency_ms=metrics["p50_latency"],
        p95_latency_ms=metrics["p95_latency"],
        p99_latency_ms=metrics["p99_latency"],
        suggested_ptu=suggested_ptu,
        ptu_capacity_tpm=ptu_capacity_tpm,
        payg_monthly_usd=payg_monthly,
        ptu_hourly_monthly_usd=hourly_monthly,
        ptu_reserved_monthly_usd=reserved_monthly,
        hybrid_monthly_usd=hybrid_monthly,
        break_even_tpm=break_even,
        spillover_percent=spillover_percent,
        dimensions=dimensions,
        throughput_series=throughput_series,
        cost_curve=cost_curve,
        dashboard=dashboard,
        note=note,
    )


def analyze_ptu(
    records: list[TraceRecord],
    deployments: list[DeploymentAnalysis],
    *,
    cost_resolver: CostResolver | None = None,
) -> PtuPortfolioAssessment:
    grouped: dict[tuple[str, str, str, str, str], list[TraceRecord]] = defaultdict(list)
    for record in records:
        grouped[
            (
                record.resource_name or "",
                record.project_name or "",
                record.deployment_name,
                record.model_name,
                record.deployment_mode,
            )
        ].append(record)
    assessments = [
        _assessment(
            grouped.get(
                (
                    deployment.summary.resource_name or "",
                    deployment.summary.project_name or "",
                    deployment.summary.deployment_name,
                    deployment.summary.model_name,
                    deployment.summary.deployment_mode,
                ),
                [],
            ),
            deployment,
            cost_resolver,
        )
        for deployment in deployments
    ]
    return PtuPortfolioAssessment(
        recommended_deployments=sum(item.recommendation == "PTU recommended" for item in assessments),
        borderline_deployments=sum(item.recommendation == "Borderline" for item in assessments),
        payg_deployments=sum(item.recommendation == "PAYG recommended" for item in assessments),
        insufficient_deployments=sum(
            item.recommendation in {"Insufficient evidence", "Model not supported", "PTU not applicable"}
            for item in assessments
        ),
        deployments=assessments,
    )
