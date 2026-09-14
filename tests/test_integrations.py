"""Provider wrapper contract tests with fake SDK clients.

No provider SDK is installed or called. The fakes mimic the response shapes of
the OpenAI Chat Completions API, the OpenAI Responses API, and the Anthropic
Messages API. Every fake counts its invocations so the wrappers can be proven
not to add a second billable request.
"""

from __future__ import annotations

import gc
import json
from types import SimpleNamespace

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import load_records_many
from tokenlens.integrations.anthropic_foundry import instrument_anthropic_foundry
from tokenlens.integrations.base import classify_error, sample_decision
from tokenlens.integrations.openai import instrument_openai
from tokenlens.reports import report_html
from tokenlens.telemetry import TelemetryConfig, TelemetryWriter
from tokenlens.telemetry.privacy import FingerprintPolicy

CANARY_PROMPT = "CANARY-PROMPT-never-store-this-user-sentence"
CANARY_SYSTEM = "CANARY-SYSTEM-never-store-this-policy"
CANARY_KEY = "CANARY-SECRET-key-value"


class FakeCompletions:
    def __init__(self) -> None:
        self.calls = 0
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if kwargs.get("stream"):
            return iter(
                [
                    SimpleNamespace(model="gpt-4.1-2026-04-14", usage=None),
                    SimpleNamespace(
                        model="gpt-4.1-2026-04-14",
                        usage=SimpleNamespace(
                            prompt_tokens=1200,
                            completion_tokens=180,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=400),
                            completion_tokens_details=SimpleNamespace(reasoning_tokens=30),
                        ),
                    ),
                ]
            )
        return SimpleNamespace(
            id="resp-1",
            model="gpt-4.1-2026-04-14",
            usage=SimpleNamespace(
                prompt_tokens=4200,
                completion_tokens=380,
                prompt_tokens_details=SimpleNamespace(cached_tokens=1800),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
            ),
        )


class FakeResponses:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            model="gpt-5.1-2026-02-02",
            usage=SimpleNamespace(
                input_tokens=900,
                output_tokens=120,
                input_tokens_details=SimpleNamespace(cached_tokens=256),
                output_tokens_details=SimpleNamespace(reasoning_tokens=64),
            ),
        )


class FakeAzureOpenAI:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)
        self.responses = FakeResponses()
        self._azure_deployment = "synthetic"
        self.api_version = "2026-05-01"

    def close(self) -> None:
        return None


class FakeRateLimitError(Exception):
    status_code = 429

    def __init__(self) -> None:
        super().__init__(f"429 rate limit hit for {CANARY_PROMPT} at https://example-endpoint.invalid")


class FailingCompletions:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        raise self.error


class FakeMessages:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if kwargs.get("stream"):
            return iter(
                [
                    SimpleNamespace(
                        type="message_start",
                        message=SimpleNamespace(
                            model="claude-synthetic-4",
                            usage=SimpleNamespace(
                                input_tokens=2000,
                                output_tokens=1,
                                cache_read_input_tokens=1500,
                                cache_creation_input_tokens=100,
                            ),
                        ),
                    ),
                    SimpleNamespace(type="content_block_delta", message=None),
                    SimpleNamespace(
                        type="message_delta",
                        message=None,
                        usage=SimpleNamespace(output_tokens=310, input_tokens=0),
                    ),
                ]
            )
        return SimpleNamespace(
            model="claude-synthetic-4",
            usage=SimpleNamespace(
                input_tokens=3000,
                output_tokens=450,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=120,
            ),
        )


class FakeAnthropicFoundry:
    def __init__(self) -> None:
        self.messages = FakeMessages()

    def close(self) -> None:
        return None


def writer_for(tmp_path, **config_overrides) -> TelemetryWriter:
    return TelemetryWriter(TelemetryConfig(output_dir=tmp_path / "traces", retention_days=0, **config_overrides))


def written(tmp_path) -> list[dict]:
    lines: list[dict] = []
    for path in sorted((tmp_path / "traces").glob("*.jsonl")):
        lines.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    return lines


def messages() -> list[dict]:
    return [
        {"role": "system", "content": CANARY_SYSTEM},
        {"role": "user", "content": CANARY_PROMPT},
    ]


