"""The guided Foundry workflow: four steps, then collect and report.

The normal path asks where to look (all accessible subscriptions or one
specific subscription), which Foundry account(s) to read, and how long a window
to analyze. Deployments are never a question: every deployment discovered in the
chosen scope is shown and collected, because a partial selection silently
removes evidence from cost and PTU conclusions.

Everything else — deployment mode, provider family, inference API, endpoints,
and the Azure Monitor regional endpoint — is detected from exact Azure metadata.
Pricing gaps and business workload enrichment are reported rather than asked
about, and neither blocks usage collection.
"""

from __future__ import annotations

import os
from typing import Sequence

from ..pricing import PricingCatalog
from ..workloads import WorkloadMapping, canonical_workload_id, safe_account_scope
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
    LOOKBACK_CHOICES,
    AccountOption,
    CollectionSummary,
    DeploymentRecord,
    FoundryAccountTarget,
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
from .pricing import (
    ASSUMPTIONS_BANNER,
    ASSUMPTIONS_WARNING,
    PRICING_ASSUMPTIONS_NOTE,
    PricingReadiness,
    load_customer_catalog,
    pricing_readiness,
)
from .prompts import PHASES, Choice, Prompter, WorkflowProgress, symbol

__all__ = [
    "TOTAL_STEPS",
    "collect_and_report",
    "configure",
    "pricing_explanation_lines",
    "preflight",
    "sync_public_pricing_if_needed",
    "workload_summary_lines",
    "configure_workloads",
]

#: Four steps: where to look, which account(s), the discovered deployments, and
#: the analysis window. Optional business workload enrichment enriches report
#: semantics rather than enabling collection, so it is not counted here.
TOTAL_STEPS = 4

#: Scope of one run. Choosing "all" never means "the first one that answered":
#: every accessible subscription is enumerated and every Foundry account in it
#: is collected.
SUBSCRIPTION_SCOPES: tuple[tuple[str, str, str], ...] = (
    (
        "all",
        "All accessible subscriptions",
        "Every Foundry/Azure OpenAI account the signed-in principal can read",
    ),
    (
        "specific",
        "Specific subscription",
        "One subscription you choose from the Azure CLI account list",
    ),
)

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


def _subscription_options(services: WorkflowServices) -> list[SubscriptionOption]:
    try:
        return list(services.subscriptions())
    except Exception:  # noqa: BLE001 - the Azure CLI is optional, never fatal
        return []


