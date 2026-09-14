"""CLI collection and setup surface.

All tests run offline: no Azure credential is constructed, no model call is
made, and Azure-facing commands are exercised only up to the point where they
would require an SDK that is not installed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from tokenlens.cli import app

runner = CliRunner()

START_NANOS = int(datetime(2026, 9, 14, 12, 0, tzinfo=UTC).timestamp() * 1_000_000_000)
CANARY_PROMPT = "CANARY-PROMPT-cli-must-not-store-this"


def span(index: int) -> dict:
    return {
        "name": "chat example-support-prod",
        "spanId": f"span-{index}",
        "startTimeUnixNano": START_NANOS + index * 1_000_000_000,
        "endTimeUnixNano": START_NANOS + index * 1_000_000_000 + 900_000_000,
        "attributes": {
            "gen_ai.system": "az.ai.openai",
            "gen_ai.request.model": "example-support-prod",
            "gen_ai.response.model": "gpt-4.1",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 120,
            "tokenlens.deployment_mode": "global",
            "gen_ai.prompt": CANARY_PROMPT,
        },
        "status": {"code": "STATUS_CODE_OK"},
    }


def test_help_lists_the_three_collection_paths():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "smoke-test-foundry" in result.output
    assert "collect-foundry-metrics" in result.output
    assert "import-otel" in result.output
    assert "Analysis is always offline" in result.output


def test_doctor_reports_each_capability_without_calling_a_model(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "offline-analyzer=ready" in result.output
    assert "sdk-instrumentation" in result.output
    assert "opentelemetry" in result.output
    assert "foundry-monitor" in result.output
    assert "telemetry-output-dir=" in result.output
    assert "telemetry-content-capture=disabled" in result.output
    assert "configuration=not found" in result.output


def test_connect_foundry_writes_a_credential_free_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        [
            "connect-foundry",
            "--noninteractive",
            "--resource-group",
            "example-resource-group",
            "--account",
            "example-foundry-account",
            "--deployment",
            "example-support-prod",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "credentials-stored=none" in result.output
    assert "next: tokenlens-azure collect-foundry-metrics" in result.output
    config = (tmp_path / ".tokenlens.yml").read_text(encoding="utf-8")
    for forbidden in ("api_key", "access_token", "secret", "subscription_id:", "connection_string", "bearer"):
        assert forbidden not in config.casefold()
    assert "subscription_id_env: AZURE_SUBSCRIPTION_ID" in config
    assert "content_capture: false" in config
    assert (tmp_path / "tokenlens-traces" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    assert (tmp_path / "local-traces" / "foundry-metrics" / ".gitignore").exists()

    # Setup is repeatable.
    again = runner.invoke(app, ["connect-foundry", "--noninteractive", "--resource-group", "rg", "--account", "acct"])
    assert again.exit_code == 0


def test_connect_foundry_requires_explicit_scope_when_noninteractive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["connect-foundry", "--noninteractive"])
    assert result.exit_code != 0


def test_import_otel_writes_canonical_records_and_reports_discards(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "spans.json"
    source.write_text(json.dumps([span(index) for index in range(5)]), encoding="utf-8")
    result = runner.invoke(app, ["import-otel", str(source), "--output-dir", "traces"])
    assert result.exit_code == 0, result.output
    assert "spans_mapped=5" in result.output
    assert "content_attributes_discarded=5" in result.output
    assert "records-written=5" in result.output
    written = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "traces").glob("*.jsonl"))
    assert CANARY_PROMPT not in written
    assert written.count("model_request") == 5


def test_imported_telemetry_analyzes_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "spans.jsonl"
    source.write_text("\n".join(json.dumps(span(index)) for index in range(8)) + "\n", encoding="utf-8")
    assert runner.invoke(app, ["import-otel", str(source), "--output-dir", "traces", "--quiet"]).exit_code == 0
    result = runner.invoke(app, ["analyze", "traces", "--format", "json", "--output", "report.json"])
    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["requests_analyzed"] == 8
    assert CANARY_PROMPT not in json.dumps(payload)


def test_collect_requires_an_explicit_subscription(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    result = runner.invoke(
        app,
        ["collect-foundry-metrics", "--resource-group", "rg", "--account", "acct"],
    )
    assert result.exit_code != 0
    assert "subscription" in result.output.casefold()


def test_collect_rejects_unsupported_granularity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(
        app,
        ["collect-foundry-metrics", "--resource-group", "rg", "--account", "acct", "--granularity", "1h"],
    )
    assert result.exit_code != 0


def test_collect_fails_clearly_when_collector_extras_are_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setattr("tokenlens.foundry.azure_clients.has_package", lambda name: False)
    result = runner.invoke(
        app,
        ["collect-foundry-metrics", "--resource-group", "rg", "--account", "acct", "--days", "14"],
    )
    assert result.exit_code != 0
    assert "foundry-monitor" in result.output


def test_smoke_test_states_the_billable_call_and_needs_an_endpoint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("AZURE_OPENAI_ENDPOINT", "FOUNDRY_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    result = runner.invoke(app, ["smoke-test-foundry", "--deployment", "example-support-prod", "--yes"])
    assert "billable=this command makes exactly one billable model request" in result.output
    assert result.exit_code != 0


def test_smoke_test_rejects_an_unknown_api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example-endpoint.invalid")
    result = runner.invoke(
        app,
        ["smoke-test-foundry", "--deployment", "d", "--api", "cohere-native", "--yes"],
    )
    assert result.exit_code != 0


def test_import_otel_is_idempotent_and_reports_existing_records(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "spans.jsonl"
    source.write_text("\n".join(json.dumps(span(index)) for index in range(4)) + "\n", encoding="utf-8")
    first = runner.invoke(app, ["import-otel", str(source), "--output-dir", "traces"])
    assert first.exit_code == 0, first.output
    assert "records-written=4" in first.output
    assert "records-already-present=0" in first.output

    second = runner.invoke(app, ["import-otel", str(source), "--output-dir", "traces"])
    assert second.exit_code == 0, second.output
    assert "records-written=0" in second.output
    assert "records-already-present=4" in second.output
    written = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "traces").glob("*.jsonl"))
    assert written.count("model_request") == 4


def test_import_otel_accepts_jsonl_of_full_otlp_documents(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "otlp.jsonl"
    source.write_text(
        "\n".join(
            json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span(index)]}]}]}) for index in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["import-otel", str(source), "--output-dir", "traces"])
    assert result.exit_code == 0, result.output
    assert "spans_mapped=3" in result.output
    assert "records-written=3" in result.output


def test_collect_rejects_an_unknown_deployment_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")
    result = runner.invoke(
        app,
        [
            "collect-foundry-metrics",
            "--resource-group",
            "rg",
            "--account",
            "acct",
            "--deployment-mode",
            "datazone",
        ],
    )
    assert result.exit_code != 0
    assert "global, regional, or unknown" in result.output


def test_collect_passes_the_deployment_mode_when_no_deployment_is_named(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000")
    captured: dict = {}

    class StubResources:
        def list_metric_definitions(self, uri):
            return ["ProcessedPromptTokens", "GeneratedTokens", "AzureOpenAIRequests"]

    def fake_clients(subscription_id):
        return StubResources(), object()

    def fake_collect(**kwargs):
        captured.update(kwargs)
        from tokenlens.foundry.monitor import CollectionResult

        return CollectionResult(records=[], window_start=None, window_end=None)

    monkeypatch.setattr("tokenlens.cli._foundry_clients", fake_clients)
    monkeypatch.setattr("tokenlens.foundry.monitor.collect_metrics", fake_collect)
    result = runner.invoke(
        app,
        [
            "collect-foundry-metrics",
            "--resource-group",
            "rg",
            "--account",
            "acct",
            "--deployment-mode",
            "global",
            "--output-dir",
            "metrics",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["default_deployment_mode"] == "global"
    assert captured["deployments"] is None


GENERIC_MAPPING = """
version: 1
fields:
  timestamp: ts
  provider: provider
  deployment_name: deployment
  model_name: model
  status_code: status
  usage:
    input_tokens: usage.prompt_tokens
    output_tokens: usage.completion_tokens