def test_chat_completion_emits_one_contentless_record_and_no_extra_call(tmp_path):
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(
        client,
        deployment_mode="global",
        workload="support-assistant",
        writer=writer_for(tmp_path),
    )
    response = instrumented.chat.completions.create(
        model="support-prod",
        messages=messages(),
        tools=[{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
        max_tokens=1000,
    )
    assert client.completions.calls == 1
    assert response.id == "resp-1"

    records = written(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["record_type"] == "model_request"
    assert record["deployment_name"] == "support-prod"
    assert record["model_name"] == "gpt-4.1-2026-04-14"
    assert record["deployment_mode"] == "global"
    assert record["workload"] == "support-assistant"
    assert record["usage"] == {
        "input_tokens": 4200,
        "cached_tokens": 1800,
        "output_tokens": 380,
        "reasoning_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert record["status_code"] == 200
    assert record["latency_ms"] >= 0
    assert record["content_features"] == {"message_count": 2, "tool_count": 1, "max_output_tokens": 1000}
    serialized = json.dumps(records)
    assert CANARY_PROMPT not in serialized
    assert CANARY_SYSTEM not in serialized


def test_original_response_object_is_returned_unchanged(tmp_path):
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(client, writer=writer_for(tmp_path))
    response = instrumented.chat.completions.create(model="d", messages=messages())
    direct = client.chat.completions.create(model="d", messages=messages())
    assert type(response) is type(direct)
    assert response.model == direct.model
    # Unwrapped attributes stay reachable and identical.
    assert instrumented.api_version == client.api_version
    assert instrumented.close() is None


def test_responses_api_usage_including_cached_and_reasoning(tmp_path):
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    instrumented.responses.create(model="reasoning-prod", input=[{"role": "user", "content": CANARY_PROMPT}])
    assert client.responses.calls == 1
    record = written(tmp_path)[0]
    assert record["api"] == "responses"
    assert record["usage"]["input_tokens"] == 900
    assert record["usage"]["cached_tokens"] == 256
    assert record["usage"]["reasoning_tokens"] == 64
    assert record["model_name"] == "gpt-5.1-2026-02-02"


def test_streaming_records_terminal_usage_without_buffering(tmp_path):
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.chat.completions.create(model="stream-prod", messages=messages(), stream=True)
    assert written(tmp_path) == []
    chunks = list(stream)
    assert len(chunks) == 2
    record = written(tmp_path)[0]
    assert record["streamed"] is True
    assert record["usage"]["input_tokens"] == 1200
    assert record["usage"]["output_tokens"] == 180
    assert record["usage"]["cached_tokens"] == 400


def test_provider_failure_emits_safe_error_telemetry(tmp_path):
    client = FakeAzureOpenAI()
    client.completions = FailingCompletions(FakeRateLimitError())
    client.chat = SimpleNamespace(completions=client.completions)
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    with pytest.raises(FakeRateLimitError):
        instrumented.chat.completions.create(model="support-prod", messages=messages())
    record = written(tmp_path)[0]
    assert record["status_code"] == 429
    assert record["error_category"] == "rate_limited"
    assert record["usage"]["input_tokens"] == 0
    serialized = json.dumps(record)
    assert CANARY_PROMPT not in serialized
    assert "example-endpoint.invalid" not in serialized


def test_error_classifier_is_allow_listed():
    assert classify_error(FakeRateLimitError()) == ("rate_limited", 429)
    assert classify_error(TimeoutError("slow")) == ("timeout", None)
    assert classify_error(ConnectionError("no route")) == ("connection", None)
    assert classify_error(ValueError("anything")) == ("unknown", None)


def test_claude_messages_usage_and_cache_fields(tmp_path):
    client = FakeAnthropicFoundry()
    instrumented = instrument_anthropic_foundry(
        client,
        deployment_mode="global",
        workload="coding-agent",
        writer=writer_for(tmp_path),
    )
    instrumented.messages.create(
        model="claude-prod",
        max_tokens=512,
        system=CANARY_SYSTEM,
        messages=[{"role": "user", "content": CANARY_PROMPT}],
    )
    assert client.messages.calls == 1
    record = written(tmp_path)[0]
    assert record["api"] == "messages"
    assert record["deployment_name"] == "claude-prod"
    assert record["model_name"] == "claude-synthetic-4"
    assert record["usage"]["input_tokens"] == 3000
    assert record["usage"]["cached_tokens"] == 900
    assert record["usage"]["cache_write_tokens"] == 120
    assert record["usage"]["output_tokens"] == 450
    assert CANARY_SYSTEM not in json.dumps(record)


def test_claude_streaming_terminal_usage(tmp_path):
    client = FakeAnthropicFoundry()
    instrumented = instrument_anthropic_foundry(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.messages.create(
        model="claude-prod",
        max_tokens=512,
        messages=[{"role": "user", "content": CANARY_PROMPT}],
        stream=True,
    )
    list(stream)
    record = written(tmp_path)[0]
    assert record["usage"]["input_tokens"] == 2000
    assert record["usage"]["cached_tokens"] == 1500
    assert record["usage"]["output_tokens"] == 310
    assert record["streamed"] is True


def test_claude_wrapper_requires_messages_api():
    with pytest.raises(TypeError):
        instrument_anthropic_foundry(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_: None))))


def test_fingerprints_are_keyed_and_content_free(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_FINGERPRINT_KEY", "local-installation-secret")
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(
        client,
        deployment_mode="global",
        writer=writer_for(tmp_path),
        measure_tokens=True,
        fingerprints=FingerprintPolicy.from_env(),
    )
    instrumented.chat.completions.create(model="support-prod", messages=messages())
    instrumented.chat.completions.create(model="support-prod", messages=messages())
    records = written(tmp_path)
    first, second = records[0]["fingerprints"], records[1]["fingerprints"]
    assert first["system_prompt"].startswith("hmac-sha256:")
    assert first["system_prompt"] == second["system_prompt"]
    assert records[0]["content_features"]["system_prompt_tokens"] > 0
    assert CANARY_SYSTEM not in json.dumps(records)


def test_deterministic_sampling_is_stable_per_request_key():
    assert sample_decision("request-a", 1.0) is True
    first = sample_decision("request-a", 0.5)
    assert sample_decision("request-a", 0.5) is first
    decisions = [sample_decision(f"request-{index}", 0.5) for index in range(400)]
    assert 150 < sum(decisions) < 250


def test_sampled_out_requests_are_not_written(tmp_path):
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(
        client,
        deployment_mode="global",
        writer=writer_for(tmp_path, sample_rate=0.0001),
    )
    instrumented.chat.completions.create(model="support-prod", messages=messages(), tokenlens_request_key="stable-key")
    assert client.completions.calls == 1
    assert written(tmp_path) == []


def test_end_to_end_instrumented_application_analyzes_offline(tmp_path):
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(
        client,
        deployment_mode="global",
        workload="support-assistant",
        writer=writer_for(tmp_path),
    )
    for _ in range(5):
        instrumented.chat.completions.create(model="support-prod", messages=messages())
    assert client.completions.calls == 5
    assert len(written(tmp_path)) == 5

    records, source = load_records_many([str(tmp_path / "traces")])
    report = analyze(records, source, generated_at="2026-09-14T13:00:00Z")
    assert report.summary.requests_analyzed == 5
    assert report.summary.input_tokens == 4200 * 5
    rendered = report_html(report)
    for canary in (CANARY_PROMPT, CANARY_SYSTEM, CANARY_KEY):
        assert canary not in rendered


# -- streaming lifecycle regressions -----------------------------------------


class FailingStream:
    """A stream that yields some chunks and then fails, like a dropped connection."""

    def __init__(self, error: Exception, chunks=None) -> None:
        self.error = error
        self.chunks = list(chunks or [])
        self.closed = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.chunks:
            return self.chunks.pop(0)
        raise self.error

    def close(self):
        self.closed += 1


class StreamingCompletions:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        return self.stream


def streaming_client(stream) -> FakeAzureOpenAI:
    client = FakeAzureOpenAI()
    client.completions = StreamingCompletions(stream)
    client.chat = SimpleNamespace(completions=client.completions)
    return client


def test_stream_failure_emits_exactly_one_safe_error_record(tmp_path):
    partial = SimpleNamespace(
        model="gpt-4.1-2026-04-14",
        usage=SimpleNamespace(
            prompt_tokens=1200,
            completion_tokens=40,
            prompt_tokens_details=SimpleNamespace(cached_tokens=400),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )
    client = streaming_client(FailingStream(FakeRateLimitError(), [partial]))
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.chat.completions.create(model="support-prod", messages=messages(), stream=True)
    assert next(stream) is partial
    assert written(tmp_path) == []
    with pytest.raises(FakeRateLimitError):
        next(stream)

    records = written(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["streamed"] is True
    assert record["status_code"] == 429
    assert record["error_category"] == "rate_limited"
    # Usage observed before the failure is preserved, not discarded.
    assert record["usage"]["input_tokens"] == 1200
    assert record["usage"]["output_tokens"] == 40
    serialized = json.dumps(record)
    assert CANARY_PROMPT not in serialized
    assert "example-endpoint.invalid" not in serialized

    # Continuing to poke the dead stream never produces a second record.
    with pytest.raises(FakeRateLimitError):
        next(stream)
    stream.close()
    assert len(written(tmp_path)) == 1


def test_abandoned_stream_emits_one_cancelled_record_on_close(tmp_path):
    chunks = [SimpleNamespace(model="gpt-4.1-2026-04-14", usage=None) for _ in range(3)]
    client = streaming_client(FailingStream(StopIteration(), chunks))
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.chat.completions.create(model="support-prod", messages=messages(), stream=True)
    next(stream)
    stream.close()

    records = written(tmp_path)
    assert len(records) == 1
    assert records[0]["error_category"] == "cancelled"
    assert "status_code" not in records[0]
    assert records[0]["streamed"] is True
    # Closing twice is safe and still yields exactly one record.
    stream.close()
    assert len(written(tmp_path)) == 1


def test_abandoned_stream_is_finalized_when_garbage_collected(tmp_path):
    chunks = [SimpleNamespace(model="gpt-4.1-2026-04-14", usage=None) for _ in range(3)]
    client = streaming_client(FailingStream(StopIteration(), chunks))
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.chat.completions.create(model="support-prod", messages=messages(), stream=True)
    next(stream)
    del stream
    gc.collect()
    records = written(tmp_path)
    assert len(records) == 1
    assert records[0]["error_category"] == "cancelled"


def test_exhausted_stream_records_success_exactly_once_even_after_close(tmp_path):
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.chat.completions.create(model="support-prod", messages=messages(), stream=True)
    assert len(list(stream)) == 2
    stream.close()
    records = written(tmp_path)
    assert len(records) == 1
    assert records[0]["status_code"] == 200
    assert "error_category" not in records[0]


def test_stream_context_manager_exit_records_once(tmp_path):
    client = FakeAzureOpenAI()
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    with instrumented.chat.completions.create(model="support-prod", messages=messages(), stream=True) as stream:
        for _ in stream:
            pass
    assert len(written(tmp_path)) == 1
    assert written(tmp_path)[0]["status_code"] == 200


def test_close_failure_still_finalizes_the_record(tmp_path):
    class UnclosableStream(FailingStream):
        def close(self):
            raise OSError("socket already gone")

    client = streaming_client(UnclosableStream(StopIteration(), [SimpleNamespace(model="m", usage=None)]))
    instrumented = instrument_openai(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.chat.completions.create(model="support-prod", messages=messages(), stream=True)
    next(stream)
    with pytest.raises(OSError):
        stream.close()
    assert len(written(tmp_path)) == 1
    assert written(tmp_path)[0]["error_category"] == "cancelled"


class FailingMessagesStream(FailingStream):
    pass


def test_claude_stream_failure_emits_one_safe_record(tmp_path):
    start = SimpleNamespace(
        type="message_start",
        message=SimpleNamespace(
            model="claude-synthetic-4",
            usage=SimpleNamespace(
                input_tokens=2000,
                output_tokens=1,
                cache_read_input_tokens=1500,
                cache_creation_input_tokens=0,
            ),
        ),
    )
    client = FakeAnthropicFoundry()
    client.messages = SimpleNamespace(
        create=lambda **kwargs: FailingMessagesStream(FakeRateLimitError(), [start]),
        stream=None,
    )
    instrumented = instrument_anthropic_foundry(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.messages.create(
        model="claude-prod",
        max_tokens=512,
        messages=[{"role": "user", "content": CANARY_PROMPT}],
        stream=True,
    )
    next(stream)
    with pytest.raises(FakeRateLimitError):
        next(stream)
    records = written(tmp_path)
    assert len(records) == 1
    assert records[0]["error_category"] == "rate_limited"
    assert records[0]["status_code"] == 429
    assert records[0]["usage"]["input_tokens"] == 2000
    assert CANARY_PROMPT not in json.dumps(records[0])


def test_claude_abandoned_stream_records_cancelled_once(tmp_path):
    client = FakeAnthropicFoundry()
    instrumented = instrument_anthropic_foundry(client, deployment_mode="global", writer=writer_for(tmp_path))
    stream = instrumented.messages.create(
        model="claude-prod",
        max_tokens=512,
        messages=[{"role": "user", "content": CANARY_PROMPT}],
        stream=True,
    )
    next(stream)
    stream.close()
    stream.close()
    records = written(tmp_path)
    assert len(records) == 1
    assert records[0]["error_category"] == "cancelled"