def _select_scope(
    prompter: Prompter,
    services: WorkflowServices,
    *,
    configured_scope: str,
    configured_subscription: str | None,
    explicit: str | None,
    all_subscriptions: bool | None,
) -> tuple[str, list[SubscriptionOption]]:
    """Ask where to look before asking what to read.

    The first question is the scope: every accessible subscription, or one
    specific subscription. "All" is implemented literally — every accessible
    subscription is enumerated and every Foundry account in each one is
    collected, never just the first that answered.
    """
    options = _subscription_options(services)
    if explicit:
        match = next((item for item in options if item.subscription_id == explicit), None)
        return "account", [match or SubscriptionOption(subscription_id=explicit, name=explicit)]
    if all_subscriptions:
        if not options:
            raise WorkflowError(
                "No accessible subscription could be listed, so 'all subscriptions' has nothing to "
                "enumerate. Run `az login`, or pass --subscription for one explicit subscription."
            )
        return "all_subscriptions", options
    if not prompter.interactive:
        if configured_scope == "all_subscriptions":
            if options:
                return "all_subscriptions", options
            raise NoninteractiveError(
                "The saved scope is all subscriptions, but accessible subscriptions could not "
                "be enumerated. Run `az login`, or reconfigure with one explicit subscription."
            )
        if configured_subscription:
            match = next((item for item in options if item.subscription_id == configured_subscription), None)
            return configured_scope if configured_scope != "all_subscriptions" else "account", [
                match or SubscriptionOption(subscription_id=configured_subscription, name=configured_subscription)
            ]
        raise NoninteractiveError(
            "A subscription is required. Pass --subscription (or --all-subscriptions) or configure "
            "one with `tokenlens-azure foundry configure`. TokenLens never falls back to an "
            "ambiguous ambient Azure CLI context in a noninteractive run."
        )
    scope_choices: list[Choice] = []
    for value, label, detail in SUBSCRIPTION_SCOPES:
        if value == "all" and options:
            detail = f"{detail} · {len(options)} accessible"
        scope_choices.append(
            Choice(
                value,
                label,
                detail,
                selected=(value == "all") == (configured_scope == "all_subscriptions"),
            )
        )
    scope = prompter.select(
        "Which subscriptions should TokenLens read?",
        scope_choices,
        default="all" if configured_scope == "all_subscriptions" else "specific",
    )
    if scope == "all":
        if not options:
            raise WorkflowError(
                "No accessible subscription could be listed, so 'all subscriptions' has nothing to "
                "enumerate. Run `az login`, then start the workflow again."
            )
        prompter.table(
            "Subscriptions in scope",
            ["Subscription", "ID", "Azure CLI"],
            [
                [item.name, item.short_id, "active" if item.is_default else ""]
                for item in options
            ],
            caption=f"{len(options)} subscription(s) will be enumerated for Foundry accounts.",
        )
        return "all_subscriptions", options
    if not options:
        value = prompter.text(
            "Azure subscription ID (run `az login` to list them automatically)",
            default=configured_subscription or "",
            allow_empty=False,
        )
        if not value:
            raise WorkflowError("A subscription is required.")
        return "account", [SubscriptionOption(subscription_id=value, name=value)]
    choices = [
        Choice(
            item.subscription_id,
            item.label(),
            selected=item.subscription_id == configured_subscription,
        )
        for item in options
    ]
    chosen = prompter.select(
        "Azure subscription", choices, default=configured_subscription or options[0].subscription_id
    )
    return "account", [item for item in options if item.subscription_id == chosen]


def _select_accounts(
    prompter: Prompter,
    accounts: Sequence[AccountOption],
    *,
    scope: str,
    configured: Sequence[str],
    explicit_account: str | None,
    explicit_resource_group: str | None,
) -> tuple[list[AccountOption], str]:
    """Resolve which discovered accounts this run reads.

    An all-subscriptions run reads every discovered account. A single
    subscription still offers one account or all of them, so a focused run
    stays possible without hiding anything that exists.
    """
    if not accounts:
        raise WorkflowError(
            "No Foundry or Azure OpenAI account was found in the selected scope. Confirm the "
            "subscription selection and that the signed-in principal can read Cognitive Services "
            "accounts."
        )
    if explicit_account:
        matched = [
            item
            for item in accounts
            if item.name == explicit_account
            and (not explicit_resource_group or item.resource_group == explicit_resource_group)
        ]
        if not matched:
            location = (
                f" in resource group {explicit_resource_group}" if explicit_resource_group else ""
            )
            raise WorkflowError(
                f"Account {explicit_account} was not found{location} for the selected scope."
            )
        return matched[:1], "account"
    if scope == "all_subscriptions":
        return list(accounts), "all_subscriptions"
    if not prompter.interactive:
        matched = [item for item in accounts if item.name in set(configured)]
        if matched:
            return matched, "subscription" if len(matched) > 1 else "account"
        raise NoninteractiveError(
            "An account is required. Pass --account (and --resource-group) explicitly, or "
            "--all-subscriptions to read every accessible account."
        )
    choices = [
        Choice(
            "*",
            f"All Foundry accounts in this subscription ({len(accounts)})",
            "Every account and every deployment in it",
            selected=len(configured) != 1,
        )
    ] + [
        Choice(item.key, item.label(), selected=item.name in set(configured))
        for item in accounts
    ]
    chosen = prompter.select("Foundry account", choices, default="*")
    if chosen == "*":
        return list(accounts), "subscription"
    return [item for item in accounts if item.key == chosen], "account"


