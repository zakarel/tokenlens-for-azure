"""The guided Foundry workflow: four decisions, then collect and report.

The normal path asks for a subscription, a Foundry account, deployments, and a
lookback period. Everything else — deployment mode, provider family, inference
API, endpoints, and the Azure Monitor regional endpoint — is detected from exact
Azure metadata. Pricing remediation and business workload enrichment appear only
when they are relevant, and neither blocks usage collection.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from ..pricing import PricingCatalog
from ..workloads import (
    WorkloadMapping,
    canonical_workload_id,
    merge_identities,
    safe_account_scope,
)
from . import discovery
from .configuration import (
    customer_catalog_path,
    load_config,
    refresh_identities,
    safe_display_path,
    save_config,
    save_run_state,
)
from .models import (
    ANALYSIS_GOALS,
    LOOKBACK_CHOICES,
    AccountOption,
    CollectionSummary,
    DeploymentRecord,
    FoundryWorkflowConfig,
    NoninteractiveError,
    SubscriptionOption,
    WorkflowError,
)
from .orchestration import (
    ReportResult,
    WorkflowServices,
    collect_deployments,
    generate_report,
    install_command,
    run_state_from,
)
from .pricing import PricingReadiness, load_customer_catalog, pricing_readiness
from .prompts import Choice, Prompter, symbol

__all__ = [
    "TOTAL_STEPS",
    "collect_and_report",
    "configure",
    "preflight",
    "workload_summary_lines",
    "configure_workloads",
]

#: The normal path never exceeds four infrastructure decisions. Optional
#: business workload enrichment enriches report semantics rather than enabling
#: collection, so it is not counted here.
TOTAL_STEPS = 4

WORKLOAD_TYPES: tuple[tuple[str, str], ...] = (
    ("agent", "Agent"),
    ("application", "Application"),
    ("api", "API"),
    ("business_process", "Business process"),
    ("batch_job", "Batch job"),
    ("other", "Other"),
)

TAGGING_EXAMPLES: dict[str, str] = {
    "python": (
        "from tokenlens.integrations import instrument_openai\n\n"
        "client = instrument_openai(\n"
        "    existing_client,\n"
        '    workload="support-assistant",\n'
        '    deployment_mode="global",\n'
        ")"
    ),
    "claude": (
        "from tokenlens.integrations import instrument_anthropic_foundry\n\n"
        "client = instrument_anthropic_foundry(\n"
        "    existing_client,\n"
        '    workload="support-assistant",\n'
        '    deployment_mode="global",\n'
        ")"
    ),
    "otel": (
        "span.set_attribute('tokenlens.workload', 'support-assistant')\n"
        "span.set_attribute('tokenlens.environment', 'production')"
    ),
    "apim": (
        "APIM policy: set the tokenlens-workload header, then map it to\n"
        "`workload` in the Application Insights import mapping."
    ),
    "yaml": (
        "fields:\n"
        "  workload: properties.workload\n"
        "  environment: properties.environment"
    ),
}


def preflight(prompter: Prompter, services: WorkflowServices | None = None) -> list[str]:
    """Report readiness before the first question. Nothing is installed silently."""
    import sys

    services = services or WorkflowServices()
    lines = [
        f"python={sys.version.split()[0]} ({'supported' if sys.version_info >= (3, 11) else 'unsupported'})",
        f"cwd-writable={'yes' if os.access('.', os.W_OK) else 'no'}",
    ]
    missing = services.missing_packages()
    lines.append("collector-extras=" + ("ready" if not missing else "missing: " + ", ".join(missing)))
    if missing:
        lines.append(f"install-command={install_command()}")
    return lines


def _select_subscription(
    prompter: Prompter,
    services: WorkflowServices,
    *,
    configured: str | None,
    explicit: str | None,
) -> str:
    if explicit:
        return explicit
    options: list[SubscriptionOption] = list(services.subscriptions())
    if not prompter.interactive:
        if configured:
            return configured
        raise NoninteractiveError(
            "A subscription is required. Pass --subscription or configure one with "
            "`tokenlens-azure foundry configure`. TokenLens never falls back to an ambiguous "
            "ambient Azure CLI context in a noninteractive run."
        )
    if not options:
        value = prompter.text(
            "Azure subscription ID (run `az login` to list them automatically)",
            default=configured or "",
            allow_empty=False,
        )
        if not value:
            raise WorkflowError("A subscription is required.")
        return value
    choices = [
        Choice(option.subscription_id, option.label(), selected=option.subscription_id == configured)
        for option in options
    ]
    return prompter.select("Azure subscription", choices, default=configured or options[0].subscription_id)


def _select_account(
    prompter: Prompter,
    accounts: Sequence[AccountOption],
    *,
    configured: str | None,
) -> AccountOption:
    if not accounts:
        raise WorkflowError(
            "No Foundry or Azure OpenAI account was found in this subscription. Confirm the "
            "subscription selection and that the signed-in principal can read Cognitive Services "
            "accounts."
        )
    if not prompter.interactive:
        match = next((item for item in accounts if item.name == configured), None)
        if match is None:
            raise NoninteractiveError(
                "An account is required. Pass --account (and --resource-group) explicitly."
            )
        return match
    choices = [
        Choice(f"{item.resource_group}/{item.name}", item.label(), selected=item.name == configured)
        for item in accounts
    ]
    chosen = prompter.select("Foundry account", choices)
    resource_group, _, name = chosen.partition("/")
    return next(item for item in accounts if item.name == name and item.resource_group == resource_group)


def _deployment_detail(deployment: DeploymentRecord, readiness: PricingReadiness | None) -> str:
    parts = [deployment.mode_label]
    if deployment.provider_family != "unknown":
        parts.append(deployment.provider_family.replace("_", " "))
    if deployment.capacity is not None:
        parts.append(f"capacity {deployment.capacity}")
    if readiness is not None:
        parts.append(f"{symbol('✓') if readiness.resolved else symbol('⚠')} {readiness.label}")
    return " · ".join(parts)


def _select_deployments(
    prompter: Prompter,
    inventory: Sequence[DeploymentRecord],
    *,
    configured: Sequence[str],
    explicit: Sequence[str],
    readiness: dict[str, PricingReadiness],
) -> list[DeploymentRecord]:
    if explicit:
        resolved, missing = discovery.selected_deployments(inventory, explicit)
        if missing:
            raise WorkflowError(
                "These deployments are not in the account inventory: " + ", ".join(missing)
            )
        return resolved
    if not inventory:
        raise WorkflowError("This account has no deployments to collect.")
    if not prompter.interactive:
        resolved, missing = discovery.selected_deployments(inventory, configured)
        if missing:
            raise WorkflowError(
                "These configured deployments are no longer in the account inventory: "
                + ", ".join(missing)
            )
        if not resolved:
            raise NoninteractiveError(
                "At least one deployment is required. Pass --deployment one or more times."
            )
        return resolved
    preselected = {name.casefold() for name in configured} or {
        item.name.casefold() for item in inventory
    }
    choices = [
        Choice(
            item.name,
            item.label(),
            _deployment_detail(item, readiness.get(item.name)),
            selected=item.name.casefold() in preselected,
        )
        for item in inventory
    ]
    names = prompter.multiselect("Select deployments", choices, minimum=1)
    resolved, _missing = discovery.selected_deployments(inventory, names)
    return resolved


def _select_lookback(prompter: Prompter, *, configured: int, explicit: int | None) -> int:
    if explicit is not None:
        if not 1 <= explicit <= 90:
            raise WorkflowError("The lookback window must be between 1 and 90 days.")
        return explicit
    if not prompter.interactive:
        return configured
    choices = [
        Choice(
            str(days),
            f"{days} day{'s' if days != 1 else ''}",
            f"{purpose} · {days * 288:,} five-minute buckets",
            selected=days == configured,
        )
        for days, purpose in LOOKBACK_CHOICES
    ]
    chosen = int(prompter.select("Analysis window", choices, default=str(configured)))
    prompter.echo(
        "PTU evidence needs active buckets, not elapsed time. A long window with little traffic "
        "still provides insufficient evidence, and a recently created deployment may have less "
        "history than the window requests."
    )
    return chosen


def _select_goal(prompter: Prompter, *, configured: str) -> str:
    if not prompter.interactive:
        return configured
    choices = [
        Choice(value, label, requirement, selected=value == configured)
        for value, label, requirement in ANALYSIS_GOALS
    ]
    return prompter.select("Analysis goal", choices, default=configured)


def workload_summary_lines(config: FoundryWorkflowConfig) -> list[str]:
    """Human-readable summary of the automatic technical workloads."""
    lines = ["Technical workloads created automatically"]
    business = {
        canonical_workload_id(deployment): mapping
        for mapping in config.workloads.mappings
        for deployment in mapping.deployments
    }
    for deployment in config.foundry.deployments:
        mapping = business.get(canonical_workload_id(deployment.name))
        if mapping is None:
            lines.append(f"{symbol('✓')} {deployment.name} — deployment-backed workload · Needs configuration")
        elif mapping.allocation == "dedicated":
            lines.append(
                f"{symbol('✓')} {deployment.name} — deployment-backed workload · "
                f"Business workload: {mapping.name} · 100% exact dedicated mapping"
            )
        else:
            lines.append(
                f"{symbol('⚠')} {deployment.name} — deployment-backed workload · shared by "
                f"{mapping.name} and others · request tagging required for allocation"
            )
    return lines


def configure_workloads(
    prompter: Prompter,
    config: FoundryWorkflowConfig,
    *,
    only: Sequence[str] | None = None,
) -> FoundryWorkflowConfig:
    """Optionally enrich the default technical workloads with business identity."""
    if not prompter.interactive:
        raise NoninteractiveError(
            "Workload configuration is interactive. Use "
            "`tokenlens-azure foundry workloads export-template` for automation."
        )
    existing = {mapping.id: mapping for mapping in config.workloads.mappings}
    targets = [
        deployment
        for deployment in config.foundry.deployments
        if only is None or deployment.name in set(only)
    ]
    for deployment in targets:
        usage = prompter.select(
            f"{deployment.name} — how is this deployment used?",
            [
                Choice("dedicated", "Dedicated to one business workload"),
                Choice("shared", "Shared by multiple business workloads"),
                Choice("technical", "Keep as technical deployment only"),
            ],
            default="dedicated",
        )
        if usage == "technical":
            for mapping in list(existing.values()):
                if deployment.name in mapping.deployments:
                    mapping.deployments = [
                        name for name in mapping.deployments if name != deployment.name
                    ]
                    if not mapping.deployments:
                        existing.pop(mapping.id, None)
            continue
        # Reassigning a deployment replaces any prior business mapping. This
        # makes renaming or changing dedicated/shared allocation safe.
        for mapping in list(existing.values()):
            if deployment.name in mapping.deployments:
                mapping.deployments = [
                    name for name in mapping.deployments if name != deployment.name
                ]
                if not mapping.deployments:
                    existing.pop(mapping.id, None)
        if usage == "shared":
            prompter.echo(
                "This deployment is shared. Azure Monitor can report deployment cost, but not cost "
                "per workload. Add SDK/OpenTelemetry workload tagging for allocation."
            )
            example = prompter.select(
                "Choose an integration example",
                [
                    Choice("python", "Python SDK"),
                    Choice("claude", "Claude on Foundry"),
                    Choice("otel", "OpenTelemetry"),
                    Choice("apim", "APIM / Application Insights"),
                    Choice("yaml", "Generic YAML mapping"),
                ],
                default="python",
            )
            prompter.echo(TAGGING_EXAMPLES[example])
        name = prompter.text("Workload name", allow_empty=False)
        if not name:
            continue
        workload_type = prompter.select(
            "Workload type",
            [Choice(value, label) for value, label in WORKLOAD_TYPES],
            default="agent",
        )
        environment = prompter.text("Environment (optional)", default="")
        purpose = prompter.text("Business purpose (optional)", default="")
        owner = prompter.text("Owner label — team or alias, never a personal name (optional)", default="")
        cost_center = prompter.text("Cost center (optional)", default="")
        criticality = prompter.select(
            "Criticality",
            [
                Choice("unknown", "Unknown"),
                Choice("low", "Low"),
                Choice("medium", "Medium"),
                Choice("high", "High"),
                Choice("mission_critical", "Mission critical"),
            ],
            default="unknown",
        )
        identifier = canonical_workload_id(name)
        mapping = existing.get(identifier)
        deployments = sorted({*(mapping.deployments if mapping else []), deployment.name})
        existing[identifier] = WorkloadMapping(
            id=identifier,
            name=name,
            type=workload_type,  # type: ignore[arg-type]
            environment=environment or None,
            deployments=deployments,
            allocation="dedicated" if usage == "dedicated" else "shared",
            business_purpose=purpose or None,
            owner_label=owner or None,
            cost_center=cost_center or None,
            criticality=criticality,  # type: ignore[arg-type]
        )
    config.workloads.mappings = sorted(existing.values(), key=lambda item: item.id)
    return refresh_identities(
        config,
        account_scope=safe_account_scope(config.foundry.account, config.foundry.resource_group),
    )


def _resolve_pricing(
    prompter: Prompter,
    config: FoundryWorkflowConfig,
    *,
    customer_catalog: PricingCatalog | None,
) -> list[PricingReadiness]:
    """Show exact pricing readiness and offer only safe remediations."""
    readiness = pricing_readiness(config.foundry.deployments, customer_catalog=customer_catalog)
    prompter.echo("\nPricing readiness")
    for item in readiness:
        marker = symbol("✓") if item.resolved else symbol("⚠")
        prompter.echo(f"{marker} {item.deployment} — {item.label}")
    unresolved = [item for item in readiness if not item.resolved]
    if not unresolved or not prompter.interactive:
        return readiness
    choice = prompter.select(
        f"{len(unresolved)} deployment(s) have no exact rate. What next?",
        [
            Choice("continue", "Continue without cost analysis for those deployments"),
            Choice("override", "Configure a customer/contracted rate now"),
            Choice("sync", "Synchronize verified public pricing"),
            Choice("remove", "Remove those deployments from this run"),
        ],
        default="continue",
    )
    if choice == "sync":
        from .pricing import PRICING_SYNC_DEFERRED_REASON

        prompter.echo(PRICING_SYNC_DEFERRED_REASON)
        prompter.echo("Use a customer rate instead: tokenlens-azure pricing set-rate")
    elif choice == "remove":
        remaining = [
            deployment
            for deployment in config.foundry.deployments
            if deployment.name not in {item.deployment for item in unresolved}
        ]
        if not remaining:
            prompter.echo("Every selected deployment is unpriced, so none were removed.")
        else:
            config.foundry.deployments = remaining
            readiness = pricing_readiness(
                config.foundry.deployments, customer_catalog=customer_catalog
            )
    elif choice == "override":
        prompter.echo(
            "Add an exact contracted rate with: tokenlens-azure pricing set-rate --model MODEL "
            "--input-per-million X --output-per-million Y --effective-from YYYY-MM-DD"
        )
        prompter.echo(f"Rates are stored locally in {safe_display_path(customer_catalog_path())}.")
    return readiness


def configure(
    prompter: Prompter,
    *,
    services: WorkflowServices | None = None,
    config_path: str | None = None,
    subscription: str | None = None,
    resource_group: str | None = None,
    account: str | None = None,
    deployments: Sequence[str] = (),
    days: int | None = None,
    configure_business_workloads: bool | None = None,
) -> tuple[FoundryWorkflowConfig, list[PricingReadiness]]:
    """Discover, select, and save a credential-free configuration."""
    services = services or WorkflowServices()
    config, raw = load_config(config_path)
    missing = services.missing_packages()
    if missing:
        raise WorkflowError(
            "The Azure discovery extras are not installed ("
            + ", ".join(missing)
            + f"). Install them with: {install_command()}"
        )

    prompter.echo("\nAnalysis goal")
    goal = _select_goal(prompter, configured=config.collection.analysis_goal)
    config.collection.analysis_goal = goal  # type: ignore[assignment]
    if goal in {"prompt_efficiency", "full_assessment"}:
        prompter.echo(
            "Prompt and token efficiency needs request-level telemetry. Azure Monitor aggregates "
            "answer cost and PTU questions; instrument the client with tokenlens.integrations or "
            "import OpenTelemetry spans for per-request diagnostics."
        )

    prompter.step(1, TOTAL_STEPS, "Azure subscription")
    subscription_id = _select_subscription(
        prompter,
        services,
        configured=config.foundry.subscription_id or os.getenv(config.foundry.subscription_id_env),
        explicit=subscription,
    )
    resources = services.resource_client(subscription_id)

    prompter.step(2, TOTAL_STEPS, "Foundry account")
    accounts = discovery.discover_accounts(resources, subscription_id, resource_group=resource_group)
    if account and resource_group:
        chosen_account = next(
            (item for item in accounts if item.name == account and item.resource_group == resource_group),
            None,
        )
        if chosen_account is None:
            raise WorkflowError(
                f"Account {account} was not found in resource group {resource_group} for this subscription."
            )
    else:
        chosen_account = _select_account(prompter, accounts, configured=account or config.foundry.account)

    prompter.step(3, TOTAL_STEPS, "Deployments")
    inventory = discovery.discover_deployments(
        resources, subscription_id, chosen_account.resource_group, chosen_account.name
    )
    customer_catalog = load_customer_catalog()
    readiness_map = {
        item.deployment: item
        for item in pricing_readiness(inventory, customer_catalog=customer_catalog)
    }
    selection = _select_deployments(
        prompter,
        inventory,
        configured=config.foundry.deployment_names,
        explicit=list(deployments),
        readiness=readiness_map,
    )

    prompter.step(4, TOTAL_STEPS, "Analysis window")
    lookback = _select_lookback(prompter, configured=config.collection.lookback_days, explicit=days)

    metadata = resources.get_account(chosen_account.resource_group, chosen_account.name)
    config.foundry.subscription_id = subscription_id
    config.foundry.resource_group = chosen_account.resource_group
    config.foundry.account = chosen_account.name
    config.foundry.region = str(metadata.get("location") or chosen_account.location or "") or None
    config.foundry.deployments = selection
    config.collection.lookback_days = lookback
    config = refresh_identities(
        config,
        account_scope=safe_account_scope(config.foundry.account, config.foundry.resource_group),
    )

    prompter.echo("")
    for line in workload_summary_lines(config):
        prompter.echo(line)
    wants_workloads = configure_business_workloads
    if wants_workloads is None and prompter.interactive:
        wants_workloads = (
            prompter.select(
                "Configure business workload names and ownership now?",
                [Choice("later", "Later"), Choice("yes", "Yes")],
                default="later",
            )
            == "yes"
        )
    if wants_workloads:
        config = configure_workloads(prompter, config)
        prompter.echo("")
        for line in workload_summary_lines(config):
            prompter.echo(line)

    readiness = _resolve_pricing(prompter, config, customer_catalog=customer_catalog)
    save_config(config, raw, path=config_path)
    return config, readiness


def collect_and_report(
    prompter: Prompter,
    config: FoundryWorkflowConfig,
    *,
    services: WorkflowServices | None = None,
    open_report: bool | None = None,
    output_format: str | None = None,
    write_run_state: bool = True,
) -> tuple[CollectionSummary, ReportResult | None]:
    """Collect every selected deployment, then analyze and open the report."""
    services = services or WorkflowServices()
    customer_catalog = load_customer_catalog()

    def progress(name: str, state: str) -> None:
        marker = symbol("✓") if state == "collected" else symbol("✕")
        prompter.echo(f"{marker} {name} · {state}")

    summary = collect_deployments(
        config, services=services, progress=progress, customer_catalog=customer_catalog
    )
    result: ReportResult | None = None
    if summary.succeeded:
        result = generate_report(
            config,
            services=services,
            open_report=open_report,
            output_format=output_format,
            customer_catalog=customer_catalog,
        )
    if write_run_state:
        save_run_state(run_state_from(summary, result, now=services.now))
    return summary, result
