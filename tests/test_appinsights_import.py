"""Application Insights import behaviour.

All fixtures are synthetic Log Analytics/Application Insights export shapes.
The importer must reuse the OpenTelemetry GenAI mapper's allow list, so a
canary prompt placed anywhere in a synthetic export must never reach a
canonical record, and the offline import + live-collection paths must
produce equivalent records for equivalent data.
"""

from __future__ import annotations

import json

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import load_records_many
from tokenlens.telemetry import TelemetryConfig, TelemetryWriter
from tokenlens.telemetry.appinsights import OtelImportError, import_export, import_file
from tokenlens.telemetry.otel import write_import

CANARY_PROMPT = "CANARY-PROMPT-app-insights-should-never-store-this"


def logs_query_result(rows: list[dict]) -> dict:
    """A synthetic Log Analytics/`az monitor app-insights query` shape."""
    columns = ["timestamp", "name", "id", "duration", "success", "resultCode", "customDimensions", "customMeasurements"]
    return {
        "tables": [
            {
                "name": "PrimaryResult",
                "columns": [{"name": name} for name in columns],
                "rows": [[row.get(name) for name in columns] for row in rows],
            }
        ]
    }


def flat_export_record(**overrides) -> dict:
    base = {
        "timestamp": "2026-09-14T12:00:00Z",
        "name": "chat example-support-prod",
        "id": "row-1",
        "duration": 1450,
        "success": "True",
        "resultCode": "200",
        "customDimensions": {
            "gen_ai.system": "az.ai.openai",
            "gen_ai.request.model": "example-support-prod",
            "gen_ai.response.model": "gpt-4.1-2026-04-14",
            "gen_ai.usage.input_tokens": 4200,
            "gen_ai.usage.output_tokens": 380,
            "gen_ai.prompt": CANARY_PROMPT,
        },
        "customMeasurements": {},
    }
    base.update(overrides)
    return base


# -- shape handling ----------------------------------------------------------


def test_import_export_reads_the_logs_query_table_shape():
    payload = logs_query_result([flat_export_record()])
    result = import_export(payload)
    assert result.spans_mapped == 1
    assert result.spans_seen == 1
    record = result.records[0]
    assert record.provider == "azure_foundry"
    assert record.usage.input_tokens == 4200
    assert record.usage.output_tokens == 380
    assert record.status_code == 200


def test_import_export_reads_the_flat_continuous_export_shape():
    payload = [flat_export_record(id="row-a"), flat_export_record(id="row-b")]
    result = import_export(payload)
    assert result.spans_mapped == 2


def test_import_export_discards_content_attributes_like_the_otel_importer():
    payload = [flat_export_record()]
    result = import_export(payload)
    assert result.content_attributes_discarded >= 1
    assert not any(CANARY_PROMPT in json.dumps(record.model_dump(mode="json")) for record in result.records)


def test_import_export_derives_latency_from_timestamp_and_duration():
    payload = [flat_export_record(duration=2000)]
    result = import_export(payload)
    record = result.records[0]
    assert record.latency_ms == pytest.approx(2000, rel=0.01)


def test_import_export_marks_a_failed_call_from_the_success_column():
    payload = [flat_export_record(success="False", resultCode="429")]
    result = import_export(payload)
    record = result.records[0]
    assert record.status_code == 429


def test_import_export_handles_customdimensions_as_a_json_string():
    record = flat_export_record()
    record["customDimensions"] = json.dumps(record["customDimensions"])
    result = import_export([record])
    assert result.spans_mapped == 1
    assert result.records[0].usage.input_tokens == 4200


def test_import_export_ignores_rows_without_a_recognizable_model():
    payload = [{"timestamp": "2026-09-14T12:00:00Z", "name": "unrelated", "customDimensions": {"foo": "bar"}}]
    result = import_export(payload)
    assert result.spans_mapped == 0
    assert result.spans_skipped == 1


# -- file import ---------------------------------------------------------


def test_import_file_reads_a_logs_query_export_json(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps(logs_query_result([flat_export_record(), flat_export_record(id="row-2")])), encoding="utf-8")
    result = import_file(path)
    assert result.spans_mapped == 2


def test_import_file_reads_a_flat_export_jsonl(tmp_path):
    path = tmp_path / "export.jsonl"
    path.write_text(
        "\n".join(json.dumps(flat_export_record(id=f"row-{i}")) for i in range(3)) + "\n",
        encoding="utf-8",
    )
    result = import_file(path)
    assert result.spans_mapped == 3


def test_import_file_missing_input_raises(tmp_path):
    with pytest.raises(OtelImportError):
        import_file(tmp_path / "missing.json")


def test_import_file_empty_input_is_a_no_op(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("", encoding="utf-8")
    assert import_file(path).spans_mapped == 0


def test_import_file_malformed_jsonl_names_the_failing_line(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(json.dumps(flat_export_record()) + "\n{not json}\n", encoding="utf-8")
    with pytest.raises(OtelImportError) as error:
        import_file(path)
    assert "line 2" in str(error.value)


def test_imported_app_insights_records_analyze_offline(tmp_path):
    payload = logs_query_result([flat_export_record(id=f"row-{i}") for i in range(6)])
    result = import_export(payload)
    writer = TelemetryWriter(TelemetryConfig(output_dir=tmp_path / "traces", retention_days=0))
    assert write_import(result, writer) == 6
    records, source = load_records_many([str(tmp_path / "traces")])
    report = analyze(records, source, generated_at="2026-09-14T13:00:00Z")
    assert report.summary.requests_analyzed == 6
    assert CANARY_PROMPT not in json.dumps(report.model_dump(mode="json"))
