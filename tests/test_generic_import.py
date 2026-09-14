"""Generic YAML-mapping import behaviour.

All fixtures are synthetic. The mapping loader must never execute code (only
``yaml.safe_load`` is used) and must never let a mapping route content or
credential-shaped source fields into canonical telemetry.
"""

from __future__ import annotations

import json

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import load_records_many
from tokenlens.telemetry import TelemetryConfig, TelemetryWriter
from tokenlens.telemetry.generic_import import (
    GenericImportError,
    MappingError,
    import_file,
    import_rows,
    load_mapping,
    map_row,
    write_import,
)

CANARY_PROMPT = "CANARY-PROMPT-generic-import-should-never-store-this"

VALID_MAPPING = """
version: 1
fields:
  timestamp: ts
  provider: provider
  deployment_name: deployment
  model_name: model
  service_tier: tier
  deployment_mode: mode
  status_code: status
  latency_ms: latency_ms
  usage:
    input_tokens: usage.prompt_tokens
    output_tokens: usage.completion_tokens
    cached_tokens: usage.cached_tokens
  scope:
    workload: workload_label
defaults:
  deployment_mode: unknown
  service_tier: standard
timestamp_format: auto
"""


def row(**overrides):
    base = {
        "ts": "2026-09-01T12:00:00Z",
        "provider": "azure_foundry",
        "deployment": "support-prod",
        "model": "gpt-4.1",
        "tier": "standard",
        "mode": "global",
        "usage": {"prompt_tokens": 420, "completion_tokens": 96, "cached_tokens": 128},
        "latency_ms": 812,
        "status": 200,
        "workload_label": "support-assistant",
        "prompt": CANARY_PROMPT,
    }
    base.update(overrides)
    return base


def write_mapping(tmp_path, text: str = VALID_MAPPING):
    path = tmp_path / "mapping.yml"
    path.write_text(text, encoding="utf-8")
    return path


