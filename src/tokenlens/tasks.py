"""Explicit task reconstruction from the unified append-only event stream."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from .events import (
    HumanReviewEvent,
    ModelCallEvent,
    TaskEvent,
    TaskResultEvent,
    ToolStepEvent,
    parse_event,
    validate_event_stream,
)
from .pricing import PricingResolution, resolve_event_cost


@dataclass(frozen=True)
class TaskStep:
    event: ModelCallEvent | ToolStepEvent
    pricing: PricingResolution

    @property
    def cost_usd(self) -> float | None:
        return self.pricing.cost_usd if self.pricing.resolved else None


@dataclass
class TaskAttempt:
    attempt_id: str
    steps: list[TaskStep] = field(default_factory=list)
    result: TaskResultEvent | None = None

    @property
    def resolved(self) -> bool:
        return all(step.pricing.resolved for step in self.steps)

    @property
    def cost_usd(self) -> float | None:
        if not self.resolved:
            return None
        return sum(step.pricing.cost_usd or 0 for step in self.steps)

    @property
    def duration_ms(self) -> float | None:
        durations = [step.event.duration_ms for step in self.steps if step.event.duration_ms is not None]
        return sum(durations) if durations else None


@dataclass
class TaskReview:
    event: HumanReviewEvent


@dataclass
class TaskTrajectory:
    task_id: str
    task_type: str
    execution_strategy: str
    strategy_version: str
    attempts: list[TaskAttempt] = field(default_factory=list)
    reviews: list[TaskReview] = field(default_factory=list)
    task_result: TaskResultEvent | None = None
    first_timestamp: datetime | None = None

    @property
    def closed(self) -> bool:
        return self.task_result is not None

    @property
    def outcome(self) -> str | None:
        return self.task_result.outcome if self.task_result else None

    @property
    def resolved(self) -> bool:
        return all(attempt.resolved for attempt in self.attempts)

    @property
    def cost_usd(self) -> float | None:
        if not self.resolved:
            return None
        return sum(attempt.cost_usd or 0 for attempt in self.attempts)

    @property
    def tokens(self) -> int:
        total = 0
        for attempt in self.attempts:
            for step in attempt.steps:
                if isinstance(step.event, ModelCallEvent):
                    total += step.event.usage.input_tokens + step.event.usage.output_tokens
        return total

    @property
    def model_calls(self) -> int:
        return sum(isinstance(step.event, ModelCallEvent) for attempt in self.attempts for step in attempt.steps)

    @property
    def tool_steps(self) -> int:
        return sum(isinstance(step.event, ToolStepEvent) for attempt in self.attempts for step in attempt.steps)

    @property
    def retry_count(self) -> int:
        return max(0, len(self.attempts) - 1)

    @property
    def cleanup_cost_usd(self) -> float | None:
        costs = [review.event.cleanup_cost_usd for review in self.reviews if review.event.cleanup_cost_usd is not None]
        return sum(costs) if costs else None

    @property
    def has_escaped_error(self) -> bool:
        return any(review.event.escaped_error is True for review in self.reviews)

    @property
    def resolved_cleanup(self) -> bool:
        return bool(self.reviews) and all(review.event.cleanup_cost_usd is not None for review in self.reviews)

    @property
    def duration_ms(self) -> float | None:
        durations = [attempt.duration_ms for attempt in self.attempts if attempt.duration_ms is not None]
        if durations:
            return sum(durations)
        if self.task_result is not None:
            return self.task_result.duration_ms
        return None


def _as_events(events: Iterable[TaskEvent | dict[str, object]]) -> list[TaskEvent]:
    parsed = [parse_event(event) if isinstance(event, dict) else event for event in events]
    return validate_event_stream(parsed)


def reconstruct_tasks(
    events: Iterable[TaskEvent | dict[str, object]],
    *,
    customer_catalog=None,
    reference_catalog=None,
    required_currency: str = "USD",
) -> list[TaskTrajectory]:
    """Reconstruct tasks independent of file order and late review arrival."""
    stream = _as_events(events)
    grouped: dict[str, list[TaskEvent]] = {}
    for event in stream:
        grouped.setdefault(event.task_id, []).append(event)
    trajectories: list[TaskTrajectory] = []
    for task_id, task_events in grouped.items():
        metadata = next(
            (
                event
                for event in task_events
                if event.task_type is not None and event.execution_strategy is not None and event.strategy_version is not None
            ),
            None,
        )
        if metadata is None:
            raise ValueError("task is missing explicit task metadata")
        trajectory = TaskTrajectory(
            task_id=task_id,
            task_type=metadata.task_type or "",
            execution_strategy=metadata.execution_strategy or "",
            strategy_version=metadata.strategy_version or "",
            first_timestamp=min(event.timestamp for event in task_events),
        )
        attempts: dict[str, TaskAttempt] = {}
        for event in sorted(task_events, key=lambda item: (item.timestamp, getattr(item, "step_index", -1))):
            if isinstance(event, (ModelCallEvent, ToolStepEvent)):
                attempt = attempts.setdefault(event.attempt_id or "attempt-unknown", TaskAttempt(event.attempt_id or "attempt-unknown"))
                attempt.steps.append(
                    TaskStep(
                        event=event,
                        pricing=resolve_event_cost(
                            event,
                            customer_catalog=customer_catalog,
                            reference_catalog=reference_catalog,
                            required_currency=required_currency,
                        ),
                    )
                )
            elif isinstance(event, TaskResultEvent):
                attempt = attempts.setdefault(event.attempt_id, TaskAttempt(event.attempt_id))
                if attempt.result is not None and attempt.result.outcome != event.outcome:
                    raise ValueError("conflicting attempt outcome")
                attempt.result = event
                if trajectory.task_result is None or event.timestamp >= trajectory.task_result.timestamp:
                    trajectory.task_result = event
            elif isinstance(event, HumanReviewEvent):
                trajectory.reviews.append(TaskReview(event))
        trajectory.attempts = sorted(attempts.values(), key=lambda attempt: attempt.attempt_id)
        trajectories.append(trajectory)
    return sorted(trajectories, key=lambda task: (task.task_type, task.task_id))


TaskReconstruction = reconstruct_tasks
