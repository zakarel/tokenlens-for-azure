"""Contentless instrumentation for Claude models on Microsoft Foundry.

Claude deployments are called through the Anthropic Messages API, never through
OpenAI ``/chat/completions``. Usage::

    from tokenlens.integrations.anthropic_foundry import instrument_anthropic_foundry

    client = instrument_anthropic_foundry(existing_client, deployment_mode="global", workload="coding-agent")
    message = client.messages.create(model="claude-prod", max_tokens=512, messages=messages)
"""

from __future__ import annotations

import time
from typing import Any

from ..telemetry.config import TelemetryConfig
from ..telemetry.privacy import FingerprintPolicy
from ..telemetry.schema import ResourceScope, TelemetryUsage
from ..telemetry.writer import TelemetryWriter
from .base import AttributeProxy, Instrumentation, classify_error, stream_outcome

__all__ = ["instrument_anthropic_foundry"]


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _get(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def extract_messages_usage(usage: Any) -> TelemetryUsage:
    """Read Anthropic Messages usage, including cache-read and cache-write tokens."""
    if usage is None:
        return TelemetryUsage()
    return TelemetryUsage(
        input_tokens=_int(_get(usage, "input_tokens")),
        output_tokens=_int(_get(usage, "output_tokens")),
        cached_tokens=_int(_get(usage, "cache_read_input_tokens")),
        cache_write_tokens=_int(_get(usage, "cache_creation_input_tokens")),
    )


class _MessageStreamProxy:
    """Yield Messages stream events unchanged while accumulating terminal usage.

    Exactly one telemetry record is emitted per stream: on exhaustion, on a
    provider error, on explicit close/exit, or on abandonment.
    """

    __slots__ = ("_stream", "_finish_callback", "_usage", "_model", "_finished", "_exhausted", "__weakref__")

    def __init__(self, stream: Any, finish: Any) -> None:
        self._stream = stream
        self._finish_callback = finish
        self._usage = TelemetryUsage()
        self._model = None
        self._finished = False
        self._exhausted = False

    def __iter__(self) -> "_MessageStreamProxy":
        return self

    def __next__(self) -> Any:
        try:
            event = next(self._stream)
        except StopIteration:
            self._exhausted = True
            self._complete(None)
            raise
        except BaseException as exc:  # noqa: BLE001 - re-raised unchanged after safe classification
            self._complete(exc)
            raise
        self._observe(event)
        return event

    def __enter__(self) -> "_MessageStreamProxy":
        entered = getattr(self._stream, "__enter__", None)
        if entered is not None:
            self._stream = entered()
        return self

    def __exit__(self, exc_type: Any = None, exc: Any = None, traceback: Any = None) -> None:
        try:
            exited = getattr(self._stream, "__exit__", None)
            if exited is not None:
                exited(exc_type, exc, traceback)
        finally:
            self._complete(exc if isinstance(exc, BaseException) else None)

    def close(self) -> None:
        try:
            closer = getattr(self._stream, "close", None)
            if closer is not None:
                closer()
        finally:
            self._complete(None)

    def __del__(self) -> None:  # pragma: no cover - interpreter finalization timing
        try:
            self._complete(None)
        except BaseException:
            pass

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)

    def _observe(self, event: Any) -> None:
        message = _get(event, "message")
        if message is not None:
            usage = extract_messages_usage(_get(message, "usage"))
            merged = self._usage.model_copy(
                update={
                    "input_tokens": usage.input_tokens or self._usage.input_tokens,
                    "cached_tokens": usage.cached_tokens or self._usage.cached_tokens,
                    "cache_write_tokens": usage.cache_write_tokens or self._usage.cache_write_tokens,
                    "output_tokens": usage.output_tokens or self._usage.output_tokens,
                }
            )
            self._usage = merged
            model = _get(message, "model")
            if isinstance(model, str) and model:
                self._model = model
        delta_usage = _get(event, "usage")
        if delta_usage is not None:
            # ``message_delta`` carries the running output count and, for cached
            # prompts, the terminal input counts.
            usage = extract_messages_usage(delta_usage)
            merged = self._usage.model_copy(
                update={
                    "output_tokens": usage.output_tokens or self._usage.output_tokens,
                    "input_tokens": usage.input_tokens or self._usage.input_tokens,
                    "cached_tokens": usage.cached_tokens or self._usage.cached_tokens,
                    "cache_write_tokens": usage.cache_write_tokens or self._usage.cache_write_tokens,
                }
            )
            self._usage = merged

    def _complete(self, error: BaseException | None) -> None:
        if self._finished:
            return
        self._finished = True
        self._finish_callback(self._usage, self._model, error, self._exhausted)


