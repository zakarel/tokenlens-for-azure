"""End-to-end guided Foundry workflow with every Azure boundary faked.

Nothing here contacts Azure: no SDK is imported, no credential is constructed,
no inference call is made, and every identifier is synthetic. The wizard is
driven by :class:`ScriptedPrompter`, so each decision path is asserted without a
TTY and without ANSI parsing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from tokenlens.foundry_workflow import discovery
from tokenlens.foundry_workflow.configuration import (
    load_config,
    load_run_state,
    migrate_mapping,
    save_config,
    safe_display_path,
)
from tokenlens.foundry_workflow.models import (
    NoninteractiveError,
    WorkflowError,
)
from tokenlens.foundry_workflow.orchestration import (
    WorkflowServices,
    collect_deployments,
    dedicated_workload_assignments,
    install_command,
    run_state_from,
)
from tokenlens.foundry_workflow.prompts import ScriptedPrompter
from tokenlens.foundry_workflow.wizard import collect_and_report, configure
from tokenlens.telemetry.schema import BucketMetrics, MetricBucketRecord

WINDOW_START = datetime(2026, 9, 1, tzinfo=UTC)
SUBSCRIPTION = "11111111-2222-3333-4444-555555555555"
TENANT_SECRET = "TENANT-SECRET-must-never-be-written"

#: phi-4 is present in the packaged verified catalog; the partner model is not.
PRICED_MODEL = "phi-4"
UNPRICED_MODEL = "ministral-3b"


class StubResources:
    """Discovery surface only. Constructing a metrics client here would be a bug."""

    def __init__(self, *, deployments: list[dict] | None = None) -> None:
        self.calls: list[str] = []
        self._deployments = deployments if deployments is not None else [
            {"name": "reasoning-prod", "model": PRICED_MODEL, "model_version": "2026-07-09", "sku": "GlobalStandard", "capacity": 100},
            {"name": "coding-prod", "model": PRICED_MODEL, "model_version": "2026-07-09", "sku": "GlobalStandard", "capacity": 50},
            {"name": "compact-prod", "model": UNPRICED_MODEL, "model_version": "1", "sku": "Standard", "capacity": 10},
        ]

    def list_accounts(self, subscription_id: str, resource_group: str | None = None) -> list[dict]:
        self.calls.append("list_accounts")
        return [
            {"name": "example-foundry-account", "resource_group": "example-rg", "kind": "AIServices", "location": "East US 2"},
            {"name": "example-speech", "resource_group": "example-rg", "kind": "SpeechServices", "location": "East US 2"},
        ]

    def list_deployments(self, subscription_id: str, resource_group: str, account: str) -> list[dict]:
        self.calls.append("list_deployments")
        return list(self._deployments)

    def get_account(self, resource_group: str, account: str) -> dict:
        self.calls.append("get_account")
        return {
            "name": account,
            "kind": "AIServices",
            "location": "East US 2",
            "endpoint": "https://example-account.services.ai.azure.com/",
        }

    def list_metric_definitions(self, uri: str) -> list[dict]:
        self.calls.append("list_metric_definitions")
        return [
            {"name": "InputTokens", "dimensions": ["ModelDeploymentName", "ModelName", "ModelVersion"]},
            {"name": "OutputTokens", "dimensions": ["ModelDeploymentName", "ModelName", "ModelVersion"]},
            {"name": "ModelRequests", "dimensions": ["ModelDeploymentName", "StatusCode"]},
        ]


def collector(*, failing: set[str] | None = None, buckets: int = 4):
    failing = failing or set()
    seen: list[dict] = []

    def fake_collect(**kwargs):
        from tokenlens.foundry.monitor import CollectionResult, CollectorError

        seen.append(kwargs)
        names = list(kwargs["deployments"])
        window = kwargs["window"]
        inventory = {item["name"]: item for item in kwargs["deployment_inventory"]}
        assignments = kwargs.get("workload_assignments") or {}
        result = CollectionResult(
            window_start=window.start, window_end=window.end, collected_at=WINDOW_START
        )
        for name in names:
            if name in failing:
                raise CollectorError("Azure Monitor request failed (status 400)")
            entry = inventory.get(name, {})
            for index in range(buckets):
                result.records.append(
                    MetricBucketRecord(
                        event_id=f"{name}-{index}",
                        timestamp=WINDOW_START + timedelta(minutes=5 * index),
                        deployment_name=name,
                        model_name=str(entry.get("model") or "unknown"),
                        model_version=str(entry.get("model_version") or "") or None,
                        deployment_mode=kwargs["default_deployment_mode"],
                        metrics=BucketMetrics(
                            input_tokens=1000, output_tokens=200, requests=5, successful_requests=5
                        ),
                        workload=assignments.get(name),
                        workload_source="deployment_mapping" if assignments.get(name) else None,
                        allocation_confidence=(
                            "exact_dedicated_deployment" if assignments.get(name) else None
                        ),
                        window_start=window.start,
                        window_end=window.end,
                        expected_buckets=window.expected_buckets,
                    )
                )
            result.active_buckets += buckets
        return result

    fake_collect.calls = seen  # type: ignore[attr-defined]
    return fake_collect


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    # The user-local directory always sits outside the working tree.
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "user-config"))
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
    monkeypatch.delenv("TOKENLENS_METRICS_ENDPOINT", raising=False)
    return tmp_path


def services(*, resources=None, collect=None, subscriptions=None, missing=None, opened=None):
    opened_paths: list[Path] = [] if opened is None else opened

    def _open(path: Path) -> bool:
        opened_paths.append(path)
        return True

    return WorkflowServices(
        resource_client=lambda subscription: resources or StubResources(),
        metrics_client=lambda endpoint: {"endpoint": endpoint},
        collect=collect or collector(),
        subscriptions=lambda: list(subscriptions or []),
        open_report=_open,
        now=lambda: datetime(2026, 9, 15, tzinfo=UTC),
        missing_packages=lambda: list(missing or []),
    )


def first_run_answers(deployments=("reasoning-prod", "coding-prod"), *, workloads: str = "later"):
    answers = [
        "workload_cost",
        SUBSCRIPTION,
        "example-rg/example-foundry-account",
        list(deployments),
        "14",
        workloads,
    ]
    if "compact-prod" in deployments:
        # The partner model has no exact rate, so the wizard asks how to proceed.
        answers.append("continue")
    return answers


# --- Preflight and dependency guidance -------------------------------------


def test_missing_extras_produce_the_exact_environment_specific_install_command(workspace):
    prompter = ScriptedPrompter(first_run_answers())
    with pytest.raises(WorkflowError) as error:
        configure(prompter, services=services(missing=["azure.identity"]))
    assert "azure.identity" in str(error.value)
    assert install_command() in str(error.value)
    assert 'pip install -e ".[foundry,foundry-claude,foundry-monitor]"' in install_command()


# --- Discovery and metadata detection --------------------------------------


@pytest.mark.parametrize(
    ("sku", "mode"),
    [
        ("GlobalStandard", "global"),
        ("Standard", "regional"),
        ("DataZoneStandard", "data_zone"),
        ("GlobalProvisionedManaged", "global_provisioned"),
        ("ProvisionedManaged", "regional_provisioned"),
        ("DataZoneProvisionedManaged", "data_zone_provisioned"),
        ("GlobalBatch", "batch"),
        ("", "unknown"),
        ("SomethingNew", "unknown"),
    ],
)
def test_sku_maps_to_an_exact_mode_and_never_defaults_to_global(sku, mode):
    assert discovery.deployment_mode_for_sku(sku) == mode


@pytest.mark.parametrize(
    ("model", "family", "api"),
    [
        ("gpt-4.1", "azure_openai", "openai"),
        ("phi-4", "azure_openai", "openai"),
        ("claude-opus-5", "claude_foundry", "anthropic"),
        ("ministral-3b", "partner_model", "openai"),
        ("entirely-unknown-model", "unknown", "unknown"),
    ],
)
def test_model_maps_to_provider_family_and_documented_api(model, family, api):
    resolved = discovery.provider_family_for_model(model)
    assert resolved == family
    assert discovery.inference_api_for_family(resolved) == api


def test_endpoint_normalization_accepts_the_base_services_endpoint():
    assert (
        discovery.normalize_endpoint("https://example.services.ai.azure.com/anthropic")
        == "https://example.services.ai.azure.com"
    )
    assert (
        discovery.anthropic_base_url("https://example.services.ai.azure.com/")
        == "https://example.services.ai.azure.com/anthropic"
    )
    with pytest.raises(WorkflowError):
        discovery.normalize_endpoint("http://example.services.ai.azure.com")


def test_metrics_endpoint_is_derived_from_the_account_region():
    assert (
        discovery.metrics_endpoint_for_region("East US 2")
        == "https://eastus2.metrics.monitor.azure.com"
    )
    # Sovereign or unusual clouds keep an explicit override.
    assert (
        discovery.metrics_endpoint_for_region("East US 2", override="https://custom.example/")
        == "https://custom.example"
    )
    with pytest.raises(WorkflowError):
        discovery.metrics_endpoint_for_region("")


def test_only_foundry_relevant_account_kinds_are_offered():
    accounts = discovery.discover_accounts(StubResources(), SUBSCRIPTION)
    assert [item.name for item in accounts] == ["example-foundry-account"]


def test_subscription_listing_ignores_disabled_subscriptions_and_marks_the_active_one():
    payload = json.dumps(
        [
            {"id": "a" * 36, "name": "Disabled Subscription", "state": "Disabled"},
            {"id": "b" * 36, "name": "Second Subscription", "state": "Enabled"},
            {"id": "c" * 36, "name": "Production Subscription", "state": "Enabled", "isDefault": True},
        ]
    )
    options = discovery.list_subscriptions(runner=lambda: payload)
    assert [item.name for item in options] == ["Production Subscription", "Second Subscription"]
    assert options[0].is_default is True
    # The full identifier is never in the default label.
    assert options[0].subscription_id not in options[0].label()
    assert options[0].short_id in options[0].label()


# --- Guided configuration ---------------------------------------------------


def test_first_run_completes_without_manually_supplied_resource_identifiers(workspace):
    prompter = ScriptedPrompter(first_run_answers())
    config, readiness = configure(prompter, services=services())
    assert config.foundry.subscription_id == SUBSCRIPTION
    assert config.foundry.resource_group == "example-rg"
    assert config.foundry.account == "example-foundry-account"
    assert config.foundry.region == "East US 2"
    assert config.foundry.deployment_names == ["reasoning-prod", "coding-prod"]
    assert config.collection.lookback_days == 14
    reasoning = config.foundry.deployments[0]
    assert reasoning.model == PRICED_MODEL
    assert reasoning.model_version == "2026-07-09"
    assert reasoning.sku == "GlobalStandard"
    assert reasoning.deployment_mode == "global"
    assert reasoning.provider_family == "azure_openai"
    assert reasoning.inference_api == "openai"
    assert [item.state for item in readiness] == ["exact_public_rate", "exact_public_rate"]
    # Four decisions only on the normal path.
    steps = [line for line in prompter.transcript if line.startswith("Step ")]
    assert steps == [
        "Step 1/4 · Azure subscription",
        "Step 2/4 · Foundry account",
        "Step 3/4 · Deployments",
        "Step 4/4 · Analysis window",
    ]


def test_multiple_subscriptions_require_an_explicit_choice(workspace):
    options = discovery.list_subscriptions(
        runner=lambda: json.dumps(
            [
                {"id": "a" * 36, "name": "Production Subscription", "state": "Enabled", "isDefault": True},
                {"id": "b" * 36, "name": "Sandbox Subscription", "state": "Enabled"},
            ]
        )
    )
    prompter = ScriptedPrompter(
        ["workload_cost", "b" * 36, "example-rg/example-foundry-account", ["reasoning-prod"], "14", "later"]
    )
    config, _ = configure(prompter, services=services(subscriptions=options))
    # The chosen subscription is used, not the Azure CLI's active one.
    assert config.foundry.subscription_id == "b" * 36
    offered = [line for line in prompter.transcript if line.startswith("  - ")]
    assert any("Production Subscription" in line for line in offered)
    assert any("Sandbox Subscription" in line for line in offered)


def test_every_selected_deployment_gets_one_default_technical_workload(workspace):
    prompter = ScriptedPrompter(first_run_answers())
    config, _ = configure(prompter, services=services())
    technical = [item for item in config.workloads.identities if item.workload_scope == "technical"]
    assert [item.workload_id for item in technical] == [
        "deployment:reasoning-prod",
        "deployment:coding-prod",
    ]
    assert all(item.configuration_status == "needs_configuration" for item in technical)
    assert not config.workloads.mappings
    assert any("Needs configuration" in line for line in prompter.transcript)


def test_optional_business_workload_enrichment_is_recorded(workspace):
    answers = first_run_answers(workloads="yes") + [
        "dedicated",
        "Support assistant",
        "agent",
        "production",
        "Handles tier-1 support",
        "platform-team",
        "CC-1234",
        "high",
        "shared",
        "python",
        "Coding agent",
        "agent",
        "production",
        "",
        "",
        "",
        "unknown",
    ]
    prompter = ScriptedPrompter(answers)
    config, _ = configure(prompter, services=services())
    mappings = {item.id: item for item in config.workloads.mappings}
    assert mappings["support-assistant"].allocation == "dedicated"
    assert mappings["support-assistant"].owner_label == "platform-team"
    assert mappings["coding-agent"].allocation == "shared"
    # A shared deployment is told plainly that Azure Monitor cannot allocate it.
    assert any("This deployment is shared." in line for line in prompter.transcript)
    assert any("instrument_openai" in line for line in prompter.transcript)


def test_noninteractive_configure_requires_explicit_values(workspace):
    prompter = ScriptedPrompter([], interactive=False)
    with pytest.raises(NoninteractiveError):
        configure(prompter, services=services())


def test_noninteractive_configure_accepts_explicit_options(workspace):
    prompter = ScriptedPrompter([], interactive=False)
    config, _ = configure(
        prompter,
        services=services(),
        subscription=SUBSCRIPTION,
        resource_group="example-rg",
        account="example-foundry-account",
        deployments=["reasoning-prod"],
        days=7,
    )
    assert config.foundry.deployment_names == ["reasoning-prod"]
    assert config.collection.lookback_days == 7


def test_an_unknown_deployment_name_is_rejected_rather_than_silently_skipped(workspace):
    prompter = ScriptedPrompter([], interactive=False)
    with pytest.raises(WorkflowError) as error:
        configure(
            prompter,
            services=services(),
            subscription=SUBSCRIPTION,
            resource_group="example-rg",
            account="example-foundry-account",
            deployments=["not-a-deployment"],
        )
    assert "not-a-deployment" in str(error.value)


# --- Configuration persistence ---------------------------------------------


def test_v1_configuration_migrates_without_losing_unrelated_keys(workspace):
    Path(".tokenlens.yml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "analysis": {"redact_content": True},
                "rules": {"TL001": {"enabled": True}},
                "report": {"overview_min_impact_percent": 1.0},
                "foundry": {
                    "subscription_id_env": "AZURE_SUBSCRIPTION_ID",
                    "resource_group": "example-rg",
                    "account": "example-foundry-account",
                    "deployments": ["reasoning-prod"],
                },
                "monitor": {"lookback_days": 30, "granularity_minutes": 5},
            }
        ),
        encoding="utf-8",
    )
    config, raw = load_config(None)
    assert config.collection.lookback_days == 30
    assert config.foundry.deployment_names == ["reasoning-prod"]
    save_config(config, raw)
    written = yaml.safe_load(Path(".tokenlens.yml").read_text(encoding="utf-8"))
    assert written["version"] == 2
    assert written["analysis"] == {"redact_content": True}
    assert written["rules"] == {"TL001": {"enabled": True}}
    # The materiality key the analyzer reads is preserved beside the new keys.
    assert written["report"]["overview_min_impact_percent"] == 1.0
    assert written["report"]["format"] == "html"


def test_migration_is_pure_and_leaves_the_source_mapping_untouched():
    original = {"version": 1, "foundry": {"deployments": ["a"]}, "monitor": {"lookback_days": 7}}
    migrated = migrate_mapping(original)
    assert original["foundry"]["deployments"] == ["a"]
    assert migrated["foundry"]["deployments"] == [{"name": "a"}]
    assert migrated["collection"]["lookback_days"] == 7


def test_saved_configuration_contains_no_credential_material(workspace):
    prompter = ScriptedPrompter(first_run_answers())
    configure(prompter, services=services())
    text = Path(".tokenlens.yml").read_text(encoding="utf-8")
    assert TENANT_SECRET not in text
    for forbidden in ("api_key", "apiKey", "connection_string", "access_token", "bearer", "secret"):
        assert forbidden not in text
    # An endpoint is discovered at run time rather than stored.
    assert "services.ai.azure.com" not in text


def test_configuration_is_written_with_user_only_permissions(workspace):
    prompter = ScriptedPrompter(first_run_answers())
    configure(prompter, services=services())
    mode = Path(".tokenlens.yml").stat().st_mode & 0o777
    assert mode == 0o600


# --- Collection orchestration ----------------------------------------------


def configured(workspace, *, answers=None, service=None):
    prompter = ScriptedPrompter(answers or first_run_answers())
    return configure(prompter, services=service or services())[0]


def test_collection_isolates_one_deployment_failure_from_the_others(workspace):
    config = configured(workspace, answers=first_run_answers(("reasoning-prod", "coding-prod", "compact-prod")))
    service = services(collect=collector(failing={"compact-prod"}))
    prompter = ScriptedPrompter([], interactive=False)
    summary, result = collect_and_report(prompter, config, services=service, open_report=False)
    assert summary.status == "partial"
    assert [item.deployment for item in summary.succeeded] == ["reasoning-prod", "coding-prod"]
    failed = summary.failed[0]
    assert failed.deployment == "compact-prod"
    assert failed.error_category == "metric_unavailable"
    # A partial run still produces a usable report.
    assert result is not None and result.path is not None and result.path.is_file()


def test_zero_successes_are_never_described_as_a_successful_collection(workspace):
    config = configured(workspace)
    service = services(collect=collector(failing={"reasoning-prod", "coding-prod"}))
    prompter = ScriptedPrompter([], interactive=False)
    summary, result = collect_and_report(prompter, config, services=service, open_report=False)
    assert summary.status == "failed"
    assert result is None


def test_refresh_is_idempotent_and_writes_no_duplicate_buckets(workspace):
    config = configured(workspace)
    service = services()
    prompter = ScriptedPrompter([], interactive=False)
    first, _ = collect_and_report(prompter, config, services=service, open_report=False)
    second, _ = collect_and_report(prompter, config, services=service, open_report=False)
    assert first.records_written == 8
    assert second.records_written == 0
    assert second.duplicates_skipped >= 8


def test_a_stale_deployment_is_reported_rather_than_failing_opaquely(workspace):
    config = configured(workspace)
    shrunk = StubResources(
        deployments=[
            {"name": "reasoning-prod", "model": PRICED_MODEL, "model_version": "2026-07-09", "sku": "GlobalStandard"}
        ]
    )
    summary = collect_deployments(config, services=services(resources=shrunk))
    statuses = {item.deployment: item.status for item in summary.outcomes}
    assert statuses == {"reasoning-prod": "succeeded", "coding-prod": "skipped"}
    skipped = next(item for item in summary.outcomes if item.status == "skipped")
    assert skipped.error_category == "deployment_not_found"


def test_only_a_dedicated_deployment_is_tagged_on_an_aggregate_bucket(workspace):
    answers = first_run_answers(workloads="yes") + [
        "dedicated",
        "Support assistant",
        "agent",
        "production",
        "",
        "",
        "",
        "unknown",
        "shared",
        "python",
        "Coding agent",
        "agent",
        "production",
        "",
        "",
        "",
        "unknown",
    ]
    config = configured(workspace, answers=answers)
    assignments = dedicated_workload_assignments(config)
    assert assignments == {"reasoning-prod": "Support assistant"}
    fake = collector()
    collect_deployments(config, services=services(collect=fake))
    tagged = {
        call["deployments"][0]: (call.get("workload_assignments") or {})
        for call in fake.calls  # type: ignore[attr-defined]
    }
    assert tagged["reasoning-prod"] == {"reasoning-prod": "Support assistant"}
    assert tagged["coding-prod"] == {}


def test_collection_requires_a_configured_target(workspace):
    config, _raw = load_config(None)
    with pytest.raises(WorkflowError):
        collect_deployments(config, services=services())


# --- Report and run state ---------------------------------------------------


def test_report_is_generated_and_opened_in_one_run(workspace):
    config = configured(workspace)
    opened: list[Path] = []
    service = services(opened=opened)
    prompter = ScriptedPrompter([], interactive=False)
    summary, result = collect_and_report(prompter, config, services=service, open_report=True)
    assert summary.status == "succeeded"
    assert result is not None
    assert result.path is not None and result.path.suffix == ".html"
    assert opened == [result.path]
    html = result.path.read_text(encoding="utf-8")
    assert "Workloads</span>" in html
    assert "reasoning-prod" in html


def test_the_report_shows_cost_by_workload_and_an_unassigned_row(workspace):
    answers = first_run_answers(workloads="yes") + [
        "dedicated",
        "Support assistant",
        "agent",
        "production",
        "",
        "",
        "",
        "unknown",
        "shared",
        "python",
        "Coding agent",
        "agent",
        "production",
        "",
        "",
        "",
        "unknown",
    ]
    config = configured(workspace, answers=answers)
    prompter = ScriptedPrompter([], interactive=False)
    _summary, result = collect_and_report(prompter, config, services=services(), open_report=False)
    assert result is not None
    portfolio = result.report.workloads
    business = {item.workload_id: item for item in portfolio.business_workloads}
    assert "support-assistant" in business
    assert business["support-assistant"].allocation_confidence == "exact_dedicated_deployment"
    # The shared deployment has no request tags, so its traffic stays unassigned.
    assert "unassigned" in business
    assert business["unassigned"].allocation_confidence == "unallocated"
    html = result.path.read_text(encoding="utf-8")
    assert "Unassigned workload" in html


def test_skipping_workload_configuration_still_renders_technical_rows(workspace):
    config = configured(workspace)
    prompter = ScriptedPrompter([], interactive=False)
    _summary, result = collect_and_report(prompter, config, services=services(), open_report=False)
    assert result is not None
    html = result.path.read_text(encoding="utf-8")
    assert "Workloads</span>" in html
    assert "Technical · Needs configuration" in html
    assert "Showing deployment-backed technical workloads" in html
    assert "tokenlens-azure foundry workloads configure" in html


def test_run_state_stores_no_identifier_path_or_secret(workspace):
    config = configured(workspace)
    prompter = ScriptedPrompter([], interactive=False)
    summary, result = collect_and_report(prompter, config, services=services(), open_report=False)
    state = load_run_state()
    assert state.successful_deployments == 2
    assert state.failed_deployments == 0
    assert state.collection_status == "succeeded"
    payload = json.dumps(state.model_dump(mode="json"))
    assert SUBSCRIPTION not in payload
    assert TENANT_SECRET not in payload
    assert "services.ai.azure.com" not in payload
    assert str(workspace) not in payload
    assert state.report is not None and state.report.startswith("reports/")


def test_run_state_reports_partial_and_failed_collections_honestly(workspace):
    config = configured(workspace, answers=first_run_answers(("reasoning-prod", "coding-prod", "compact-prod")))
    summary = collect_deployments(config, services=services(collect=collector(failing={"compact-prod"})))
    state = run_state_from(summary, None, now=lambda: datetime(2026, 9, 15, tzinfo=UTC))
    assert state.collection_status == "partial"
    assert state.successful_deployments == 2
    assert state.failed_deployments == 1
    assert state.report is None


def test_safe_display_path_never_leaks_a_home_directory(workspace):
    assert safe_display_path(Path("reports") / "a.html") == str(Path("reports") / "a.html")
