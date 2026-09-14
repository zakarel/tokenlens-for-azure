"""Provider-neutral instrumentation primitives.

Every provider wrapper in this package follows the same contract:

* wrap an existing, already-authenticated client;
* never issue an extra inference request;
* return the provider's own response object unchanged;
* emit exactly one canonical, contentless record per call attempt;
* classify failures through an allow list instead of serializing exceptions.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Sequence

from ..telemetry.config import TelemetryConfig
from ..telemetry.privacy import FingerprintPolicy
from ..telemetry.schema import (
    ContentFeatures,
    ErrorCategory,
    Fingerprints,
    ModelRequestRecord,
    ResourceScope,
    TelemetryUsage,
)
from ..telemetry.writer import TelemetryWriter

#: Exception class names mapped to allow-listed categories. The exception's own
#: message is never read, because provider errors routinely echo request content
#: and endpoint URLs.
_ERROR_CLASS_CATEGORIES: dict[str, ErrorCategory] = {
    "RateLimitError": "rate_limited",
    "AuthenticationError": "authentication",
    "PermissionDeniedError": "authorization",
    "NotFoundError": "not_found",
    "BadRequestError": "bad_request",
    "UnprocessableEntityError": "bad_request",
    "ConflictError": "bad_request",
    "APITimeoutError": "timeout",
    "TimeoutError": "timeout",
    "APIConnectionError": "connection",
    "ConnectionError": "connection",
    "InternalServerError": "server_error",
    "APIStatusError": "server_error",
    "APIError": "unknown",
    "CancelledError": "cancelled",
}

_STATUS_CATEGORIES: tuple[tuple[range, ErrorCategory], ...] = (
    (range(400, 401), "bad_request"),
    (range(401, 402), "authentication"),
    (range(403, 404), "authorization"),
    (range(404, 405), "not_found"),
    (range(408, 409), "timeout"),
    (range(429, 430), "rate_limited"),
    (range(500, 600), "server_error"),
)


def classify_error(exc: BaseException) -> tuple[ErrorCategory, int | None]:
    """Return an allow-listed error category and status code for an exception."""
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    status = status if isinstance(status, int) and 100 <= status < 600 else None
    for name in (type(exc).__name__, *(base.__name__ for base in type(exc).__mro__[1:])):
        category = _ERROR_CLASS_CATEGORIES.get(name)
        if category:
            if category == "server_error" and status is not None:
                return _category_for_status(status), status
            return category, status
    if status is not None:
        return _category_for_status(status), status
    return "unknown", None


def _category_for_status(status: int) -> ErrorCategory:
    for window, category in _STATUS_CATEGORIES:
        if status in window:
            return category
    return "unknown"


def sample_decision(key: str, rate: float) -> bool:
    """Deterministic, stable sampling decision for one request key."""
    if rate >= 1:
        return True
    digest = hashlib.sha256(key.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big") / float(1 << 64) < rate


#: ``(usage, returned_model, error, exhausted)``. ``exhausted`` distinguishes a
#: stream the caller consumed to completion from one it abandoned early.
StreamFinish = Callable[[TelemetryUsage, Any, BaseException | None, bool], None]


def stream_outcome(error: BaseException | None, exhausted: bool) -> tuple[int | None, ErrorCategory | None]:
    """Classify how a stream ended into a status code and safe error category."""
    if error is not None:
        category, status = classify_error(error)
        return status, category
    if exhausted:
        return 200, None
    # The caller closed or dropped the stream before the terminal chunk. The
    # request was still issued, so it is recorded as cancelled rather than
    # discarded or reported as a success.
    return None, "cancelled"


def _value(source: Any, *names: str) -> Any:
    for name in names:
        if source is None:
            return None
        if isinstance(source, dict):
            if name in source:
                return source[name]
            continue
        value = getattr(source, name, None)
        if value is not None:
            return value
    return None


def _int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def extract_usage(usage: Any) -> TelemetryUsage:
    """Read aggregate token counters from an OpenAI-compatible usage object."""
    if usage is None:
        return TelemetryUsage()
    input_details = _value(usage, "prompt_tokens_details", "input_tokens_details")
    output_details = _value(usage, "completion_tokens_details", "output_tokens_details")
    return TelemetryUsage(
        input_tokens=_int(_value(usage, "input_tokens", "prompt_tokens")),
        output_tokens=_int(_value(usage, "output_tokens", "completion_tokens")),
        cached_tokens=_int(_value(input_details, "cached_tokens")),
        reasoning_tokens=_int(_value(output_details, "reasoning_tokens")),
        cache_write_tokens=_int(_value(input_details, "cache_creation_tokens")),
    )


@dataclass
class Instrumentation:
    """Shared state for a wrapped provider client."""

    provider: str
    writer: TelemetryWriter
    config: TelemetryConfig
    deployment_mode: str = "unknown"
    service_tier: str = "standard"
    workload: str | None = None
    scope: ResourceScope = field(default_factory=ResourceScope)
    measure_tokens: bool = False
    fingerprints: FingerprintPolicy = field(default_factory=lambda: FingerprintPolicy(enabled=False, key=None))

    def record(
        self,
        *,
        api: str,
        deployment_name: str,
        model_name: str,
        usage: TelemetryUsage,
        started: float,
        status_code: int | None,
        error_category: ErrorCategory | None = None,
        streamed: bool = False,
        retry_of: str | None = None,
        retry_count: int = 0,
        content_features: ContentFeatures | None = None,
        fingerprints: Fingerprints | None = None,
        request_key: str | None = None,
    ) -> bool:
        """Emit one canonical record. Returns ``True`` when it was persisted."""
        key = request_key or uuid.uuid4().hex
        if not sample_decision(key, self.config.sample_rate):
            return False
        record = ModelRequestRecord(
            source="sdk_wrapper",
            event_id=hashlib.sha256(key.encode("utf-8")).hexdigest()[:32],
            timestamp=datetime.now(UTC),
            provider=self.provider,
            deployment_name=deployment_name or "unknown",
            model_name=model_name or "unknown",
            service_tier=self.service_tier,
            deployment_mode=self.deployment_mode,
            api=api,
            workload=self.workload,
            scope=self.scope,
            usage=usage,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            status_code=status_code,
            error_category=error_category,
            retry_of=retry_of,
            retry_count=retry_count,
            streamed=streamed,
            sample_rate=self.config.sample_rate,
            content_features=content_features or ContentFeatures(),
            fingerprints=fingerprints or Fingerprints(),
        )
        return self.writer.write(record)

    # -- contentless request measurement --------------------------------
    def describe_request(
        self,
        *,
        messages: Sequence[Any] | None,
        tools: Sequence[Any] | None,
        max_output_tokens: int | None,
        model_hint: str,
        system_text: str | None = None,
    ) -> tuple[ContentFeatures, Fingerprints]:
        """Measure request structure without retaining any content."""
        features = ContentFeatures(
            message_count=len(messages) if messages is not None else None,
            tool_count=len(tools) if tools is not None else None,
            max_output_tokens=max_output_tokens,
        )
        fingerprints = Fingerprints()
        if not (self.measure_tokens or self.fingerprints.active):
            return features, fingerprints
        system = system_text if system_text is not None else _system_text(messages)
        tool_schema = _tool_schema_text(tools)
        if self.measure_tokens:
            from ..tokens import count_text

            features = features.model_copy(
                update={
                    "system_prompt_tokens": count_text(system, model_hint) if system else None,
                    "tool_definition_tokens": count_text(tool_schema, model_hint) if tool_schema else None,
                }
            )
        if self.fingerprints.active:
            fingerprints = Fingerprints(
                system_prompt=self.fingerprints.fingerprint(system),
                tool_schema=self.fingerprints.fingerprint(tool_schema),
            )
        return features, fingerprints


def _message_text(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, Iterable) and not isinstance(content, (bytes, bytearray)):
        parts = []
        for part in content:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return " ".join(parts)
    return ""


def _system_text(messages: Sequence[Any] | None) -> str:
    if not messages:
        return ""
    collected = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role in {"system", "developer"}:
            collected.append(_message_text(message))
    return "\n".join(collected)


def _tool_schema_text(tools: Sequence[Any] | None) -> str:
    if not tools:
        return ""
    try:
        return json.dumps(
            [tool if isinstance(tool, dict) else getattr(tool, "model_dump", dict)() for tool in tools],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        return ""


class AttributeProxy:
    """Delegating proxy that preserves the wrapped SDK surface."""

    __slots__ = ("_tokenlens_target",)

    def __init__(self, target: Any) -> None:
        object.__setattr__(self, "_tokenlens_target", target)

    @property
    def tokenlens_wrapped(self) -> Any:
        return object.__getattribute__(self, "_tokenlens_target")

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_tokenlens_target"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_tokenlens_target"), name, value)

    def __dir__(self) -> list[str]:
        return sorted(set(dir(object.__getattribute__(self, "_tokenlens_target"))))

    def __repr__(self) -> str:
        return f"TokenLensInstrumented({object.__getattribute__(self, '_tokenlens_target')!r})"


class StreamProxy:
    """Iterate a provider stream without buffering content.

    Chunks are yielded unchanged. Only the terminal usage object is read, so
    streaming instrumentation stays O(1) in memory.

    Exactly one telemetry record is emitted per stream, whichever way the stream
    ends: normal exhaustion, a provider error mid-stream, an explicit ``close``
    or context-manager exit, or abandonment without either.
    """

    __slots__ = ("_stream", "_on_finish", "_usage", "_finished", "_model", "_exhausted", "__weakref__")

    def __init__(self, stream: Any, on_finish: StreamFinish) -> None:
        self._stream = stream
        self._on_finish = on_finish
        self._usage = TelemetryUsage()
        self._finished = False
        self._model = None
        self._exhausted = False

    def __iter__(self) -> "StreamProxy":
        return self

    def __next__(self) -> Any:
        try:
            chunk = next(self._stream)
        except StopIteration:
            self._exhausted = True
            self._finish(None)
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised unchanged after safe classification
            # A provider failure mid-stream is still one request attempt, so it
            # must produce one safe record rather than silently none.
            self._finish(exc)
            raise
        self._observe(chunk)
        return chunk

    def __enter__(self) -> "StreamProxy":
        entered = getattr(self._stream, "__enter__", None)
        if entered is not None:
            entered()
        return self

    def __exit__(self, exc_type: Any = None, exc: Any = None, traceback: Any = None) -> None:
        try:
            exited = getattr(self._stream, "__exit__", None)
            if exited is not None:
                exited(exc_type, exc, traceback)
        finally:
            self._finish(exc if isinstance(exc, BaseException) else None)

    def close(self) -> None:
        try:
            closer = getattr(self._stream, "close", None)
            if closer is not None:
                closer()
        finally:
            self._finish(None)

    def __del__(self) -> None:  # pragma: no cover - interpreter finalization timing
        try:
            self._finish(None)
        except BaseException:
            # Never let telemetry raise during garbage collection.
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def _observe(self, chunk: Any) -> None:
        usage = getattr(chunk, "usage", None)
        if usage is None:
            response = getattr(chunk, "response", None)
            usage = getattr(response, "usage", None) if response is not None else None
        if usage is None and isinstance(chunk, dict):
            usage = chunk.get("usage") or (chunk.get("response") or {}).get("usage")
        if usage is not None:
            self._usage = extract_usage(usage)
        model = getattr(chunk, "model", None)
        if model is None:
            response = getattr(chunk, "response", None)
            model = getattr(response, "model", None) if response is not None else None
        if isinstance(model, str) and model:
            self._model = model

    def _finish(self, error: BaseException | None) -> None:
        if self._finished:
            return
        self._finished = True
        self._on_finish(self._usage, self._model, error, self._exhausted)