defaults:
  deployment_mode: unknown
"""


def generic_row(index: int) -> dict:
    return {
        "ts": f"2026-09-14T12:0{index}:00Z",
        "provider": "azure_foundry",
        "deployment": "example-support-prod",
        "model": "gpt-4.1",
        "status": 200,
        "usage": {"prompt_tokens": 1000, "completion_tokens": 120},
        "prompt": CANARY_PROMPT,
    }


def test_help_lists_the_generic_and_app_insights_import_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "import-app-insights" in result.output
    assert "collect-app-insights" in result.output


def test_import_generic_writes_canonical_records_via_a_yaml_mapping(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mapping = tmp_path / "mapping.yml"
    mapping.write_text(GENERIC_MAPPING, encoding="utf-8")
    source = tmp_path / "app-logs.jsonl"
    source.write_text("\n".join(json.dumps(generic_row(i)) for i in range(5)) + "\n", encoding="utf-8")
    result = runner.invoke(app, ["import", str(source), "--mapping", str(mapping), "--output-dir", "traces"])
    assert result.exit_code == 0, result.output
    assert "rows_mapped=5" in result.output
    assert "records-written=5" in result.output
    written = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "traces").glob("*.jsonl"))
    assert CANARY_PROMPT not in written
    assert written.count("model_request") == 5


def test_import_generic_rejects_a_mapping_that_targets_content_shaped_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mapping = tmp_path / "mapping.yml"
    mapping.write_text(
        "version: 1\nfields:\n  timestamp: ts\n  provider: provider\n  deployment_name: deployment\n"
        "  model_name: prompt\n",
        encoding="utf-8",
    )
    source = tmp_path / "app-logs.jsonl"
    source.write_text(json.dumps(generic_row(0)) + "\n", encoding="utf-8")
    result = runner.invoke(app, ["import", str(source), "--mapping", str(mapping)])
    assert result.exit_code != 0


def test_import_generic_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mapping = tmp_path / "mapping.yml"
    mapping.write_text(GENERIC_MAPPING, encoding="utf-8")
    source = tmp_path / "app-logs.jsonl"
    source.write_text("\n".join(json.dumps(generic_row(i)) for i in range(3)) + "\n", encoding="utf-8")
    first = runner.invoke(app, ["import", str(source), "--mapping", str(mapping), "--output-dir", "traces"])
    assert "records-written=3" in first.output
    second = runner.invoke(app, ["import", str(source), "--mapping", str(mapping), "--output-dir", "traces"])
    assert "records-written=0" in second.output
    assert "records-already-present=3" in second.output


def app_insights_export(index: int) -> dict:
    return {
        "timestamp": f"2026-09-14T12:0{index}:00Z",
        "name": "chat example-support-prod",
        "id": f"row-{index}",
        "duration": 900,
        "success": "True",
        "resultCode": "200",
        "customDimensions": {
            "gen_ai.system": "az.ai.openai",
            "gen_ai.request.model": "example-support-prod",
            "gen_ai.response.model": "gpt-4.1",
            "gen_ai.usage.input_tokens": 1000,
            "gen_ai.usage.output_tokens": 120,
            "gen_ai.prompt": CANARY_PROMPT,
        },
    }


def test_import_app_insights_writes_canonical_records_and_reports_discards(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "app-insights-export.json"
    source.write_text(
        json.dumps(
            {
                "tables": [
                    {
                        "columns": [
                            {"name": name}
                            for name in ("timestamp", "name", "id", "duration", "success", "resultCode", "customDimensions")
                        ],
                        "rows": [
                            [row[name] for name in ("timestamp", "name", "id", "duration", "success", "resultCode", "customDimensions")]
                            for row in (app_insights_export(i) for i in range(4))
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["import-app-insights", str(source), "--output-dir", "traces"])
    assert result.exit_code == 0, result.output
    assert "spans_mapped=4" in result.output
    assert "records-written=4" in result.output
    written = "\n".join(path.read_text(encoding="utf-8") for path in (tmp_path / "traces").glob("*.jsonl"))
    assert CANARY_PROMPT not in written


def test_collect_app_insights_fails_clearly_when_extras_are_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("tokenlens.foundry.azure_clients.has_package", lambda name: False)
    result = runner.invoke(app, ["collect-app-insights", "--workspace-id", "workspace-id"])
    assert result.exit_code != 0
    assert "foundry-monitor" in result.output


def test_collect_app_insights_states_it_contacts_azure_before_failing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("tokenlens.foundry.azure_clients.has_package", lambda name: False)
    result = runner.invoke(app, ["collect-app-insights", "--workspace-id", "workspace-id"])
    assert "network=this command contacts Azure Monitor Logs" in result.output
