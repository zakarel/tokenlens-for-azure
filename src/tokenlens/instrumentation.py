"""Optional local instrumentation helper for explicit task economics events.

The helper only appends metadata and aggregate usage to a local JSONL file. It
does not proxy traffic, inspect credentials, or make network calls.
"""

from __future__ import annotations

import atexit
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator


def _event_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(UTC).isoformat()


class EventWriter:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path or os.getenv("TOKENLENS_EVENTS_PATH", "tokenlens-events.jsonl"))
        self._lock = threading.Lock()
        self._handle = None
        atexit.register(self.close)

    def emit(self, event: dict[str, Any]) -> None:
        safe = dict(event)
        safe.setdefault("schema_version", 2)
        safe.setdefault("event_id", _event_id())
        safe.setdefault("timestamp", _now())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(safe, separators=(",", ":"), ensure_ascii=False) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def flush(self) -> None:
        # Each event opens and flushes its append handle, so there is no
        # buffered state to drain. Kept as a stable API for process hooks.
        return None

    def close(self) -> None:
        self.flush()


class TaskContext:
    def __init__(
        self,
        writer: EventWriter,
        *,
        task_id: str,
        task_type: str,
        execution_strategy: str,
        strategy_version: str,
        attempt_id: str = "attempt-1",
    ) -> None:
        self.writer = writer
        self.task_id = task_id
        self.task_type = task_type
        self.execution_strategy = execution_strategy
        self.strategy_version = strategy_version
        self.attempt_id = attempt_id
        self._step_index = 0

    def _common(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "attempt_id": self.attempt_id,
            "execution_strategy": self.execution_strategy,
            "strategy_version": self.strategy_version,
        }

    def record_model_call(
        self,
        *,
        deployment: str,
        model: str,
        call: Callable[[], Any],
        provider: str = "azure_foundry",
        service_tier: str = "standard",
        region: str | None = "global",
        usage: dict[str, int] | None = None,
        observed_cost_usd: float | None = None,
        duration_ms: float | None = None,
    ) -> Any:
        """Execute a caller-provided function and emit aggregate usage only."""
        result = call()
        measured = usage or self._usage_from_result(result)
        self._step_index += 1
        self.writer.emit(
            {
                **self._common(),
                "event_type": "model_call",
                "step_index": self._step_index,
                "provider": provider,
                "deployment_name": deployment,
                "model_name": model,
                "service_tier": service_tier,
                "region": region,
                "usage": {
                    "input_tokens": int(measured.get("input_tokens", measured.get("prompt_tokens", 0))),
                    "cached_input_tokens": int(measured.get("cached_input_tokens", measured.get("cached_tokens", 0))),
                    "cache_write_tokens": int(measured.get("cache_write_tokens", 0)),
                    "output_tokens": int(measured.get("output_tokens", measured.get("completion_tokens", 0))),
                    "reasoning_tokens": int(measured.get("reasoning_tokens", 0)),
                },
                "observed_cost_usd": observed_cost_usd,
                "duration_ms": duration_ms,
            }
        )
        return result

    def record_tool_step(
        self,
        *,
        tool_category: str,
        observed_cost_usd: float | None = None,
        duration_ms: float | None = None,
        outcome: str | None = "success",
    ) -> None:
        self._step_index += 1
        self.writer.emit(
            {
                **self._common(),
                "event_type": "tool_step",
                "step_index": self._step_index,
                "tool_category": tool_category,
                "observed_cost_usd": observed_cost_usd,
                "duration_ms": duration_ms,
                "outcome": outcome,
            }
        )

    def complete(self, *, outcome: str, automated_check: str | None = None, duration_ms: float | None = None) -> None:
        self.writer.emit(
            {
                **self._common(),
                "event_type": "task_result",
                "outcome": outcome,
                "automated_check": automated_check,
                "duration_ms": duration_ms,
            }
        )

    def review(
        self,
        *,
        review_outcome: str,
        escaped_error: bool | None = None,
        policy_compliant: bool | None = None,
        cleanup_cost_usd: float | None = None,
    ) -> None:
        self.writer.emit(
            {
                **self._common(),
                "event_type": "human_review",
                "review_outcome": review_outcome,
                "escaped_error": escaped_error,
                "policy_compliant": policy_compliant,
                "cleanup_cost_usd": cleanup_cost_usd,
            }
        )

    @staticmethod
    def _usage_from_result(result: Any) -> dict[str, int]:
        usage = getattr(result, "usage", None)
        if usage is None and isinstance(result, dict):
            usage = result.get("usage")
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            return usage.model_dump()
        if isinstance(usage, dict):
            return usage
        return {
            key: int(getattr(usage, key, 0) or 0)
            for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens")
        }


@contextmanager
def task(
    *,
    task_id: str,
    task_type: str,
    execution_strategy: str,
    strategy_version: str,
    attempt_id: str = "attempt-1",
    path: str | os.PathLike[str] | None = None,
) -> Iterator[TaskContext]:
    context = TaskContext(
        EventWriter(path),
        task_id=task_id,
        task_type=task_type,
        execution_strategy=execution_strategy,
        strategy_version=strategy_version,
        attempt_id=attempt_id,
    )
    try:
        yield context
    finally:
        context.writer.flush()


def emit_event(event: dict[str, Any], *, path: str | os.PathLike[str] | None = None) -> None:
    """Manual event emission for applications that do not use the context helper."""
    EventWriter(path).emit(event)