def _deployment_detail(deployment: DeploymentRecord, readiness: PricingReadiness | None) -> str:
    parts = [deployment.mode_label]
    if deployment.provider_family != "unknown":
        parts.append(deployment.provider_family.replace("_", " "))
    if deployment.capacity is not None:
        parts.append(f"capacity {deployment.capacity}")
    if readiness is not None:
        parts.append(f"{symbol('✓') if readiness.resolved else symbol('⚠')} {readiness.label}")
    return " · ".join(parts)


def _show_inventory(
    prompter: Prompter,
    inventory: Sequence[tuple[AccountOption, list[DeploymentRecord]]],
    *,
    readiness: dict[str, PricingReadiness],
    multi_account: bool,
) -> None:
    """Show every discovered deployment; all of them are always collected."""
    headers = ["Deployment", "Model", "Mode", "Pricing"]
    if multi_account:
        headers.insert(0, "Account")
    rows: list[list[str]] = []
    for account, deployments in inventory:
        for item in deployments:
            state = readiness.get(item.name)
            marker = symbol("✓") if state is not None and state.resolved else symbol("⚠")
            row = [
                item.name,
                f"{item.model or 'model unknown'}" + (f" v{item.model_version}" if item.model_version else ""),
                item.mode_label,
                f"{marker} {state.label}" if state is not None else "not evaluated",
            ]
            if multi_account:
                row.insert(0, account.name)
            rows.append(row)
    prompter.table(
        "Deployments discovered in scope",
        headers,
        rows,
        caption=(
            f"All {len(rows)} deployment(s) are collected. Use "
            "`tokenlens-azure foundry collect --deployment NAME` for a narrower automated run."
        ),
    )


def _explicit_selection(
    inventory: Sequence[tuple[AccountOption, list[DeploymentRecord]]],
    explicit: Sequence[str],
) -> list[tuple[AccountOption, list[DeploymentRecord]]]:
    """Apply an explicit --deployment list, rejecting any unknown name."""
    wanted = {str(name).casefold(): index for index, name in enumerate(explicit)}
    narrowed: list[tuple[AccountOption, list[DeploymentRecord]]] = []
    found: set[str] = set()
    for account, deployments in inventory:
        selected = [item for item in deployments if item.name.casefold() in wanted]
        # The caller's order is preserved so an explicit automated run reports
        # its deployments in the order it asked for them.
        selected.sort(key=lambda item: wanted[item.name.casefold()])
        found.update(item.name.casefold() for item in selected)
        if selected:
            narrowed.append((account, selected))
    missing = [str(name) for name in explicit if str(name).casefold() not in found]
    if missing:
        raise WorkflowError(
            "These deployments are not in the account inventory: " + ", ".join(missing)
        )
    return narrowed

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
    prompter.panel(
        "How the window is used",
        [
            "PTU evidence needs active buckets, not elapsed time. A long window with little",
            "traffic still provides insufficient evidence, and a recently created deployment",
            "may have less history than the window requests.",
        ],
    )
    return chosen


def workload_summary_lines(config: FoundryWorkflowConfig) -> list[str]:
    """Human-readable summary of the automatic technical workloads."""
    lines = ["Technical workloads created automatically"]
    business = {
        canonical_workload_id(deployment): mapping
        for mapping in config.workloads.mappings
        for deployment in mapping.deployments
    }
    for deployment in config.foundry.all_deployments:
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
        for deployment in config.foundry.all_deployments
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


def pricing_explanation_lines(unresolved: Sequence[PricingReadiness]) -> list[str]:
    """Explain *why* a rate is missing and the one command that fixes it.

    TokenLens never guesses a rate. A missing rate means neither the packaged
    verified catalog nor your customer catalog contains an entry for that exact
    model, version, and deployment mode — which is the case for most partner and
    marketplace models, and for a model published after the packaged snapshot.
    """
    identity = [item for item in unresolved if item.state == "identity_unresolved"]
    rate_missing = [item for item in unresolved if item.state != "identity_unresolved"]
    lines = [
        f"{len(unresolved)} deployment(s) have no exact rate in the packaged verified catalog or "
        "your customer catalog for that exact model, version, and deployment mode.",
        "The full assessment continues: usage, throughput, and PTU evidence are collected, and "
        "cost is withheld for those deployments only. A rate is never guessed from a related "
        "model or family.",
    ]
    keys = [item.suggested_override_key for item in rate_missing if item.suggested_override_key]
    if keys:
        lines.append(
            "To add a contracted rate: tokenlens-azure pricing set-rate --model "
            f"{keys[0]} --input-per-million X --output-per-million Y --effective-from YYYY-MM-DD"
        )
        lines.append(f"Rates are stored locally in {safe_display_path(customer_catalog_path())}.")
    if identity:
        lines.append(
            "Collection identity must be fixed first for: "
            + ", ".join(item.deployment for item in identity)
            + " — `unknown` is never a pricing override key."
        )
    lines.append(PRICING_ASSUMPTIONS_NOTE)
    return lines


