"""End-to-end first-run onboarding with every Azure boundary mocked.

The walkthrough this test locks down is: install readiness (`doctor`), local
setup (`connect-foundry`), subscription fallback to the Azure CLI's selected
subscription, account-location lookup, regional metrics endpoint construction,
deployment inventory enrichment, and the next command the operator should run.

Nothing here contacts Azure: no SDK is imported, no credential is constructed,
and every value is synthetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from tokenlens.cli import app
from tokenlens.foundry.azure_clients import metrics_endpoint_for_location
from tokenlens.foundry.monitor import CollectionResult
from tokenlens.telemetry.schema import BucketMetrics, MetricBucketRecord

runner = CliRunner()

WINDOW_START = datetime(2026, 9, 1, tzinfo=UTC)


class StubResourceClient:
    """Discovery surface only. Constructing a metrics client here would be a bug."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_account(self, resource_group: str, account: str) -> dict:
        self.calls.append("get_account")
        return {"name": account, "kind": "AIServices", "location": "East US 2"}

    def list_metric_definitions(self, uri: str) -> list[dict]:
        self.calls.append("list_metric_definitions")
        return [
            {"name": "InputTokens", "dimensions": ["ModelDeploymentName", "ModelName", "ModelVersion"]},
            {"name": "OutputTokens", "dimensions": ["ModelDeploymentName", "ModelName", "ModelVersion"]},
            {"name": "ModelRequests", "dimensions": ["ModelDeploymentName", "StatusCode"]},
            {"name": "SuccessfulCalls", "dimensions": ["ApiName", "StatusCode"]},
        ]

    def list_deployments(self, subscription_id: str, resource_group: str, account: str) -> list[dict]:
        self.calls.append("list_deployments")
        return [{"name": "chat-route", "model": "gpt-4.1", "model_version": "2026-04-14", "sku": "GlobalStandard"}]


@pytest.fixture()
def onboarding(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    monkeypatch.delenv("TOKENLENS_METRICS_ENDPOINT", raising=False)
    monkeypatch.setattr(
        "tokenlens.cli._azure_cli_subscription",
        lambda: "00000000-0000-0000-0000-000000000000",
    )
    state: dict = {"resources": StubResourceClient(), "endpoints": [], "collect": {}}
    monkeypatch.setattr("tokenlens.cli._foundry_resource_client", lambda subscription: state["resources"])

    def metrics_client(endpoint: str):
        state["endpoints"].append(endpoint)
        return object()

    monkeypatch.setattr("tokenlens.cli._foundry_metrics_client", metrics_client)

    def fake_collect(**kwargs):
        state["collect"] = kwargs
        record = MetricBucketRecord(
            event_id="synthetic-onboarding-bucket",
            timestamp=WINDOW_START,
            deployment_name="chat-route",
            model_name="gpt-4.1",
            model_version="2026-04-14",
            deployment_mode="global",
            metrics=BucketMetrics(input_tokens=18, output_tokens=9, requests=2, successful_requests=1, failed_requests=1, throttled_requests=0),
            metric_provenance={"input_tokens": "InputTokens", "requests": "ModelRequests"},
            status_codes={"200": 1, "400": 1},
            outcome_coverage="complete",
            window_start=WINDOW_START,
            window_end=WINDOW_START + timedelta(days=14),
            expected_buckets=4032,
        )
        return CollectionResult(
            records=[record],
            window_start=WINDOW_START,
            window_end=WINDOW_START + timedelta(days=14),
            active_buckets=1,
            outcome_coverage_complete=True,
            status_codes={"200": 1, "400": 1},
            excluded_metrics={"SuccessfulCalls": "metric definition does not support a ModelDeploymentName filter"},
        )

    monkeypatch.setattr("tokenlens.foundry.monitor.collect_metrics", fake_collect)
    return state


def test_first_run_walkthrough_is_executable_in_order(tmp_path, onboarding):
    # 1. Readiness is reported before anything contacts Azure.
    doctor = runner.invoke(app, ["doctor"])
    assert doctor.exit_code == 0, doctor.output
    assert "offline-analyzer=ready" in doctor.output
    assert "foundry-monitor (tokenlens-azure[foundry-monitor])=" in doctor.output
    assert "configuration=not found (run tokenlens-azure connect-foundry)" in doctor.output

    # 2. Local setup writes a credential-free configuration.
    connect = runner.invoke(
        app,
        ["connect-foundry", "--noninteractive", "--resource-group", "rg", "--account", "acct", "--deployment", "chat-route"],
    )
    assert connect.exit_code == 0, connect.output
    assert "credentials-stored=none" in connect.output
    assert "next: tokenlens-azure collect-foundry-metrics" in connect.output
    config = (tmp_path / ".tokenlens.yml").read_text(encoding="utf-8")
    assert "subscription_id_env: AZURE_SUBSCRIPTION_ID" in config
    assert "chat-route" in config

    # 3. Collection falls back to the Azure CLI's selected subscription and
    #    derives the regional metrics endpoint from the account location.
    collect = runner.invoke(
        app,
        ["collect-foundry-metrics", "--resource-group", "rg", "--account", "acct", "--days", "14", "--deployment", "chat-route", "--deployment-mode", "global", "--output-dir", "metrics"],
    )
    assert collect.exit_code == 0, collect.output
    assert "subscription-source=azure-cli-active" in collect.output
    assert "metrics-endpoint-source=account-location" in collect.output
    assert onboarding["endpoints"] == [metrics_endpoint_for_location("East US 2")]
    assert onboarding["collect"]["subscription_id"] == "00000000-0000-0000-0000-000000000000"
    assert onboarding["collect"]["deployments"] == ["chat-route"]
    assert list(onboarding["collect"]["deployment_inventory"])[0]["model"] == "gpt-4.1"
    assert "deployment-inventory=1 deployment(s)" in collect.output
    assert "excluded_metrics={'SuccessfulCalls'" in collect.output
    assert "records-written=1" in collect.output
    assert "next: tokenlens-azure analyze metrics --format html --open" in collect.output

    # 4. The printed next command runs offline and produces a report.
    analyze = runner.invoke(app, ["analyze", "metrics", "--format", "html", "--output", "report.html"])
    assert analyze.exit_code == 0, analyze.output
    rendered = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "chat-route" in rendered
    assert "gpt-4.1" in rendered
    # Requests come from the metric, and the report never prints a local path.
    assert ">2</div>" in rendered
    assert str(tmp_path) not in rendered


def test_resource_discovery_never_constructs_a_metrics_client(tmp_path, onboarding):
    resources = runner.invoke(app, ["list-foundry-deployments", "--resource-group", "rg", "--account", "acct"])
    assert resources.exit_code == 0, resources.output
    assert "deployment=chat-route model=gpt-4.1" in resources.output
    assert onboarding["endpoints"] == []


def test_metrics_endpoint_override_stays_an_advanced_option(tmp_path, onboarding, monkeypatch):
    monkeypatch.setenv("TOKENLENS_METRICS_ENDPOINT", "https://sovereign.metrics.example.net")
    result = runner.invoke(
        app,
        ["collect-foundry-metrics", "--resource-group", "rg", "--account", "acct", "--output-dir", "metrics"],
    )
    assert result.exit_code == 0, result.output
    assert "metrics-endpoint-source=configured-override" in result.output
    assert onboarding["endpoints"] == ["https://sovereign.metrics.example.net"]
