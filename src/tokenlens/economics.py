"""Task-level economics calculations and non-overlapping scenario reporting."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime
from statistics import median
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .tasks import TaskTrajectory


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * percentile
    lower, upper = int(position), min(len(values) - 1, int(position) + 1)
    fraction = position - lower
    return values[lower] + (values[upper] - values[lower]) * fraction


class Distribution(BaseModel):
    p50: float | None = None
    p90: float | None = None


class PricingCoverage(BaseModel):
    resolved_trajectories: int = 0
    unresolved_trajectories: int = 0
    resolved_billable_events: int = 0
    unresolved_billable_events: int = 0
    observed_events: int = 0
    customer_events: int = 0
    reference_events: int = 0
    coverage_percent: float = 0


class TaskCohort(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: str
    maturity_level: int
    maturity_label: str
    attempted_tasks: int
    open_tasks: int
    closed_tasks: int
    solved_tasks: int
    failed_tasks: int
    abandoned_tasks: int
    success_rate: float | None = None
    resolved_tasks: int = 0
    unresolved_tasks: int = 0
    cost_per_observed_task_usd: float | None = None
    cost_per_closed_task_usd: float | None = None
    cost_per_solved_task_usd: float | None = None
    tokens_per_task: Distribution = Field(default_factory=Distribution)
    cost_distribution_usd: Distribution = Field(default_factory=Distribution)
    solved_cost_distribution_usd: Distribution = Field(default_factory=Distribution)
    latency_distribution_ms: Distribution = Field(default_factory=Distribution)
    model_calls_per_task: float | None = None
    attempts_per_task: float | None = None
    retries: int = 0
    failed_trajectory_spend_usd: float | None = None
    reviewed_automated_passes: int = 0
    escaped_errors: int = 0
    observed_leak_rate: float | None = None
    policy_compliance_rate: float | None = None
    observed_cleanup_spend_usd: float | None = None
    cleanup_spend_per_solved_task_usd: float | None = None
    observed_cost_per_corrected_task_usd: float | None = None
    pricing: PricingCoverage = Field(default_factory=PricingCoverage)
    fresh_input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    observed_tool_cost_usd: float | None = None


class StrategyComparison(BaseModel):
    task_type: str
    execution_strategy: str
    strategy_version: str
    maturity_level: int
    closed_tasks: int
    solved_tasks: int
    eventual_success_rate: float | None = None
    attempt_success_rate: float | None = None
    cost_per_task_usd: float | None = None
    cost_per_solved_task_usd: float | None = None
    p50_cost_usd: float | None = None
    p90_cost_usd: float | None = None
    failed_spend_usd: float | None = None
    observed_cleanup_usd: float | None = None
    pricing_coverage_percent: float = 0
    model_composition: list[str] = Field(default_factory=list)
    provisional: bool = True
    recommendation_eligible: bool = False
    pareto_frontier: bool = False


class SavingsScenario(BaseModel):
    name: str
    label: str
    min_value: float | None = None
    max_value: float | None = None
    unit: str
    note: str
    overlaps_other_scenarios: bool = True


class TaskEconomicsReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
    report_period: dict[str, str | None] = Field(default_factory=dict)
    task_types: list[TaskCohort] = Field(default_factory=list)
    execution_strategies: list[StrategyComparison] = Field(default_factory=list)
    scenarios: list[SavingsScenario] = Field(default_factory=list)
    thresholds: dict[str, int] = Field(default_factory=lambda: {"provisional_closed_tasks": 30, "ranked_closed_tasks": 100})
    total_attempted_tasks: int = 0
    total_solved_tasks: int = 0
    pricing: PricingCoverage = Field(default_factory=PricingCoverage)

    @property
    def has_task_telemetry(self) -> bool:
        return self.total_attempted_tasks > 0


def _coverage(tasks: list[TaskTrajectory]) -> PricingCoverage:
    billable = [step for task in tasks for attempt in task.attempts for step in attempt.steps]
    resolved_events = [step for step in billable if step.pricing.resolved]
    source_counts = Counter(step.pricing.source for step in resolved_events)
    resolved_tasks = sum(task.resolved for task in tasks)
    unresolved_tasks = len(tasks) - resolved_tasks
    total_events = len(billable)
    return PricingCoverage(
        resolved_trajectories=resolved_tasks,
        unresolved_trajectories=unresolved_tasks,
        resolved_billable_events=len(resolved_events),
        unresolved_billable_events=total_events - len(resolved_events),
        observed_events=source_counts["observed"],
        customer_events=source_counts["customer"],
        reference_events=source_counts["reference"],
        coverage_percent=round(len(resolved_events) / total_events * 100, 1) if total_events else 0,
    )


def _distribution(values: Iterable[float]) -> Distribution:
    items = list(values)
    return Distribution(
        p50=round(_percentile(items, 0.5), 6) if items else None,
        p90=round(_percentile(items, 0.9), 6) if items else None,
    )


def _maturity(tasks: list[TaskTrajectory]) -> tuple[int, str]:
    if not tasks or not any(task.attempts for task in tasks):
        return 0, "No task cost data"
    if not any(task.closed for task in tasks):
        return 1, "Task cost measured"
    if not all(task.outcome in {"solved", "failed", "abandoned"} for task in tasks if task.closed):
        return 1, "Task cost measured"
    if not any(task.reviews for task in tasks):
        return 2, "Solved-task economics measured"
    return 3, "Correct-task economics measured"


def _cohort(task_type: str, tasks: list[TaskTrajectory]) -> TaskCohort:
    level, label = _maturity(tasks)
    closed = [task for task in tasks if task.closed]
    resolved = [task for task in tasks if task.resolved]
    resolved_closed = [task for task in closed if task.resolved]
    solved = [task for task in closed if task.outcome == "solved"]
    resolved_solved = [task for task in solved if task.resolved]
    costs = [task.cost_usd for task in resolved if task.cost_usd is not None]
    closed_costs = [task.cost_usd for task in resolved_closed if task.cost_usd is not None]
    solved_costs = [task.cost_usd for task in resolved_solved if task.cost_usd is not None]
    cleanup_tasks = [task for task in tasks if task.resolved_cleanup]
    cleanup_values = [task.cleanup_cost_usd or 0 for task in cleanup_tasks]
    reviewed_passes = sum(
        review.event.review_outcome == "correct" and review.event.escaped_error is not True
        for task in tasks
        for review in task.reviews
    )
    escaped = sum(task.has_escaped_error for task in tasks)
    cleanup_spend = sum(cleanup_values) if cleanup_tasks else None
    corrected = [task for task in cleanup_tasks if task.has_escaped_error and task.cost_usd is not None]
    corrected_costs = [(task.cost_usd or 0) + (task.cleanup_cost_usd or 0) for task in corrected]
    review_count = sum(len(task.reviews) for task in tasks)
    policy_count = sum(
        review.event.policy_compliant is not None
        for task in tasks for review in task.reviews
    )
    compliant = sum(
        review.event.policy_compliant is True
        for task in tasks for review in task.reviews
    )
    failed_spend = [task.cost_usd or 0 for task in closed if task.outcome in {"failed", "abandoned"} and task.resolved]
    coverage = _coverage(tasks)
    model_steps = [
        step.event
        for task in tasks
        for attempt in task.attempts
        for step in attempt.steps
        if hasattr(step.event, "usage")
    ]
    tool_costs = [
        step.cost_usd
        for task in tasks
        for attempt in task.attempts
        for step in attempt.steps
        if not hasattr(step.event, "usage") and step.cost_usd is not None
    ]
    return TaskCohort(
        task_type=task_type,
        maturity_level=level,
        maturity_label=label,
        attempted_tasks=len(tasks),
        open_tasks=len(tasks) - len(closed),
        closed_tasks=len(closed),
        solved_tasks=len(solved),
        failed_tasks=sum(task.outcome == "failed" for task in closed),
        abandoned_tasks=sum(task.outcome == "abandoned" for task in closed),
        success_rate=round(len(solved) / len(closed) * 100, 1) if closed else None,
        resolved_tasks=len(resolved),
        unresolved_tasks=len(tasks) - len(resolved),
        cost_per_observed_task_usd=round(sum(costs) / len(costs), 6) if costs else None,
        cost_per_closed_task_usd=round(sum(closed_costs) / len(closed_costs), 6) if closed_costs and len(closed_costs) == len(closed) else None,
        cost_per_solved_task_usd=round(sum(solved_costs) / len(solved_costs), 6) if solved_costs and len(solved_costs) == len(solved) else None,
        tokens_per_task=_distribution(task.tokens for task in tasks),
        cost_distribution_usd=_distribution(costs),
        solved_cost_distribution_usd=_distribution(solved_costs),
        latency_distribution_ms=_distribution(task.duration_ms for task in tasks if task.duration_ms is not None),
        model_calls_per_task=round(sum(task.model_calls for task in tasks) / len(tasks), 3) if tasks else None,
        attempts_per_task=round(sum(len(task.attempts) for task in tasks) / len(tasks), 3) if tasks else None,
        retries=sum(task.retry_count for task in tasks),
        failed_trajectory_spend_usd=round(sum(failed_spend), 6) if failed_spend and len(failed_spend) == sum(task.outcome in {"failed", "abandoned"} for task in closed) else None,
        reviewed_automated_passes=reviewed_passes,
        escaped_errors=escaped,
        observed_leak_rate=round(escaped / review_count * 100, 1) if review_count else None,
        policy_compliance_rate=round(compliant / policy_count * 100, 1) if policy_count else None,
        observed_cleanup_spend_usd=round(cleanup_spend, 6) if cleanup_spend is not None else None,
        cleanup_spend_per_solved_task_usd=round(cleanup_spend / len(solved), 6) if cleanup_spend is not None and solved else None,
        observed_cost_per_corrected_task_usd=round(sum(corrected_costs) / len(corrected_costs), 6) if corrected_costs else None,
        pricing=coverage,
        fresh_input_tokens=sum(
            event.usage.input_tokens - event.usage.cached_input_tokens - event.usage.cache_write_tokens
            for event in model_steps
        ),
        cached_input_tokens=sum(event.usage.cached_input_tokens for event in model_steps),
        cache_write_tokens=sum(event.usage.cache_write_tokens for event in model_steps),
        output_tokens=sum(event.usage.output_tokens for event in model_steps),
        observed_tool_cost_usd=round(sum(tool_costs), 6) if tool_costs else None,
    )


def compare_execution_strategies(
    tasks: Iterable[TaskTrajectory],
    *,
    task_type: str,
    provisional_closed_tasks: int = 30,
    ranked_closed_tasks: int = 100,
) -> list[StrategyComparison]:
    """Compare only equivalent task types; mixed task-type input is rejected."""
    selected = list(tasks)
    if any(task.task_type != task_type for task in selected):
        raise ValueError("cross-task strategy comparisons are not allowed")
    groups: dict[tuple[str, str], list[TaskTrajectory]] = defaultdict(list)
    for task in selected:
        groups[(task.execution_strategy, task.strategy_version)].append(task)
    comparisons: list[StrategyComparison] = []
    for (strategy, version), group in sorted(groups.items()):
        cohort = _cohort(task_type, group)
        closed = [task for task in group if task.closed]
        if len(closed) < provisional_closed_tasks:
            continue
        resolved_closed = [task for task in closed if task.resolved]
        solved = [task for task in closed if task.outcome == "solved"]
        solved_resolved = [task for task in solved if task.resolved]
        model_composition = sorted({
            f"{step.event.model_name}:{step.event.service_tier}"
            for task in group for attempt in task.attempts for step in attempt.steps
            if hasattr(step.event, "model_name")
        })
        comparisons.append(
            StrategyComparison(
                task_type=task_type,
                execution_strategy=strategy,
                strategy_version=version,
                maturity_level=cohort.maturity_level,
                closed_tasks=len(closed),
                solved_tasks=len(solved),
                eventual_success_rate=round(len(solved) / len(closed) * 100, 1) if closed else None,
                attempt_success_rate=round(sum(attempt.result is not None and attempt.result.outcome == "solved" for task in group for attempt in task.attempts) / max(1, sum(len(task.attempts) for task in group)) * 100, 1),
                cost_per_task_usd=round(sum(task.cost_usd or 0 for task in resolved_closed) / len(resolved_closed), 6) if resolved_closed and len(resolved_closed) == len(closed) else None,
                cost_per_solved_task_usd=round(sum(task.cost_usd or 0 for task in solved_resolved) / len(solved_resolved), 6) if solved_resolved and len(solved_resolved) == len(solved) else None,
                p50_cost_usd=cohort.cost_distribution_usd.p50,
                p90_cost_usd=cohort.cost_distribution_usd.p90,
                failed_spend_usd=cohort.failed_trajectory_spend_usd,
                observed_cleanup_usd=cohort.observed_cleanup_spend_usd,
                pricing_coverage_percent=cohort.pricing.coverage_percent,
                model_composition=model_composition,
                provisional=len(closed) < ranked_closed_tasks,
                recommendation_eligible=len(closed) >= ranked_closed_tasks,
            )
        )
    for item in comparisons:
        item.pareto_frontier = not any(
            other is not item
            and other.cost_per_solved_task_usd is not None
            and item.cost_per_solved_task_usd is not None
            and other.eventual_success_rate is not None
            and item.eventual_success_rate is not None
            and other.cost_per_solved_task_usd <= item.cost_per_solved_task_usd
            and other.eventual_success_rate >= item.eventual_success_rate
            and (
                other.cost_per_solved_task_usd < item.cost_per_solved_task_usd
                or other.eventual_success_rate > item.eventual_success_rate
            )
            for other in comparisons
        )
    return comparisons


def build_savings_scenarios(*, findings=None) -> list[SavingsScenario]:
    """Preserve independent opportunities; never emit an additive total."""
    scenarios: list[SavingsScenario] = []
    for finding in findings or []:
        estimate = finding.estimated_savings
        if estimate.min_tokens is None:
            continue
        scenarios.append(
            SavingsScenario(
                name=finding.rule_id,
                label=finding.title,
                min_value=estimate.min_tokens,
                max_value=estimate.max_tokens if estimate.max_tokens is not None else estimate.min_tokens,
                unit=estimate.unit,
                note="Independent scenario; opportunities may overlap and are not additive.",
            )
        )
    return scenarios


def calculate_task_economics(
    tasks: Iterable[TaskTrajectory],
    *,
    provisional_closed_tasks: int = 30,
    ranked_closed_tasks: int = 100,
    report_period: dict[str, str | None] | None = None,
) -> TaskEconomicsReport:
    trajectories = list(tasks)
    by_type: dict[str, list[TaskTrajectory]] = defaultdict(list)
    for task in trajectories:
        by_type[task.task_type].append(task)
    cohorts = [_cohort(task_type, group) for task_type, group in sorted(by_type.items())]
    strategies = [
        comparison
        for task_type in sorted(by_type)
        for comparison in compare_execution_strategies(
            by_type[task_type],
            task_type=task_type,
            provisional_closed_tasks=provisional_closed_tasks,
            ranked_closed_tasks=ranked_closed_tasks,
        )
    ]
    starts = [task.first_timestamp for task in trajectories if task.first_timestamp]
    ends = [event.timestamp for task in trajectories if task.task_result for event in [task.task_result]]
    return TaskEconomicsReport(
        report_period={
            "start": min(starts).astimezone(UTC).isoformat() if starts else None,
            "end": max(ends).astimezone(UTC).isoformat() if ends else (max(starts).astimezone(UTC).isoformat() if starts else None),
            **(report_period or {}),
        },
        task_types=cohorts,
        execution_strategies=strategies,
        total_attempted_tasks=len(trajectories),
        total_solved_tasks=sum(task.outcome == "solved" for task in trajectories),
        pricing=_coverage(trajectories),
        thresholds={"provisional_closed_tasks": provisional_closed_tasks, "ranked_closed_tasks": ranked_closed_tasks},
    )


task_economics = calculate_task_economics
TaskEconomics = TaskEconomicsReport
