"""Unified, privacy-safe task economics event schema and validation."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class EventValidationError(ValueError):
    """Raised when an event stream violates task-economics invariants."""


class UsageTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_subsets(self) -> "UsageTokens":
        if self.cached_input_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens plus cache_write_tokens cannot exceed input_tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("reasoning_tokens cannot exceed output_tokens")
        return self


class _CommonEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[2] = 2
    event_id: str = Field(min_length=1, max_length=200)
    timestamp: datetime
    task_id: str = Field(min_length=1, max_length=200)
    task_type: str | None = Field(default=None, min_length=1, max_length=100)
    attempt_id: str | None = Field(default=None, min_length=1, max_length=200)
    execution_strategy: str | None = Field(default=None, min_length=1, max_length=100)
    strategy_version: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("timestamp")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @field_validator("event_id", "task_id", "attempt_id", mode="before")
    @classmethod
    def reject_newlines(cls, value: object) -> object:
        if isinstance(value, str) and any(char in value for char in "\r\n"):
            raise ValueError("identifiers cannot contain newlines")
        return value


class ModelCallEvent(_CommonEvent):
    event_type: Literal["model_call"] = "model_call"
    task_type: str = Field(min_length=1, max_length=100)
    execution_strategy: str = Field(min_length=1, max_length=100)
    strategy_version: str = Field(min_length=1, max_length=100)
    step_index: int = Field(ge=0)
    provider: str = Field(min_length=1, max_length=100)
    deployment_name: str = Field(min_length=1, max_length=200)
    model_name: str = Field(min_length=1, max_length=200)
    service_tier: str = Field(default="standard", min_length=1, max_length=50)
    region: str | None = Field(default=None, max_length=100)
    context_length: int | None = Field(default=None, ge=0)
    usage: UsageTokens
    observed_cost_usd: float | None = Field(default=None, ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    status_code: int | None = Field(default=None, ge=100, le=999)


class ToolStepEvent(_CommonEvent):
    event_type: Literal["tool_step"] = "tool_step"
    task_type: str = Field(min_length=1, max_length=100)
    execution_strategy: str = Field(min_length=1, max_length=100)
    strategy_version: str = Field(min_length=1, max_length=100)
    step_index: int = Field(ge=0)
    tool_category: str = Field(min_length=1, max_length=100)
    observed_cost_usd: float | None = Field(default=None, ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    outcome: Literal["success", "failed", "abandoned"] | None = None


class TaskResultEvent(_CommonEvent):
    event_type: Literal["task_result"] = "task_result"
    task_type: str = Field(min_length=1, max_length=100)
    execution_strategy: str = Field(min_length=1, max_length=100)
    strategy_version: str = Field(min_length=1, max_length=100)
    attempt_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["solved", "failed", "abandoned"]
    automated_check: str | None = Field(default=None, max_length=100)
    duration_ms: float | None = Field(default=None, ge=0)


class HumanReviewEvent(_CommonEvent):
    event_type: Literal["human_review"] = "human_review"
    review_outcome: Literal["correct", "incorrect", "unclear"]
    escaped_error: bool | None = None
    policy_compliant: bool | None = None
    cleanup_cost_usd: float | None = Field(default=None, ge=0)


TaskEvent: TypeAlias = Annotated[
    ModelCallEvent | ToolStepEvent | TaskResultEvent | HumanReviewEvent,
    Field(discriminator="event_type"),
]

_EVENT_ADAPTER = TypeAdapter(TaskEvent)

Event = TaskEvent
ModelCall = ModelCallEvent
ToolStep = ToolStepEvent
TaskResult = TaskResultEvent
HumanReview = HumanReviewEvent


def parse_event(raw: dict[str, object], aliases: dict[str, str] | None = None) -> TaskEvent:
    """Parse one v2 event without exposing raw event values in errors."""
    if aliases:
        mapped = dict(raw)
        for canonical, alias in aliases.items():
            if canonical in mapped:
                continue
            value: object = raw
            for part in alias.split("."):
                if not isinstance(value, dict) or part not in value:
                    value = None
                    break
                value = value[part]
            if value is not None:
                mapped[canonical] = value
                if alias in mapped and alias != canonical:
                    mapped.pop(alias, None)
                if "." in alias:
                    mapped.pop(alias.split(".", 1)[0], None)
        raw = mapped
    return _EVENT_ADAPTER.validate_python(raw)


def validate_event_stream(events: list[TaskEvent]) -> list[TaskEvent]:
    """Validate stream-level invariants and return the de-duplicated stream.

    Duplicate event IDs are ignored only when the payload is byte-for-byte
    equivalent at the model level; conflicting duplicates are rejected.
    """
    unique: dict[str, TaskEvent] = {}
    task_metadata: dict[str, tuple[str | None, str | None, str | None]] = {}
    step_keys: dict[tuple[str, str], set[int]] = {}
    outcomes: dict[str, list[TaskResultEvent]] = {}
    known_tasks: set[str] = set()
    for event in events:
        prior = unique.get(event.event_id)
        if prior is not None:
            if prior.model_dump(mode="json") != event.model_dump(mode="json"):
                raise EventValidationError("conflicting duplicate event_id")
            continue
        unique[event.event_id] = event
        if not isinstance(event, HumanReviewEvent):
            known_tasks.add(event.task_id)
        if event.task_type is not None or event.execution_strategy is not None or event.strategy_version is not None:
            metadata = (event.task_type, event.execution_strategy, event.strategy_version)
            previous = task_metadata.get(event.task_id)
            if previous is not None and any(
                value is not None and value != previous[index]
                for index, value in enumerate(metadata)
            ):
                raise EventValidationError("conflicting task metadata")
            if all(value is not None for value in metadata):
                task_metadata[event.task_id] = metadata
        if isinstance(event, (ModelCallEvent, ToolStepEvent)):
            if not event.attempt_id:
                raise EventValidationError("step event is missing attempt_id")
            key = (event.task_id, event.attempt_id)
            seen = step_keys.setdefault(key, set())
            if event.step_index in seen:
                raise EventValidationError("duplicate step_index within task attempt")
            seen.add(event.step_index)
        if isinstance(event, TaskResultEvent):
            outcomes.setdefault(event.task_id, []).append(event)
    for event in unique.values():
        if isinstance(event, HumanReviewEvent) and event.task_id not in known_tasks:
            raise EventValidationError("human review references unknown task")
    for task_id, results in outcomes.items():
        ordered = sorted(results, key=lambda item: item.timestamp)
        if any(previous.outcome == "solved" and current.outcome != "solved" for previous, current in zip(ordered, ordered[1:])):
            raise EventValidationError("conflicting final task outcome")
    return list(unique.values())


def is_task_event(value: object) -> bool:
    return isinstance(value, (ModelCallEvent, ToolStepEvent, TaskResultEvent, HumanReviewEvent))
