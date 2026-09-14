"""Contentless instrumentation for ``OpenAI`` and ``AzureOpenAI`` clients.

Usage::

    from tokenlens.integrations.openai import instrument_openai

    client = instrument_openai(existing_client, deployment_mode="global", workload="support-assistant")
    response = client.chat.completions.create(model="support-prod", messages=messages)

The wrapper adds no inference request of its own, returns the SDK's response
object unchanged, and records only aggregate usage, latency, status, and
structural request features.
"""

from __future__ import annotations

import time
from typing import Any

from ..telemetry.config import TelemetryConfig
from ..telemetry.privacy import FingerprintPolicy
from ..telemetry.schema import TelemetryUsage
from ..telemetry.writer import TelemetryWriter
from .base import AttributeProxy, Instrumentation, StreamProxy, classify_error, extract_usage, stream_outcome

__all__ = ["instrument_openai"]


def _is_azure(client: Any) -> bool:
    return type(client).__name__.casefold().startswith("azure") or hasattr(client, "_azure_deployment")


class _InstrumentedCreate:
    """Wrap one ``create`` callable while preserving its signature."""

    def __init__(self, target: Any, instrumentation: Instrumentation, api: str) -> None:
        self._target = target
        self._instrumentation = instrumentation
        self._api = api

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        instrumentation = self._instrumentation
        deployment = str(kwargs.get("model") or (args[0] if args else "") or "unknown")
        messages = kwargs.get("messages")
        request_input = kwargs.get("input")
        if messages is None and isinstance(request_input, list):
            messages = request_input
        features, fingerprints = instrumentation.describe_request(
            messages=messages,
            tools=kwargs.get("tools"),
            max_output_tokens=kwargs.get("max_output_tokens") or kwargs.get("max_completion_tokens") or kwargs.get("max_tokens"),
            model_hint=deployment,
            system_text=kwargs.get("instructions") if isinstance(kwargs.get("instructions"), str) else None,
        )
        request_key = kwargs.pop("tokenlens_request_key", None)
        retry_of = kwargs.pop("tokenlens_retry_of", None)
        streaming = bool(kwargs.get("stream"))
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
        if streaming and hasattr(response, "__next__"):
            def finish_stream(usage: TelemetryUsage, model: Any, error: BaseException | None, exhausted: bool) -> None:
                status, category = stream_outcome(error, exhausted)
                instrumentation.record(
                    api=self._api,
                    deployment_name=deployment,
                    model_name=model or "unknown",
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

            return StreamProxy(response, finish_stream)
        usage_source = getattr(response, "usage", None)
        if usage_source is None and isinstance(response, dict):
            usage_source = response.get("usage")
        model_name = getattr(response, "model", None)
        if model_name is None and isinstance(response, dict):
            model_name = response.get("model")
        instrumentation.record(
            api=self._api,
            deployment_name=deployment,
            model_name=str(model_name or "unknown"),
            usage=extract_usage(usage_source),
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


class _CreateSurface(AttributeProxy):
    """Expose an instrumented ``create`` alongside the original surface."""

    __slots__ = ("_tokenlens_create",)

    def __init__(self, target: Any, instrumentation: Instrumentation, api: str) -> None:
        super().__init__(target)
        object.__setattr__(self, "_tokenlens_create", _InstrumentedCreate(target.create, instrumentation, api))

    @property
    def create(self) -> Any:
        return object.__getattribute__(self, "_tokenlens_create")


class _ChatSurface(AttributeProxy):
    __slots__ = ("_tokenlens_completions",)

    def __init__(self, target: Any, instrumentation: Instrumentation) -> None:
        super().__init__(target)
        object.__setattr__(
            self,
            "_tokenlens_completions",
            _CreateSurface(target.completions, instrumentation, "chat.completions"),
        )

    @property
    def completions(self) -> Any:
        return object.__getattribute__(self, "_tokenlens_completions")


class _InstrumentedOpenAIClient(AttributeProxy):
    __slots__ = ("_tokenlens_chat", "_tokenlens_responses", "_tokenlens_instrumentation")

    def __init__(self, target: Any, instrumentation: Instrumentation) -> None:
        super().__init__(target)
        chat = getattr(target, "chat", None)
        responses = getattr(target, "responses", None)
        object.__setattr__(
            self,
            "_tokenlens_chat",
            _ChatSurface(chat, instrumentation) if chat is not None and hasattr(chat, "completions") else chat,
        )
        object.__setattr__(
            self,
            "_tokenlens_responses",
            _CreateSurface(responses, instrumentation, "responses") if responses is not None and hasattr(responses, "create") else responses,
        )
        object.__setattr__(self, "_tokenlens_instrumentation", instrumentation)

    @property
    def chat(self) -> Any:
        return object.__getattribute__(self, "_tokenlens_chat")

    @property
    def responses(self) -> Any:
        return object.__getattribute__(self, "_tokenlens_responses")

    @property
    def tokenlens_instrumentation(self) -> Instrumentation:
        return object.__getattribute__(self, "_tokenlens_instrumentation")


def instrument_openai(
    client: Any,
    *,
    deployment_mode: str = "unknown",
    workload: str | None = None,
    provider: str | None = None,
    service_tier: str = "standard",
    config: TelemetryConfig | None = None,
    writer: TelemetryWriter | None = None,
    measure_tokens: bool = False,
    fingerprints: FingerprintPolicy | None = None,
    resource_name: str | None = None,
    project_name: str | None = None,
) -> Any:
    """Return an instrumented view of an OpenAI-compatible client.

    ``deployment_mode`` is not inferred: PTU sizing and pricing differ between
    Global and Regional deployments, so an unset mode stays ``unknown``.
    """
    from ..telemetry.schema import ResourceScope

    resolved = config or (writer.config if writer is not None else TelemetryConfig.from_env())
    instrumentation = Instrumentation(
        provider=provider or ("azure_foundry" if _is_azure(client) else "openai"),
        writer=writer or TelemetryWriter(resolved),
        config=resolved,
        deployment_mode=deployment_mode or "unknown",
        service_tier=service_tier,
        workload=workload,
        scope=ResourceScope(resource_name=resource_name, project_name=project_name, workload=workload),
        measure_tokens=measure_tokens,
        fingerprints=fingerprints or resolved.fingerprints,
    )
    return _InstrumentedOpenAIClient(client, instrumentation)