class _InstrumentedMessagesCreate:
    def __init__(self, target: Any, instrumentation: Instrumentation, api: str) -> None:
        self._target = target
        self._instrumentation = instrumentation
        self._api = api

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        instrumentation = self._instrumentation
        deployment = str(kwargs.get("model") or (args[0] if args else "") or "unknown")
        system = kwargs.get("system")
        system_text = system if isinstance(system, str) else None
        features, fingerprints = instrumentation.describe_request(
            messages=kwargs.get("messages"),
            tools=kwargs.get("tools"),
            max_output_tokens=kwargs.get("max_tokens"),
            model_hint=deployment,
            system_text=system_text,
        )
        request_key = kwargs.pop("tokenlens_request_key", None)
        retry_of = kwargs.pop("tokenlens_retry_of", None)
        streaming = bool(kwargs.get("stream")) or self._api == "messages.stream"
        started = time.perf_counter()
        try:
            response = self._target(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised unchanged after safe classification
            category, status = classify_error(exc)
            instrumentation.record(
                api=self._api,
                deployment_name=deployment,
                model_name="unknown",
                usage=TelemetryUsage(),
                started=started,
                status_code=status,
                error_category=category,
                streamed=streaming,
                retry_of=retry_of,
                content_features=features,
                fingerprints=fingerprints,
                request_key=request_key,
            )
            raise

        def finish(usage: TelemetryUsage, model: Any, error: BaseException | None, exhausted: bool) -> None:
            status, category = stream_outcome(error, exhausted)
            instrumentation.record(
                api=self._api,
                deployment_name=deployment,
                model_name=str(model or "unknown"),
                usage=usage,
                started=started,
                status_code=status,
                error_category=category,
                streamed=True,
                retry_of=retry_of,
                content_features=features,
                fingerprints=fingerprints,
                request_key=request_key,
            )

        if streaming and (hasattr(response, "__next__") or hasattr(response, "__enter__")):
            return _MessageStreamProxy(response, finish)
        instrumentation.record(
            api=self._api,
            deployment_name=deployment,
            model_name=str(_get(response, "model") or "unknown"),
            usage=extract_messages_usage(_get(response, "usage")),
            started=started,
            status_code=200,
            streamed=False,
            retry_of=retry_of,
            content_features=features,
            fingerprints=fingerprints,
            request_key=request_key,
        )
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


class _MessagesSurface(AttributeProxy):
    __slots__ = ("_tokenlens_create", "_tokenlens_stream")

    def __init__(self, target: Any, instrumentation: Instrumentation) -> None:
        super().__init__(target)
        object.__setattr__(
            self,
            "_tokenlens_create",
            _InstrumentedMessagesCreate(target.create, instrumentation, "messages"),
        )
        streamer = getattr(target, "stream", None)
        object.__setattr__(
            self,
            "_tokenlens_stream",
            _InstrumentedMessagesCreate(streamer, instrumentation, "messages.stream") if streamer is not None else None,
        )

    @property
    def create(self) -> Any:
        return object.__getattribute__(self, "_tokenlens_create")

    @property
    def stream(self) -> Any:
        streamer = object.__getattribute__(self, "_tokenlens_stream")
        if streamer is None:
            raise AttributeError("stream")
        return streamer


class _InstrumentedAnthropicClient(AttributeProxy):
    __slots__ = ("_tokenlens_messages", "_tokenlens_instrumentation")

    def __init__(self, target: Any, instrumentation: Instrumentation) -> None:
        super().__init__(target)
        messages = getattr(target, "messages", None)
        if messages is None or not hasattr(messages, "create"):
            raise TypeError("client does not expose a Messages API; Claude on Foundry requires messages.create")
        object.__setattr__(self, "_tokenlens_messages", _MessagesSurface(messages, instrumentation))
        object.__setattr__(self, "_tokenlens_instrumentation", instrumentation)

    @property
    def messages(self) -> Any:
        return object.__getattribute__(self, "_tokenlens_messages")

    @property
    def tokenlens_instrumentation(self) -> Instrumentation:
        return object.__getattribute__(self, "_tokenlens_instrumentation")


def instrument_anthropic_foundry(
    client: Any,
    *,
    deployment_mode: str = "unknown",
    workload: str | None = None,
    service_tier: str = "standard",
    provider: str = "azure_foundry",
    config: TelemetryConfig | None = None,
    writer: TelemetryWriter | None = None,
    measure_tokens: bool = False,
    fingerprints: FingerprintPolicy | None = None,
    resource_name: str | None = None,
    project_name: str | None = None,
) -> Any:
    """Return an instrumented view of an ``AnthropicFoundry``-style client."""
    resolved = config or (writer.config if writer is not None else TelemetryConfig.from_env())
    instrumentation = Instrumentation(
        provider=provider,
        writer=writer or TelemetryWriter(resolved),
        config=resolved,
        deployment_mode=deployment_mode or "unknown",
        service_tier=service_tier,
        workload=workload,
        scope=ResourceScope(resource_name=resource_name, project_name=project_name, workload=workload),
        measure_tokens=measure_tokens,
        fingerprints=fingerprints or resolved.fingerprints,
    )
    return _InstrumentedAnthropicClient(client, instrumentation)
