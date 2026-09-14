"""OpenTelemetry import and span-processor behaviour.

No OpenTelemetry package is required or imported: the fixtures are synthetic
OTLP/JSON payloads and a duck-typed fake span. No network access occurs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import load_records_many
from tokenlens.telemetry import TelemetryConfig, TelemetryWriter
from tokenlens.telemetry.otel import (
    OtelImportError,
    TokenLensSpanProcessor,
    import_file,
    import_spans,
    map_span,
    write_import,
)

CANARY_PROMPT = "CANARY-PROMPT-otel-should-never-store-this"
CANARY_COMPLETION = "CANARY-COMPLETION-otel-should-never-store-this"

START_NANOS = int(datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp() * 1_000_000_000)


def span(**overrides):
    attributes = {
        "gen_ai.system": "az.ai.openai",
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": "example-support-prod",
        "gen_ai.response.model": "gpt-4.1-2026-04-14",
        "gen_ai.usage.input_tokens": 4200,
        "gen_ai.usage.output_tokens": 380,
        "gen_ai.usage.cached_input_tokens": 1800,
        "tokenlens.deployment_mode": "global",
        "tokenlens.workload": "support-assistant",
        "gen_ai.prompt": CANARY_PROMPT,
        "gen_ai.completion": CANARY_COMPLETION,
        "gen_ai.input.messages": [{"role": "user", "content": CANARY_PROMPT}],
        "server.address": "example-endpoint.invalid",
        "http.request.header.authorization": "Bearer CANARY-SECRET",
    }
    attributes.update(overrides.pop("attributes", {}))
    payload = {
        "name": "chat example-support-prod",
        "spanId": "0123456789abcdef",
        "startTimeUnixNano": START_NANOS,
        "endTimeUnixNano": START_NANOS + 1_450_000_000,
        "attributes": attributes,
        "status": {"code": "STATUS_CODE_OK"},
    }
    payload.update(overrides)
    return payload


def otlp(spans):
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "example-app"}}]},
                "scopeSpans": [{"scope": {"name": "example"}, "spans": spans}],
            }
        ]
    }


def test_span_maps_to_canonical_record_and_discards_content():
    record, discarded, ignored = map_span(span())
    assert record is not None
    assert record.source == "otel"
    assert record.provider == "azure_foundry"
    assert record.deployment_name == "example-support-prod"
    assert record.model_name == "gpt-4.1-2026-04-14"
    assert record.deployment_mode == "global"
    assert record.workload == "support-assistant"
    assert record.usage.input_tokens == 4200
    assert record.usage.cached_tokens == 1800
    assert record.usage.output_tokens == 380
    assert record.latency_ms == pytest.approx(1450.0)
    assert discarded >= 3
    serialized = record.model_dump_json()
    assert CANARY_PROMPT not in serialized
    assert CANARY_COMPLETION not in serialized
    assert "example-endpoint.invalid" not in serialized
    assert "CANARY-SECRET" not in serialized


def test_unknown_deployment_mode_stays_unknown():
    payload = span()
    payload["attributes"].pop("tokenlens.deployment_mode")
    record, _, _ = map_span(payload)
    assert record.deployment_mode == "unknown"


def test_error_spans_use_allow_listed_categories():
    payload = span(
        attributes={"error.type": "RateLimitError", "http.response.status_code": 429},
        status={"code": "STATUS_CODE_ERROR"},
    )
    record, _, _ = map_span(payload)
    assert record.error_category == "rate_limited"
    assert record.status_code == 429

    payload = span(attributes={"error.type": "SomethingProprietary"}, status={"code": "STATUS_CODE_ERROR"})
    record, _, _ = map_span(payload)
    assert record.error_category == "unknown"


def test_spans_without_a_model_are_skipped():
    payload = span()
    payload["attributes"].pop("gen_ai.request.model")
    payload["attributes"].pop("gen_ai.response.model")
    record, _, _ = map_span(payload)
    assert record is None


def test_import_detects_duplicates_through_a_local_identifier():
    result = import_spans([span(), span(), span(spanId="fedcba9876543210")])
    assert result.spans_seen == 3
    assert result.spans_mapped == 2
    assert result.duplicates_skipped == 1
    assert result.content_attributes_discarded >= 6
    # The upstream span id is never written into telemetry.
    assert all("0123456789abcdef" not in record.event_id for record in result.records)


def test_import_file_supports_otlp_json_array_and_jsonl(tmp_path):
    otlp_path = tmp_path / "spans-otlp.json"
    otlp_path.write_text(json.dumps(otlp([span()])), encoding="utf-8")
    assert import_file(otlp_path).spans_mapped == 1

    array_path = tmp_path / "spans-array.json"
    array_path.write_text(json.dumps([span(), span(spanId="a1")]), encoding="utf-8")
    assert import_file(array_path).spans_mapped == 2

    jsonl_path = tmp_path / "spans.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(span(spanId=f"span-{index}")) for index in range(3)) + "\n",
        encoding="utf-8",
    )
    assert import_file(jsonl_path).spans_mapped == 3

    with pytest.raises(OtelImportError):
        import_file(tmp_path / "missing.json")


def test_otlp_attribute_list_form_is_supported():
    attributes = [
        {"key": "gen_ai.system", "value": {"stringValue": "az.ai.openai"}},
        {"key": "gen_ai.request.model", "value": {"stringValue": "example-support-prod"}},
        {"key": "gen_ai.response.model", "value": {"stringValue": "gpt-4.1-2026-04-14"}},
        {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1200"}},
        {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "300"}},
        {"key": "gen_ai.prompt", "value": {"stringValue": CANARY_PROMPT}},
    ]
    payload = span()
    payload["attributes"] = attributes
    record, discarded, _ = map_span(payload)
    assert record.usage.input_tokens == 1200
    assert record.usage.output_tokens == 300
    assert discarded == 1


def test_imported_records_analyze_offline_without_prompt_text(tmp_path):
    result = import_spans([span(spanId=f"span-{index}") for index in range(12)])
    writer = TelemetryWriter(TelemetryConfig(output_dir=tmp_path / "traces", retention_days=0))
    assert write_import(result, writer) == 12
    records, source = load_records_many([str(tmp_path / "traces")])
    report = analyze(records, source, generated_at="2026-09-14T13:00:00Z")
    assert report.summary.requests_analyzed == 12
    assert report.summary.input_tokens == 4200 * 12
    assert CANARY_PROMPT not in json.dumps(report.model_dump(mode="json"))


def test_span_processor_writes_without_replacing_an_exporter(tmp_path):
    processor = TokenLensSpanProcessor(
        config=TelemetryConfig(output_dir=tmp_path / "traces", retention_days=0)
    )
    processor.on_start(object())
    processor.on_end(span())
    processor.on_end(span(spanId="another-span"))
    assert processor.spans_written == 2
    assert processor.force_flush() is True
    processor.shutdown()
    lines = (tmp_path / "traces" / "tokenlens-2026-09-14.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert CANARY_PROMPT not in "\n".join(lines)


def test_span_processor_accepts_a_duck_typed_sdk_span(tmp_path):
    processor = TokenLensSpanProcessor(
        config=TelemetryConfig(output_dir=tmp_path / "traces", retention_days=0)
    )
    sdk_span = SimpleNamespace(
        name="chat example-support-prod",
        attributes={
            "gen_ai.system": "az.ai.openai",
            "gen_ai.request.model": "example-support-prod",
            "gen_ai.response.model": "gpt-4.1-2026-04-14",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 4,
            "gen_ai.prompt": CANARY_PROMPT,
        },
        context=SimpleNamespace(span_id=12345),
        start_time=START_NANOS,
        end_time=START_NANOS + 500_000_000,
        status=SimpleNamespace(status_code=SimpleNamespace(name="OK")),
    )
    processor.on_end(sdk_span)
    assert processor.spans_written == 1
    assert processor.content_attributes_discarded == 1
    assert CANARY_PROMPT not in (tmp_path / "traces" / "tokenlens-2026-09-14.jsonl").read_text(encoding="utf-8")


def test_jsonl_of_complete_otlp_payloads_is_detected(tmp_path):
    """Each line is a full OTLP document, so the file starts with `{"resourceSpans"`."""
    path = tmp_path / "otlp-lines.jsonl"
    path.write_text(
        "\n".join(json.dumps(otlp([span(spanId=f"span-{index}")])) for index in range(4)) + "\n",
        encoding="utf-8",
    )
    assert path.read_text(encoding="utf-8").startswith('{"resourceSpans"')
    result = import_file(path)
    assert result.spans_seen == 4
    assert result.spans_mapped == 4
    assert result.duplicates_skipped == 0


def test_pretty_printed_otlp_document_still_imports(tmp_path):
    path = tmp_path / "otlp-pretty.json"
    path.write_text(json.dumps(otlp([span(), span(spanId="second")]), indent=2), encoding="utf-8")
    assert import_file(path).spans_mapped == 2


def test_single_line_jsonl_and_empty_files(tmp_path):
    single = tmp_path / "single.jsonl"
    single.write_text(json.dumps(span()) + "\n", encoding="utf-8")
    assert import_file(single).spans_mapped == 1

    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n", encoding="utf-8")
    assert import_file(empty).spans_mapped == 0


def test_malformed_input_names_the_failing_line(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(json.dumps(span()) + "\n{not json}\n", encoding="utf-8")
    with pytest.raises(OtelImportError) as error:
        import_file(path)
    assert "line 2" in str(error.value)


def test_repeated_import_of_the_same_export_is_idempotent(tmp_path):
    source = tmp_path / "spans.jsonl"
    source.write_text("\n".join(json.dumps(span(spanId=f"span-{index}")) for index in range(6)) + "\n", encoding="utf-8")
    config = TelemetryConfig(output_dir=tmp_path / "traces", retention_days=0)

    first = TelemetryWriter(config)
    assert write_import(import_file(source), first) == 6

    second = TelemetryWriter(config)
    assert write_import(import_file(source), second) == 0
    assert second.skipped_duplicates == 6

    lines = (tmp_path / "traces" / "tokenlens-2026-09-14.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6

    # An overlapping export that adds two new spans appends only those two.
    extended = tmp_path / "spans-extended.jsonl"
    extended.write_text("\n".join(json.dumps(span(spanId=f"span-{index}")) for index in range(8)) + "\n", encoding="utf-8")
    third = TelemetryWriter(config)
    assert write_import(import_file(extended), third) == 2
    assert len((tmp_path / "traces" / "tokenlens-2026-09-14.jsonl").read_text(encoding="utf-8").splitlines()) == 8