def sync_public_pricing_if_needed(
    prompter: Prompter,
    config: FoundryWorkflowConfig,
    *,
    services: WorkflowServices | None = None,
) -> list[str]:
    """Refresh the cached official pricing snapshot when it is absent or stale.

    Analysis itself never touches a network. Only this guided step may, it is
    bounded, and any failure is reported and stepped over: the assessment
    continues on the previous snapshot, the packaged catalog, and any customer
    rates, rather than aborting or inventing a rate.

    ``TOKENLENS_NO_PRICING_SYNC=1`` disables the attempt entirely, for
    air-gapped environments, CI, and the test suite.
    """
    from ..pricing_sources.fetch import FetchBudget
    from ..pricing_sources.sync import public_sync_needed, sync_public_pricing

    if os.getenv("TOKENLENS_NO_PRICING_SYNC", "").strip() not in {"", "0", "false", "no"}:
        return ["Public pricing synchronization is disabled (TOKENLENS_NO_PRICING_SYNC)."]
    if not config.pricing.public_cache:
        return ["Public pricing synchronization is disabled in .tokenlens.yml (pricing.public_cache)."]
    deployments = config.foundry.all_deployments
    modes = sorted(
        {
            item.deployment_mode
            for item in deployments
            if item.deployment_mode in {"global", "data_zone", "regional"}
        }
    ) or None
    claude_deployments = [
        item for item in deployments if (item.publisher or "").casefold() == "anthropic"
    ]
    claude_models = sorted({item.model for item in claude_deployments if item.model})
    # Claude modes come from Claude deployments only. A data-zone GPT deployment
    # elsewhere in the portfolio must never add a data-zone premium to Claude.
    claude_modes = sorted(
        {
            item.deployment_mode
            for item in claude_deployments
            if item.deployment_mode in {"global", "data_zone", "regional"}
        }
    ) or None
    wants_claude = bool(claude_models)
    # Freshness is judged against the sources this selection actually needs, so
    # a portfolio without Claude is never told a synchronization is overdue.
    if not public_sync_needed(include_claude=wants_claude):
        return ["Cached official pricing snapshot is current; no network request was made."]
    region = next((item.region for item in config.foundry.targets if item.region), None)
    try:
        report = sync_public_pricing(
            region=region,
            account_region=region,
            deployments=modes,
            claude_models=claude_models or None,
            claude_deployment_modes=claude_modes,
            include_claude=wants_claude,
            # The guided path must never stall: one short attempt per source.
            budget=FetchBudget(timeout_seconds=10.0, retries=0),
        )
    except Exception as exc:  # noqa: BLE001 - a sync failure must never stop the assessment
        return [
            f"Official pricing synchronization failed ({type(exc).__name__}). The assessment "
            "continues with the packaged catalog and any customer rates."
        ]
    lines: list[str] = []
    for item in report.outcomes:
        if item.skipped:
            lines.append(f"{item.source}: skipped — {item.reason}")
        elif item.ok:
            lines.append(
                f"{item.source}: synchronized {item.entries} entry(ies)"
                + (f", quarantined {item.quarantined}" if item.quarantined else "")
            )
        elif not item.feed_complete:
            lines.append(
                f"{item.source}: the published feed was truncated by a bounded-read ceiling, so "
                "nothing was cached and the previous snapshot stays in effect."
            )
        else:
            lines.append(
                f"{item.source}: not synchronized ({item.error}); "
                + (
                    "the previous cached snapshot is still used."
                    if item.used_cache
                    else "the packaged catalog and customer rates still apply."
                )
            )
    return lines