def write_log(tmp_path, rows, name: str = "app-logs.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


# -- mapping validation -----------------------------------------------------


def test_load_mapping_accepts_a_valid_file(tmp_path):
    mapping = load_mapping(write_mapping(tmp_path))
    assert mapping.fields["provider"] == "provider"
    assert mapping.usage["input_tokens"] == "usage.prompt_tokens"
    assert mapping.scope["workload"] == "workload_label"
    assert mapping.defaults == {"deployment_mode": "unknown", "service_tier": "standard"}


def test_load_mapping_rejects_unknown_top_level_key(tmp_path):
    path = tmp_path / "mapping.yml"
    path.write_text(VALID_MAPPING + "\nplugin: some.module:function\n", encoding="utf-8")
    with pytest.raises(MappingError):
        load_mapping(path)


def test_load_mapping_rejects_unknown_field_name(tmp_path):
    path = tmp_path / "mapping.yml"
    path.write_text(
        "version: 1\nfields:\n  timestamp: ts\n  provider: provider\n  deployment_name: deployment\n"
        "  model_name: model\n  not_a_real_field: whatever\n",
        encoding="utf-8",
    )
    with pytest.raises(MappingError):
        load_mapping(path)


@pytest.mark.parametrize(
    "bad_path",
    ["prompt", "messages.0.content", "system_prompt", "authorization", "api_key", "tool_arguments"],
)
def test_load_mapping_rejects_content_or_credential_shaped_source_paths(tmp_path, bad_path):
    path = tmp_path / "mapping.yml"
    path.write_text(
        "version: 1\nfields:\n  timestamp: ts\n  provider: provider\n  deployment_name: deployment\n"
        f"  model_name: {bad_path}\n",
        encoding="utf-8",
    )
    with pytest.raises(MappingError):
        load_mapping(path)


def test_load_mapping_requires_the_required_fields(tmp_path):
    path = tmp_path / "mapping.yml"
    path.write_text("version: 1\nfields:\n  timestamp: ts\n", encoding="utf-8")
    with pytest.raises(MappingError):
        load_mapping(path)


def test_load_mapping_rejects_arbitrary_yaml_object_construction(tmp_path):
    """`yaml.safe_load` must reject unsafe tags outright; no code can run."""
    path = tmp_path / "mapping.yml"
    path.write_text(
        "version: 1\n"
        "fields: !!python/object/apply:os.system ['echo unsafe']\n",
        encoding="utf-8",
    )
    with pytest.raises(MappingError):
        load_mapping(path)


def test_load_mapping_rejects_a_non_mapping_document(tmp_path):
    path = tmp_path / "mapping.yml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(MappingError):
        load_mapping(path)


def test_load_mapping_missing_file_raises(tmp_path):
    with pytest.raises(MappingError):
        load_mapping(tmp_path / "does-not-exist.yml")


# -- row mapping -------------------------------------------------------------


def test_map_row_produces_a_canonical_record_without_the_content_field(tmp_path):
    mapping = load_mapping(write_mapping(tmp_path))
    record = map_row(row(), mapping)
    assert record is not None
    assert record.source == "import"
    assert record.provider == "azure_foundry"
    assert record.deployment_name == "support-prod"
    assert record.model_name == "gpt-4.1"
    assert record.usage.input_tokens == 420
    assert record.usage.output_tokens == 96
    assert record.usage.cached_tokens == 128
    assert record.status_code == 200
    assert record.latency_ms == 812
    assert record.scope.workload == "support-assistant"
    assert CANARY_PROMPT not in record.model_dump_json()


def test_map_row_skips_a_row_missing_a_required_field(tmp_path):
    mapping = load_mapping(write_mapping(tmp_path))
    incomplete = row()
    del incomplete["model"]
    assert map_row(incomplete, mapping) is None


def test_map_row_drops_an_unrecognized_error_category_instead_of_guessing(tmp_path):
    mapping_with_error = load_mapping(
        write_mapping(
            tmp_path,
            VALID_MAPPING.replace("status_code: status", "status_code: status\n  error_category: err"),
        )
    )
    record = map_row(row(err="not-a-real-category"), mapping_with_error)
    assert record is not None
    assert record.error_category is None

    valid_record = map_row(row(err="rate_limited"), mapping_with_error)
    assert valid_record.error_category == "rate_limited"


# -- file import + idempotency ------------------------------------------------


def test_import_rows_is_idempotent(tmp_path):
    mapping = load_mapping(write_mapping(tmp_path))
    rows = [row(ts=f"2026-09-01T12:0{i}:00Z") for i in range(3)]
    first = import_rows(rows, mapping)
    assert first.rows_mapped == 3
    second = import_rows(rows, mapping)
    assert second.rows_mapped == 3
    assert {r.event_id for r in first.records} == {r.event_id for r in second.records}


def test_import_file_reads_jsonl_and_never_stores_content(tmp_path):
    mapping_path = write_mapping(tmp_path)
    log_path = write_log(tmp_path, [row(ts=f"2026-09-01T12:0{i}:00Z") for i in range(4)])
    result = import_file(log_path, mapping_path)
    assert result.rows_seen == 4
    assert result.rows_mapped == 4
    assert result.rows_skipped == 0
    writer = TelemetryWriter(TelemetryConfig.from_env(output_dir=tmp_path / "traces"))
    written = write_import(result, writer)
    assert written == 4
    contents = "\n".join(p.read_text(encoding="utf-8") for p in (tmp_path / "traces").glob("*.jsonl"))
    assert CANARY_PROMPT not in contents
    assert contents.count("model_request") == 4


def test_import_file_missing_input_raises(tmp_path):
    mapping_path = write_mapping(tmp_path)
    with pytest.raises(GenericImportError):
        import_file(tmp_path / "missing.jsonl", mapping_path)


def test_imported_generic_records_analyze_offline(tmp_path):
    mapping_path = write_mapping(tmp_path)
    log_path = write_log(tmp_path, [row(ts=f"2026-09-01T12:0{i}:00Z") for i in range(5)])
    result = import_file(log_path, mapping_path)
    writer = TelemetryWriter(TelemetryConfig.from_env(output_dir=tmp_path / "traces"))
    write_import(result, writer)
    records, source = load_records_many([str(tmp_path / "traces")])
    report = analyze(records, source, generated_at="2026-09-14T13:00:00Z")
    assert report.summary.requests_analyzed == 5
    assert CANARY_PROMPT not in json.dumps(report.model_dump(mode="json"))
