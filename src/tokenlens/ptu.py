"""Offline PTU suitability and cost analysis.

The decision shape, thresholds, capacity table, and cost formulas are adapted
from the MIT-licensed https://github.com/msftse-org/ptu-advisor project at
revision eb0558cd4c6d3794be76d9caa2e87129d1f8221c.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from math import ceil, sqrt
from statistics import mean, pstdev
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import DeploymentAnalysis, TraceRecord
from .pricing import canonical_model_name


class PtuDimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    verdict: Literal["positive", "neutral", "negative", "insufficient"]
    summary: str
    metric: str


class PtuDeploymentAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_name: str
    model_name: str
    deployment_mode: str
    eligible: bool
    recommendation: Literal[
        "PTU recommended",
        "Borderline",
        "PAYG recommended",
        "Insufficient evidence",
        "Model not supported",
    ]
    economic_result: Literal["PTU lower", "PAYG lower", "Unavailable"]
    data_points: int = Field(ge=0)
    observed_buckets: int = Field(ge=0)
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


def _series(records: list[TraceRecord], bucket_minutes: int = 5) -> tuple[list[float], list[float], list[TraceRecord], float, int]:
    parsed = [(stamp, record) for record in records if (stamp := _timestamp(record.timestamp)) is not None]
    if not parsed:
        return [], [], [], 0.0, 0
    parsed.sort(key=lambda item: item[0])
    end = parsed[-1][0]
    start_limit = end - timedelta(days=90)
    parsed = [item for item in parsed if item[0] >= start_limit]
    bucket_seconds = bucket_minutes * 60
    buckets: dict[int, tuple[float, float]] = defaultdict(lambda: (0.0, 0.0))
    for stamp, record in parsed:
        key = int(stamp.timestamp()) // bucket_seconds
        input_tokens, output_tokens = buckets[key]
        buckets[key] = (input_tokens + record.usage.input_tokens, output_tokens + record.usage.output_tokens)
    first, last = min(buckets), max(buckets)
    inputs = [buckets.get(key, (0.0, 0.0))[0] / bucket_minutes for key in range(first, last + 1)]
    outputs = [buckets.get(key, (0.0, 0.0))[1] / bucket_minutes for key in range(first, last + 1)]
    observed_days = max(bucket_minutes / 1440, (last - first + 1) * bucket_minutes / 1440)
    return inputs, outputs, [record for _, record in parsed], observed_days, len(buckets)


def _dimension(name: str, verdict: str, summary: str, metric: str) -> PtuDimension:
    return PtuDimension(name=name, verdict=verdict, summary=summary, metric=metric)


def _dimensions(
    records: list[TraceRecord],
    tpm: list[float],
    observed_buckets: int,
    capacity: _Capacity | None,
) -> tuple[list[PtuDimension], dict[str, float | None]]:
    if observed_buckets < 100:
        metric = f"{observed_buckets} observed · {len(tpm)} elapsed buckets"
        workload = _dimension("Workload shape", "insufficient", "Need activity in at least 100 five-minute buckets.", metric)
        predictability = _dimension("Load predictability", "insufficient", "Need activity in at least 100 five-minute buckets.", metric)
    else:
        average = mean(tpm)
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

    total_requests = len(records)
    throttled = sum(record.status_code == 429 for record in records)
    throttle_rate = throttled / total_requests if total_requests else 0
    p95_tpm = _percentile(tpm, 95)
    utilization = p95_tpm / capacity.payg_tpm_quota if capacity and capacity.payg_tpm_quota else 0
    if throttle_rate > 0.05 or utilization > 0.85:
        pressure = _dimension("Capacity pressure", "positive", "PAYG capacity pressure favors dedicated throughput.", f"{throttle_rate:.1%} throttled · {utilization:.1%} quota")
    elif throttle_rate < 0.01 and utilization < 0.5:
        pressure = _dimension("Capacity pressure", "negative", "PAYG is handling observed demand.", f"{throttle_rate:.1%} throttled · {utilization:.1%} quota")
    else:
        pressure = _dimension("Capacity pressure", "neutral", "Some capacity pressure is present.", f"{throttle_rate:.1%} throttled · {utilization:.1%} quota")

    latencies = [record.latency_ms for record in records if record.latency_ms is not None]
    p50_latency = _percentile(latencies, 50) if latencies else None
    p95_latency = _percentile(latencies, 95) if latencies else None
    p99_latency = _percentile(latencies, 99) if latencies else None
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
    return [workload, pressure, latency, predictability], {
        "throttle_rate": throttle_rate,
        "p50_latency": p50_latency,
        "p95_latency": p95_latency,
        "p99_latency": p99_latency,
    }


def _recommendation(dimensions: list[PtuDimension], eligible: bool) -> str:
    if not eligible:
        return "Model not supported"
    if sum(item.verdict == "insufficient" for item in dimensions) >= 2:
        return "Insufficient evidence"
    positives = sum(item.verdict == "positive" for item in dimensions)
    if positives >= 3:
        return "PTU recommended"
    if positives >= 2:
        return "Borderline"
    return "PAYG recommended"


def _assessment(records: list[TraceRecord], deployment: DeploymentAnalysis) -> PtuDeploymentAssessment:
    summary = deployment.summary
    input_tpm, output_tpm, filtered_records, observed_days, observed_buckets = _series(records)
    tpm = [input_value + output_value for input_value, output_value in zip(input_tpm, output_tpm)]
    capacity = _capacity_for(summary.model_name)
    dimensions, metrics = _dimensions(filtered_records, tpm, observed_buckets, capacity)
    recommendation = _recommendation(dimensions, capacity is not None)
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
    economic_result: Literal["PTU lower", "PAYG lower", "Unavailable"] = "Unavailable"
    note = "Exact supported model capacity is required for PTU sizing."

    mode = summary.deployment_mode.casefold()
    if capacity and mode in {"global", "regional"}:
        regional = mode == "regional"
        minimum = capacity.regional_min_ptu if regional else capacity.global_min_ptu
        increment = capacity.regional_increment if regional else capacity.global_increment
        hourly_rate = 2.0 if regional else 1.0
        weighted_tpm = [input_value + output_value * capacity.output_ratio for input_value, output_value in zip(input_tpm, output_tpm)]
        raw_ptu = _percentile(weighted_tpm, 95) * 1.2 / capacity.input_tpm_per_ptu
        p95_sized_ptu = max(minimum, ceil(raw_ptu / increment) * increment)
        effective_capacity_per_ptu = capacity.input_tpm_per_ptu

        cached_rate = summary.cached_input_price_per_million
        cached_tokens = summary.cached_tokens
        fresh_tokens = max(0, summary.input_tokens - cached_tokens)
        if (
            summary.input_price_per_million is not None
            and summary.output_price_per_million is not None
            and (not cached_tokens or cached_rate is not None)
            and total_tokens
            and summary.pricing_currency == "USD"
            and summary.pricing_complete
            and filtered_records
            and all(record.observed_cost_usd is None for record in filtered_records)
        ):
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
        else:
            suggested_ptu = p95_sized_ptu if filtered_records else None
            if suggested_ptu is not None:
                ptu_capacity_tpm = suggested_ptu * effective_capacity_per_ptu
                hourly_monthly = suggested_ptu * hourly_rate * 720
                reserved_monthly = hourly_monthly * 0.35
            note = "Timestamped request evidence is required for PTU economics." if not filtered_records else "Workload fit is available; USD PAYG model pricing is required for break-even and hybrid cost."
    elif capacity:
        note = "Workload fit is available; deployment mode must be Global or Regional before PTU sizing and economics."

    return PtuDeploymentAssessment(
        deployment_name=summary.deployment_name,
        model_name=summary.model_name,
        deployment_mode=summary.deployment_mode,
        eligible=capacity is not None,
        recommendation=recommendation,
        economic_result=economic_result,
        data_points=len(tpm),
        observed_buckets=observed_buckets,
        observed_days=round(observed_days, 2),
        average_tpm=round(average_tpm, 2),
        p95_tpm=round(p95_tpm, 2),
        throttling_rate_percent=round(float(metrics["throttle_rate"] or 0) * 100, 2),
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
        note=note,
    )


def analyze_ptu(records: list[TraceRecord], deployments: list[DeploymentAnalysis]) -> PtuPortfolioAssessment:
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
        )
        for deployment in deployments
    ]
    return PtuPortfolioAssessment(
        recommended_deployments=sum(item.recommendation == "PTU recommended" for item in assessments),
        borderline_deployments=sum(item.recommendation == "Borderline" for item in assessments),
        payg_deployments=sum(item.recommendation == "PAYG recommended" for item in assessments),
        insufficient_deployments=sum(
            item.recommendation in {"Insufficient evidence", "Model not supported"}
            for item in assessments
        ),
        deployments=assessments,
    )