def _report_pricing(
    prompter: Prompter,
    config: FoundryWorkflowConfig,
    *,
    customer_catalog: PricingCatalog | None,
) -> list[PricingReadiness]:
    """Report exact pricing readiness. There is no question to answer here.

    Asking "what next?" for an unpriced deployment only ever produced one safe
    answer, so the workflow states the reason, continues the full assessment
    with cost withheld, and prints the single remediation command.
    """
    sync_lines = sync_public_pricing_if_needed(prompter, config)
    readiness = pricing_readiness(config.foundry.all_deployments, customer_catalog=customer_catalog)
    prompter.panel(
        "Pricing assumptions",
        [ASSUMPTIONS_BANNER, ASSUMPTIONS_WARNING, *sync_lines],
    )
    prompter.table(
        "Pricing readiness",
        ["Deployment", "Model", "State"],
        [
            [
                item.deployment,
                item.model + (f" v{item.model_version}" if item.model_version else ""),
                f"{symbol('✓') if item.resolved else symbol('⚠')} {item.label}",
            ]
            for item in readiness
        ],
        caption=f"{sum(1 for item in readiness if item.resolved)}/{len(readiness)} priced exactly.",
    )
    unresolved = [item for item in readiness if not item.resolved]
    if unresolved:
        prompter.panel("Why cost is withheld", pricing_explanation_lines(unresolved), tone="warning")
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
    all_subscriptions: bool | None = None,
    configure_business_workloads: bool | None = None,
) -> tuple[FoundryWorkflowConfig, list[PricingReadiness]]:
    """Discover, select, and save a credential-free configuration.

    TokenLens always runs the full assessment, so there is no analysis-goal
    question, and every deployment discovered in the chosen scope is collected,
    so there is no per-deployment selection question either.
    """
    services = services or WorkflowServices()
    config, raw = load_config(config_path)
    missing = services.missing_packages()
    if missing:
        raise WorkflowError(
            "The Azure discovery extras are not installed ("
            + ", ".join(missing)
            + f"). Install them with: {install_command()}"
        )
    config.collection.analysis_goal = "full_assessment"

    prompter.step(1, TOTAL_STEPS, "Azure subscription scope")
    scope, subscriptions = _select_scope(
        prompter,
        services,
        configured_scope=config.foundry.scope,
        configured_subscription=(
            config.foundry.subscription_id or os.getenv(config.foundry.subscription_id_env)
        ),
        explicit=subscription,
        all_subscriptions=all_subscriptions,
    )

    prompter.step(2, TOTAL_STEPS, "Foundry accounts")
    discovery_errors: list[str] = []
    accounts = discovery.discover_accounts_in_scope(
        services.resource_client,
        subscriptions,
        resource_group=resource_group,
        on_error=lambda subscription_id, exc: discovery_errors.append(
            f"a subscription could not be read ({type(exc).__name__})"
        ),
    )
    if discovery_errors:
        prompter.panel(
            "Partial discovery",
            sorted(set(discovery_errors))
            + ["Confirm the signed-in principal can read Cognitive Services accounts there."],
            tone="warning",
        )
    configured_accounts = [item.account for item in config.foundry.targets]
    chosen_accounts, resolved_scope = _select_accounts(
        prompter,
        accounts,
        scope=scope,
        configured=configured_accounts,
        explicit_account=account,
        explicit_resource_group=resource_group,
    )

    prompter.step(3, TOTAL_STEPS, "Deployments")
    inventory_errors: list[str] = []
    inventory = discovery.discover_inventory(
        services.resource_client,
        chosen_accounts,
        on_error=lambda account_option, exc: inventory_errors.append(
            f"{account_option.name}: deployments could not be listed ({type(exc).__name__})"
        ),
    )
    if inventory_errors:
        prompter.panel(
            "Accounts skipped",
            sorted(set(inventory_errors))
            + ["Confirm the signed-in principal can read deployments in that account."],
            tone="warning",
        )
    if list(deployments):
        inventory = _explicit_selection(inventory, list(deployments))
    customer_catalog = load_customer_catalog()
    all_records = [item for _account, records in inventory for item in records]
    if not all_records:
        raise WorkflowError(
            "No deployment was found in the selected scope. Confirm the account selection, or "
            "create a deployment in the Foundry account first."
        )
    readiness_map = {
        item.deployment: item
        for item in pricing_readiness(all_records, customer_catalog=customer_catalog)
    }
    _show_inventory(
        prompter,
        inventory,
        readiness=readiness_map,
        multi_account=len(chosen_accounts) > 1,
    )

    prompter.step(4, TOTAL_STEPS, "Analysis window")
    lookback = _select_lookback(prompter, configured=config.collection.lookback_days, explicit=days)

    targets: list[FoundryAccountTarget] = []
    empty_accounts: list[str] = []
    for account_option, records in inventory:
        if not records:
            # An account with no deployment has nothing to collect; it is named
            # rather than silently folded into the selection.
            empty_accounts.append(account_option.name)
            continue
        metadata = services.resource_client(account_option.subscription_id).get_account(
            account_option.resource_group, account_option.name
        )
        targets.append(
            FoundryAccountTarget(
                subscription_id=account_option.subscription_id,
                resource_group=account_option.resource_group,
                account=account_option.name,
                region=str(metadata.get("location") or account_option.location or "") or None,
                metrics_endpoint=config.foundry.metrics_endpoint,
                deployments=records,
            )
        )
    if empty_accounts:
        prompter.panel(
            "Accounts without deployments",
            [", ".join(sorted(empty_accounts))],
            tone="warning",
        )
    primary = targets[0]
    config.foundry.scope = resolved_scope  # type: ignore[assignment]
    config.foundry.subscription_id = primary.subscription_id
    config.foundry.subscription_ids = (
        [item.subscription_id for item in subscriptions]
        if resolved_scope == "all_subscriptions"
        else [item.subscription_id for item in targets if item.subscription_id]
    )
    config.foundry.accounts = targets
    config.foundry.resource_group = primary.resource_group
    config.foundry.account = primary.account
    config.foundry.region = primary.region
    config.foundry.deployments = list(primary.deployments)
    config.collection.lookback_days = lookback
    config = refresh_identities(
        config,
        account_scope=safe_account_scope(config.foundry.account, config.foundry.resource_group),
    )

    prompter.panel("Workloads", workload_summary_lines(config))
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
        prompter.panel("Workloads", workload_summary_lines(config))

    readiness = _report_pricing(prompter, config, customer_catalog=customer_catalog)
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
    """Collect every selected deployment, then analyze and open the report.

    The run writes into its own telemetry directory and the report is generated
    from that directory alone, so a previous run's files — including any slice
    whose identity was never resolved — can never re-enter a fresh report.
    """
    services = services or WorkflowServices()
    customer_catalog = load_customer_catalog()
    selected = config.foundry.all_deployments
    result: ReportResult | None = None
    with WorkflowProgress(prompter, deployments=len(selected)) as progress:
        progress.phase(PHASES[0], f"{len(config.foundry.targets)} account(s)")
        progress.phase(PHASES[1], f"{len(selected)} deployment(s)")
        summary = collect_deployments(
            config,
            services=services,
            progress=progress.item,
            customer_catalog=customer_catalog,
        )
        if summary.succeeded:
            progress.phase(PHASES[2], f"{summary.records_written:,} record(s)")
            # Only this run's directory is analyzed. Historical telemetry stays
            # on disk untouched and is never mixed into the current report.
            sources = [summary.run_dir] if summary.run_dir else None
            result = generate_report(
                config,
                services=services,
                inputs=sources,
                open_report=open_report,
                output_format=output_format,
                customer_catalog=customer_catalog,
            )
            progress.phase(PHASES[3], result.display_path)
        progress.finish()
    if write_run_state:
        save_run_state(run_state_from(summary, result, now=services.now))
    return summary, result
