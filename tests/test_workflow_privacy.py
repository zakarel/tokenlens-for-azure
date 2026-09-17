"""Privacy and security gates for the guided Foundry workflow.

Fake secrets, endpoints, resource identifiers, prompts, and absolute paths are
seeded everywhere they could plausibly travel, and asserted absent from the
configuration, run state, terminal output, exceptions, and the generated report.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tokenlens import foundry_cli
from tokenlens.analyzer import analyze
from tokenlens.cli import app
from tokenlens.foundry_workflow.configuration import load_run_state
from tokenlens.foundry_workflow.models import WorkflowError
from tokenlens.foundry_workflow.orchestration import WorkflowServices, collect_deployments
from tokenlens.ingest import iter_records
from tokenlens.reports import report_html
from tokenlens.telemetry.schema import MetricBucketRecord

from test_foundry_workflow import SUBSCRIPTION, StubResources, collector

runner = CliRunner()

CANARIES = {
    "prompt": "CANARY-PROMPT-must-never-be-stored",
    "response": "CANARY-RESPONSE-must-never-be-stored",
    "key": "CANARY-KEY-sk-0000000000000000",
    "token": "CANARY-ACCESS-TOKEN-eyJhbGciOi",
    "tenant": "CANARY-TENANT-99999999-8888-7777-6666-555555555555",
    "request_id": "CANARY-REQUEST-ID-abcdef123456",
    "user": "canary.person@example.invalid",
    "ip": "203.0.113.42",
}


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "user-config"))
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    state: dict = {"collect": collector(), "resources": StubResources()}

    def _services() -> WorkflowServices:
        return WorkflowServices(
            resource_client=lambda subscription: state["resources"],
            metrics_client=lambda endpoint: {"endpoint": endpoint},
            collect=state["collect"],
            subscriptions=lambda: [],
            open_report=lambda path: True,
            now=lambda: datetime(2026, 9, 15, tzinfo=UTC),
            missing_packages=lambda: [],
        )

    monkeypatch.setattr(foundry_cli, "_services", _services)
    return state


COLLECT_ARGS = [
    "foundry",
    "collect",
    "--subscription",
    SUBSCRIPTION,
    "--resource-group",
    "example-rg",
    "--account",
    "example-foundry-account",
    "--deployment",
    "reasoning-prod",
    "--days",
    "14",
]


def test_no_canary_reaches_configuration_run_state_or_terminal_output(cli):
    result = runner.invoke(app, COLLECT_ARGS)
    assert result.exit_code == 0, result.output
    surfaces = {
        "terminal": result.output,
        "configuration": Path(".tokenlens.yml").read_text(encoding="utf-8"),
        "run-state": json.dumps(load_run_state().model_dump(mode="json")),
        "report": next(Path("reports").glob("*.html")).read_text(encoding="utf-8"),
    }
    for name, content in surfaces.items():
        for label, canary in CANARIES.items():
            assert canary not in content, f"{label} canary leaked into {name}"


def test_the_report_never_exposes_a_subscription_endpoint_or_request_id(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    html = next(Path("reports").glob("*.html")).read_text(encoding="utf-8")
    assert SUBSCRIPTION not in html
    assert "services.ai.azure.com" not in html
    assert "/subscriptions/" not in html
    assert "metrics.monitor.azure.com" not in html
    # Absolute local paths would carry a username.
    assert str(Path.home()) not in html


def test_collected_telemetry_is_contentless(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    # Each run writes into its own directory beneath the configured output dir.
    written = list(Path("local-traces/foundry-metrics").rglob("*.jsonl"))
    assert written
    for path in written:
        for line in path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            record = MetricBucketRecord.model_validate(payload)
            assert record.record_type == "foundry_metric_bucket"
            # The canonical schema has nowhere to put content, and nothing
            # resource-identifying is written beside it.
            assert "messages" not in payload
            assert "prompt" not in json.dumps(payload).casefold()
            assert SUBSCRIPTION not in json.dumps(payload)


def test_a_collection_failure_message_carries_no_response_body_or_identifier(cli):
    class Boom:
        def __init__(self) -> None:
            self.status_code = 403

    def failing(**kwargs):
        from tokenlens.foundry.monitor import AuthorizationError

        raise AuthorizationError(
            "Azure Monitor rejected the request for this resource. Confirm the signed-in principal "
            "has Monitoring Reader on the selected account."
        )

    cli["collect"] = failing
    result = runner.invoke(app, COLLECT_ARGS)
    assert result.exit_code == foundry_cli.EXIT_FAILED
    assert "authorization" in result.output
    assert "Monitoring Reader" in result.output
    for canary in CANARIES.values():
        assert canary not in result.output
    assert SUBSCRIPTION not in result.output


def test_workflow_errors_never_echo_a_credential(cli, monkeypatch):
    def exploding(**kwargs):
        raise RuntimeError(f"boom with {CANARIES['key']}")

    cli["collect"] = exploding
    result = runner.invoke(app, COLLECT_ARGS)
    assert result.exit_code == foundry_cli.EXIT_FAILED
    # The classifier reports the error class, never the provider message.
    assert "unexpected" in result.output
    assert CANARIES["key"] not in result.output


def test_prompt_content_in_request_telemetry_is_never_rendered(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = {
        "timestamp": "2026-09-01T00:00:00Z",
        "deployment_name": "support-prod",
        "model_name": "phi-4",
        "deployment_mode": "global",
        "messages": [{"role": "user", "content": CANARIES["prompt"]}],
        "usage": {"input_tokens": 1000, "output_tokens": 200},
        "status_code": 200,
        "metadata": {"workload": "support-assistant"},
    }
    record = next(iter_records(io.StringIO(json.dumps(raw) + "\n")))
    report = analyze([record], "fixture", generated_at="2026-09-15T08:00:00Z")
    html = report_html(report)
    assert CANARIES["prompt"] not in html
    assert "support-assistant" in html


def test_a_workload_template_carries_no_cost_or_identifier(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    assert runner.invoke(app, ["foundry", "workloads", "export-template"]).exit_code == 0
    template = Path("workloads-template.yml").read_text(encoding="utf-8")
    payload = yaml.safe_load(template)
    assert set(payload) == {"workloads"}
    for canary in CANARIES.values():
        assert canary not in template
    assert SUBSCRIPTION not in template


def test_status_output_reveals_no_secret_or_full_identifier(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    result = runner.invoke(app, ["foundry", "status"])
    assert result.exit_code == 0
    for canary in CANARIES.values():
        assert canary not in result.output
    assert SUBSCRIPTION not in result.output
    assert str(Path.home()) not in result.output


def test_the_workflow_never_contacts_an_inference_endpoint(cli):
    calls: list[str] = []

    class Recording(StubResources):
        def get_account(self, resource_group: str, account: str) -> dict:
            calls.append("get_account")
            return super().get_account(resource_group, account)

    cli["resources"] = Recording()
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    # Discovery and metrics only: no OpenAI/Anthropic client is ever built.
    assert "get_account" in calls
    assert "list_deployments" in cli["resources"].calls
    assert all(call in {"list_accounts", "list_deployments", "get_account", "list_metric_definitions"} for call in cli["resources"].calls)


def test_local_output_directories_are_created_private(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    metrics_dir = Path("local-traces/foundry-metrics")
    assert metrics_dir.is_dir()
    assert metrics_dir.stat().st_mode & 0o077 == 0
    run_dir = next((metrics_dir / "runs").iterdir())
    assert run_dir.stat().st_mode & 0o077 == 0


def test_the_run_directory_is_a_safe_relative_path(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    line = next(
        item for item in runner.invoke(app, ["foundry", "status"]).output.splitlines()
        if item.startswith("last-run-directory=")
    )
    value = line.split("=", 1)[1]
    assert value.startswith("local-traces/foundry-metrics/runs/run-")
    assert str(Path.home()) not in value
    assert SUBSCRIPTION not in value
