"""CLI transcript tests for the guided Foundry workflow and pricing commands.

Every assertion is on meaningful terminal output, not ANSI styling. No Azure
client is constructed, no credential is created, and no model call is made.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from tokenlens import foundry_cli
from tokenlens.cli import app
from tokenlens.foundry_workflow.configuration import customer_catalog_path, run_state_path

from test_foundry_workflow import (  # noqa: F401 - shared synthetic doubles
    PRICED_MODEL,
    SUBSCRIPTION,
    UNPRICED_MODEL,
    StubResources,
    collector,
)

runner = CliRunner()


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    # The user-local directory always sits outside the working tree.
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "user-config"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    state: dict = {"opened": [], "collect": collector(), "resources": StubResources()}

    from tokenlens.foundry_workflow.orchestration import WorkflowServices

    def _services() -> WorkflowServices:
        return WorkflowServices(
            resource_client=lambda subscription: state["resources"],
            metrics_client=lambda endpoint: {"endpoint": endpoint},
            collect=state["collect"],
            subscriptions=lambda: [],
            open_report=lambda path: state["opened"].append(path) or True,
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
    "--deployment",
    "coding-prod",
    "--days",
    "14",
    "--format",
    "html",
    "--output-dir",
    "reports",
]


def test_root_help_points_at_the_guided_workflow_first():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "tokenlens-azure foundry" in result.output
    assert "Guided setup" in result.output
    # The advanced commands remain available.
    assert "collect-foundry-metrics" in result.output
    assert "smoke-test-foundry" in result.output


def test_foundry_help_lists_the_workflow_commands():
    result = runner.invoke(app, ["foundry", "--help"])
    assert result.exit_code == 0
    for command in ("configure", "collect", "refresh", "status", "pricing", "workloads"):
        assert command in result.output


def test_bare_foundry_in_a_non_tty_prints_help_instead_of_hanging(cli):
    result = runner.invoke(app, ["foundry"], input="")
    assert result.exit_code == 0
    assert "noninteractive=" in result.output
    assert "foundry collect --subscription" in result.output


def test_noninteractive_collect_produces_a_report_and_a_readiness_table(cli):
    result = runner.invoke(app, COLLECT_ARGS)
    assert result.exit_code == 0, result.output
    assert "azure-access=this command queries Azure Resource Manager and Azure Monitor" in result.output
    assert "2 / 2 deployments succeeded" in result.output
    assert "14-day window" in result.output
    assert "Deployment" in result.output and "PTU evidence" in result.output
    assert "model-identity-coverage=100%" in result.output
    assert "token-pricing-coverage=100%" in result.output
    assert "business-workload-identity-coverage=0%" in result.output
    assert "technical-workloads=2" in result.output
    assert "report=reports/tokenlens-report-" in result.output
    # Automation never opens a browser unless asked.
    assert "browser-open=not requested or unavailable" in result.output
    assert cli["opened"] == []
    assert list(Path("reports").glob("*.html"))


def test_collect_never_prints_a_full_local_path_or_subscription_id(cli):
    result = runner.invoke(app, COLLECT_ARGS)
    assert result.exit_code == 0
    assert SUBSCRIPTION not in result.output
    assert str(Path.home()) not in result.output
    assert "services.ai.azure.com" not in result.output


def test_partial_collection_uses_the_documented_partial_exit_code(cli):
    cli["collect"] = collector(failing={"coding-prod"})
    result = runner.invoke(app, COLLECT_ARGS)
    assert result.exit_code == foundry_cli.EXIT_PARTIAL
    assert "1 / 2 deployments succeeded" in result.output
    assert "coding-prod" in result.output
    assert "metric_unavailable" in result.output
    # A partial run still generates a usable report.
    assert "report=reports/tokenlens-report-" in result.output


def test_a_collection_with_no_successes_exits_nonzero_and_generates_no_report(cli):
    cli["collect"] = collector(failing={"reasoning-prod", "coding-prod"})
    result = runner.invoke(app, COLLECT_ARGS)
    assert result.exit_code == foundry_cli.EXIT_FAILED
    assert "Collection failed" in result.output
    assert "report=not generated because no deployment succeeded" in result.output


def test_noninteractive_collect_requires_explicit_resource_values(cli):
    result = runner.invoke(app, ["foundry", "collect"])
    assert result.exit_code == foundry_cli.EXIT_FAILED
    assert "error=" in result.output
    assert "subscription" in result.output.casefold()


def test_configure_reports_detected_metadata_and_stores_no_credential(cli):
    result = runner.invoke(
        app,
        [
            "foundry",
            "configure",
            "--noninteractive",
            "--subscription",
            SUBSCRIPTION,
            "--resource-group",
            "example-rg",
            "--account",
            "example-foundry-account",
            "--deployment",
            "reasoning-prod",
            "--deployment",
            "compact-prod",
            "--days",
            "7",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "deployment=reasoning-prod" in result.output
    assert "mode=Global Standard" in result.output
    assert "family=azure_openai api=openai" in result.output
    assert "mode=Regional Standard" in result.output
    assert "family=partner_model api=openai" in result.output
    assert "lookback-days=7" in result.output
    assert "pricing-resolved=1/2" in result.output
    assert "credentials-stored=none" in result.output


def test_refresh_reuses_saved_settings_and_is_idempotent(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    first = runner.invoke(app, ["foundry", "refresh", "--no-open"])
    assert first.exit_code == 0, first.output
    assert "records-written=0" in first.output
    assert "already-present=8" in first.output


def test_refresh_without_configuration_fails_with_an_actionable_error(cli):
    result = runner.invoke(app, ["foundry", "refresh"])
    assert result.exit_code == foundry_cli.EXIT_FAILED
    assert "foundry configure" in result.output


def test_status_reports_coverage_without_leaking_identifiers(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    result = runner.invoke(app, ["foundry", "status"])
    assert result.exit_code == 0, result.output
    assert "configured=True" in result.output
    assert "account=example-foundry-account" in result.output
    assert "deployments=reasoning-prod, coding-prod" in result.output
    assert "lookback-days=14" in result.output
    assert "collection-status=succeeded" in result.output
    assert "pricing-resolved=2/2" in result.output
    assert "last-report=reports/tokenlens-report-" in result.output
    # Only the shortened subscription form is ever printed.
    assert SUBSCRIPTION not in result.output
    assert SUBSCRIPTION[:8] in result.output
    assert "az login" not in result.output or "token" not in result.output


def test_status_json_is_machine_readable(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    result = runner.invoke(app, ["foundry", "status", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["configured"] is True
    assert payload["deployments"] == ["reasoning-prod", "coding-prod"]
    assert payload["pricing-resolved"] == "2/2"
    assert SUBSCRIPTION not in json.dumps(payload)


def test_foundry_pricing_separates_identity_from_a_missing_rate(cli):
    assert (
        runner.invoke(
            app,
            [
                "foundry",
                "configure",
                "--noninteractive",
                "--subscription",
                SUBSCRIPTION,
                "--resource-group",
                "example-rg",
                "--account",
                "example-foundry-account",
                "--deployment",
                "reasoning-prod",
                "--deployment",
                "compact-prod",
            ],
        ).exit_code
        == 0
    )
    result = runner.invoke(app, ["foundry", "pricing"])
    assert result.exit_code == 0, result.output
    assert "reasoning-prod" in result.output and "Exact public rate" in result.output
    assert "compact-prod" in result.output and "Model identified, rate unavailable" in result.output
    assert "unresolved=1 of 2 deployment(s)" in result.output
    assert "TokenLens never guesses a rate" in result.output
    assert f"suggested override key {UNPRICED_MODEL}" in result.output
    # `unknown` is never offered as a pricing override key.
    assert "suggested override key unknown" not in result.output


def test_workloads_list_and_status_describe_the_default_technical_workloads(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    listed = runner.invoke(app, ["foundry", "workloads", "list"])
    assert listed.exit_code == 0, listed.output
    assert "workload=deployment:reasoning-prod" in listed.output
    assert "scope=technical type=ai_deployment" in listed.output
    assert "source=system_default status=needs_configuration" in listed.output
    assert "allocation=deployment_total" in listed.output

    status = runner.invoke(app, ["foundry", "workloads", "status"])
    assert status.exit_code == 0, status.output
    assert "technical-workloads=2" in status.output
    assert "technical-coverage=2/2 deployments" in status.output
    assert "business-workloads=0" in status.output
    assert "business-identity-configured=0/2 deployments" in status.output
    assert "deployments-needing-configuration=coding-prod, reasoning-prod" in status.output


def test_workloads_export_template_is_credential_free(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    result = runner.invoke(app, ["foundry", "workloads", "export-template"])
    assert result.exit_code == 0, result.output
    template = Path("workloads-template.yml").read_text(encoding="utf-8")
    payload = yaml.safe_load(template)
    assert payload["workloads"]["defaults"]["create_for_each_deployment"] is True
    assert [item["name"] for item in payload["workloads"]["mappings"]] == [
        "reasoning-prod",
        "coding-prod",
    ]
    assert SUBSCRIPTION not in template
    for forbidden in ("$", "usd", "prompt", "request_id", "tenant_id", "subscription"):
        assert forbidden not in template.casefold()


def test_workloads_configure_is_explicit_about_being_interactive(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    result = runner.invoke(app, ["foundry", "workloads", "configure"])
    assert result.exit_code == foundry_cli.EXIT_FAILED
    assert "workloads.mappings" in result.output
    assert "export-template" in result.output


# --- Pricing catalogs -------------------------------------------------------


def test_pricing_status_is_offline_and_reports_the_deferred_sync(cli):
    result = runner.invoke(app, ["pricing", "status"])
    assert result.exit_code == 0, result.output
    assert "packaged-catalog=" in result.output
    assert "customer-catalog=not configured" in result.output
    assert "public-sync=deferred" in result.output


def test_pricing_sync_is_deferred_and_never_fabricates_a_rate(cli):
    result = runner.invoke(app, ["pricing", "sync"])
    assert result.exit_code == foundry_cli.EXIT_DEFERRED
    assert "pricing-sync=deferred" in result.output
    assert "network=no request was made" in result.output
    assert "deterministic parser" in result.output
    assert "pricing set-rate" in result.output


def test_pricing_verify_validates_local_catalogs_offline(cli):
    result = runner.invoke(app, ["pricing", "verify"])
    assert result.exit_code == 0, result.output
    assert "catalogs=valid" in result.output


def test_pricing_set_rate_writes_a_labelled_customer_override(cli):
    result = runner.invoke(
        app,
        [
            "pricing",
            "set-rate",
            "--model",
            UNPRICED_MODEL,
            "--input-per-million",
            "0.4",
            "--output-per-million",
            "1.2",
            "--effective-from",
            "2026-01-01",
            "--note",
            "Negotiated enterprise agreement",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "confidence=customer_override" in result.output
    catalog = yaml.safe_load(customer_catalog_path().read_text(encoding="utf-8"))
    entry = catalog["prices"][0]
    assert entry["model"] == UNPRICED_MODEL
    assert entry["confidence"] == "customer_override"
    assert entry["input_per_million"] == 0.4
    assert catalog["currency"] == "USD"
    # The catalog lives outside the repository with user-only permissions.
    assert customer_catalog_path().stat().st_mode & 0o777 == 0o600
    assert Path.cwd() not in customer_catalog_path().parents


def test_a_customer_rate_resolves_previously_unpriced_deployments(cli):
    assert (
        runner.invoke(
            app,
            [
                "pricing",
                "set-rate",
                "--model",
                UNPRICED_MODEL,
                "--input-per-million",
                "0.4",
                "--output-per-million",
                "1.2",
                "--effective-from",
                "2026-01-01",
                "--yes",
            ],
        ).exit_code
        == 0
    )
    result = runner.invoke(app, COLLECT_ARGS + ["--deployment", "compact-prod"])
    assert result.exit_code == 0, result.output
    assert "token-pricing-coverage=100%" in result.output


def test_a_second_currency_is_rejected_rather_than_converted(cli):
    assert (
        runner.invoke(
            app,
            [
                "pricing",
                "set-rate",
                "--model",
                UNPRICED_MODEL,
                "--input-per-million",
                "0.4",
                "--output-per-million",
                "1.2",
                "--effective-from",
                "2026-01-01",
                "--yes",
            ],
        ).exit_code
        == 0
    )
    result = runner.invoke(
        app,
        [
            "pricing",
            "set-rate",
            "--model",
            "another-model-test-synthetic",
            "--currency",
            "EUR",
            "--input-per-million",
            "0.4",
            "--output-per-million",
            "1.2",
            "--effective-from",
            "2026-01-01",
            "--yes",
        ],
    )
    assert result.exit_code == foundry_cli.EXIT_FAILED
    assert "never converts currencies" in result.output


def test_run_state_file_lives_outside_the_repository(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    assert run_state_path().is_file()
    assert Path.cwd() not in run_state_path().parents
    assert run_state_path().stat().st_mode & 0o777 == 0o600


def test_the_subscription_is_never_written_into_the_repository_config(cli):
    """Regression: a subscription is a tenant identifier and .tokenlens.yml can be tracked."""
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    document = Path(".tokenlens.yml").read_text(encoding="utf-8")
    assert SUBSCRIPTION not in document
    assert "subscription_id:" not in document
    assert "subscription_id_env: AZURE_SUBSCRIPTION_ID" in document
    # It is still remembered, user-locally and outside the working tree.
    from tokenlens.foundry_workflow.configuration import load_subscription, target_path

    assert load_subscription() == SUBSCRIPTION
    assert Path.cwd() not in target_path().parents
    assert target_path().stat().st_mode & 0o777 == 0o600


def test_refresh_reuses_the_user_local_subscription(cli):
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    result = runner.invoke(app, ["foundry", "refresh", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "2 / 2 deployments succeeded" in result.output


def test_an_invalid_hand_edited_mapping_fails_with_an_actionable_error(cli):
    """Regression: a plausible manual edit must not produce a traceback."""
    assert runner.invoke(app, COLLECT_ARGS).exit_code == 0
    document = yaml.safe_load(Path(".tokenlens.yml").read_text(encoding="utf-8"))
    document["workloads"]["mappings"] = [
        {"id": "a", "name": "A", "deployments": ["reasoning-prod"], "allocation": "dedicated"},
        {"id": "b", "name": "B", "deployments": ["reasoning-prod"], "allocation": "dedicated"},
    ]
    Path(".tokenlens.yml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    result = runner.invoke(app, ["foundry", "refresh", "--no-open"])
    assert result.exit_code == foundry_cli.EXIT_FAILED
    assert "error=" in result.output
    assert "dedicated to both" in result.output
    assert "Traceback" not in result.output
