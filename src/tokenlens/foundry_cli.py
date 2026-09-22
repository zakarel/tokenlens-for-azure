"""Typer commands for the guided Foundry workflow and pricing catalogs.

These commands are thin: every decision lives in
:mod:`tokenlens.foundry_workflow`, so the same behaviour is testable without a
terminal and without Azure.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import typer

from .foundry_workflow import discovery
from .foundry_workflow.configuration import (
    customer_catalog_path,
    load_config,
    load_run_state,
    safe_display_path,
    save_config,
)
from .foundry_workflow.models import (
    DEPLOYMENT_MODE_LABELS,
    CollectionSummary,
    FoundryWorkflowConfig,
    NoninteractiveError,
    WorkflowError,
)
from .foundry_workflow.orchestration import (
    ReportResult,
    WorkflowServices,
    install_command,
    missing_collector_packages,
)
from .foundry_workflow.pricing import (
    ASSUMPTIONS_BANNER,
    ASSUMPTIONS_WARNING,
    CustomerRate,
    catalog_status,
    load_customer_catalog,
    pricing_readiness,
    verify_catalogs,
    write_customer_rate,
)
from .foundry_workflow.prompts import TyperPrompter, symbol
from .foundry_workflow.status import status_lines, status_payload, workloads_lines, workloads_payload
from .pricing_sources.assumptions import (
    PricingDimensionError,
    canonical_context,
    canonical_deployment,
)
from .pricing_sources.sync import sync_public_pricing
from .foundry_workflow.wizard import (
    collect_and_report,
    configure as run_configure,
    configure_workloads,
    preflight,
    workload_summary_lines,
)
from .workloads import WorkloadMappingError, canonical_workload_id, validate_mappings

foundry_app = typer.Typer(
    help=(
        "Guided Foundry workflow: discover deployments, collect Azure Monitor metrics, "
        "resolve pricing, and open one report.\n\n"
        "Run `tokenlens-azure foundry` for the guided path, or `foundry collect` with explicit "
        "options for automation."
    ),
    invoke_without_command=True,
)

pricing_app = typer.Typer(help="Inspect and extend local pricing catalogs. Analysis stays offline.")
workloads_app = typer.Typer(help="Technical and business workload identity.")
foundry_app.add_typer(workloads_app, name="workloads")

#: Documented exit codes for automation.
EXIT_OK = 0
EXIT_PARTIAL = 3
EXIT_FAILED = 1
EXIT_DEFERRED = 2


def _services() -> WorkflowServices:
    return WorkflowServices()


def _prompter(noninteractive: bool = False) -> TyperPrompter:
    interactive = (
        False if noninteractive else bool(sys.stdin.isatty() and sys.stdout.isatty())
    )
    return TyperPrompter(interactive=interactive)


def _fail(exc: Exception) -> None:
    typer.echo(f"error={exc}", err=True)
    raise typer.Exit(code=EXIT_FAILED)


def _print_summary(summary: CollectionSummary, result: ReportResult | None) -> None:
    typer.echo("")
    typer.echo("Collection complete" if summary.status != "failed" else "Collection failed")
    typer.echo(f"{len(summary.succeeded)} / {len(summary.outcomes)} deployments succeeded")
    typer.echo(f"{summary.lookback_days}-day window")
    if len(summary.accounts) > 1:
        typer.echo(f"accounts={', '.join(summary.accounts)}")
    header = (
        f"{'Deployment':<24}{'Identity':<12}{'Metrics':<10}{'Active':>8}{'Requests':>10}"
        f"{'Tokens':>12}  {'Pricing':<28}{'PTU evidence'}"
    )
    typer.echo(header)
    for outcome in summary.outcomes:
        if outcome.status != "succeeded":
            typer.echo(
                f"{outcome.deployment:<24}{symbol('✕')} {outcome.status} · {outcome.error_category} · "
                f"{outcome.message}"
            )
            continue
        requests = f"{outcome.requests:,}" if outcome.requests is not None else "unavailable"
        typer.echo(
            f"{outcome.deployment:<24}"
            f"{('exact' if outcome.identity_resolved else 'unresolved'):<12}"
            f"{('available' if outcome.metrics_available else 'none'):<10}"
            f"{outcome.active_buckets:>8,}{requests:>10}{outcome.tokens:>12,}  "
            f"{outcome.pricing_status:<28}{outcome.ptu_evidence}"
        )
    for conflict in summary.identity_conflicts:
        typer.echo(f"identity-conflict={conflict}")
    typer.echo(f"records-written={summary.records_written} already-present={summary.duplicates_skipped}")
    if summary.run_dir:
        typer.echo(f"run-directory={safe_display_path(summary.run_dir)}")
    if summary.unresolved_pricing:
        typer.echo(
            "pricing-withheld=" + ", ".join(summary.unresolved_pricing)
        )
        typer.echo(
            "pricing-remediation=tokenlens-azure pricing set-rate --model MODEL "
            "--input-per-million X --output-per-million Y --effective-from YYYY-MM-DD"
        )
    if result is None:
        typer.echo("report=not generated because no deployment succeeded")
        return
    portfolio = getattr(result.report, "workloads", None)
    if portfolio is not None:
        typer.echo(f"model-identity-coverage={portfolio.technical_workload_coverage_percent:.0f}%")
        typer.echo(f"token-pricing-coverage={portfolio.pricing_coverage_percent:.0f}%")
        typer.echo(
            f"business-workload-identity-coverage={portfolio.workload_identity_coverage_percent:.0f}%"
        )
        typer.echo(f"technical-workloads={len(portfolio.workloads)}")
    typer.echo(f"report={result.display_path}")
    typer.echo(f"browser-open={'succeeded' if result.opened else 'not requested or unavailable'}")


def _exit_for(summary: CollectionSummary) -> None:
    if summary.status == "failed" or summary.status == "empty":
        raise typer.Exit(code=EXIT_FAILED)
    if summary.status == "partial":
        raise typer.Exit(code=EXIT_PARTIAL)


@foundry_app.callback()
def foundry_entry(ctx: typer.Context) -> None:
    """Run the guided workflow when invoked without a subcommand."""
    if ctx.invoked_subcommand is not None:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        # A non-TTY environment must never hang waiting for an answer.
        typer.echo(ctx.get_help())
        typer.echo(
            "\nnoninteractive=run `tokenlens-azure foundry collect --subscription ... "
            "--resource-group ... --account ... --deployment ... --days 14`"
        )
        return
    prompter = _prompter()
    prompter.panel(
        "TokenLens Foundry setup",
        preflight(prompter),
    )
    missing = missing_collector_packages()
    if missing:
        typer.echo(
            "The Azure collector extras are required before discovery can run. Install them with:"
        )
        typer.echo(f"  {install_command()}")
        if not typer.confirm("Install them now?", default=False):
            typer.echo("cancelled=nothing was installed or changed")
            raise typer.Exit(code=EXIT_FAILED)
        typer.echo("Run the command above, then start `tokenlens-azure foundry` again.")
        raise typer.Exit(code=EXIT_FAILED)
    config, _raw = load_config(None)
    if config.configured:
        prompter.panel(
            "Saved setup",
            [
                f"scope={config.foundry.scope}",
                f"accounts={', '.join(item.account for item in config.foundry.targets)}",
                f"region={config.foundry.region or 'unknown'}",
                f"deployments={', '.join(config.foundry.deployment_names)}",
                f"window={config.collection.lookback_days} days",
            ],
        )
        choice = typer.prompt("Refresh this setup or reconfigure it? [refresh/reconfigure]", default="refresh")
        if str(choice).strip().casefold().startswith("recon"):
            config, _readiness = run_configure(prompter, services=_services())
    else:
        config, _readiness = run_configure(prompter, services=_services())
    try:
        summary, result = collect_and_report(prompter, config, services=_services())
    except (WorkflowError, WorkloadMappingError) as exc:
        _fail(exc)
        return
    _print_summary(summary, result)
    _exit_for(summary)


@foundry_app.command("configure")
def foundry_configure(
    subscription: str | None = typer.Option(None, "--subscription", help="Explicit subscription ID."),
    all_subscriptions: bool = typer.Option(
        False,
        "--all-subscriptions",
        help="Read every accessible subscription and every Foundry account in them.",
    ),
    resource_group: str | None = typer.Option(None, "--resource-group", help="Resource group of the account."),
    account: str | None = typer.Option(None, "--account", help="Foundry/Azure OpenAI account name."),
    deployment: list[str] = typer.Option([], "--deployment", help="Deployment to select; repeat for more."),
    days: int | None = typer.Option(None, "--days", help="Lookback window, 1-90 days."),
    workloads: bool | None = typer.Option(
        None, "--workloads/--no-workloads", help="Configure business workload identity now."
    ),
    noninteractive: bool = typer.Option(False, "--noninteractive", help="Never prompt; fail on ambiguity."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Discover Azure resources and save a credential-free configuration.

    This command contacts Azure Resource Manager. It writes no credential, key,
    token, or connection string.
    """
    typer.echo("azure-access=this command queries Azure Resource Manager")
    prompter = _prompter(noninteractive)
    try:
        saved, readiness = run_configure(
            prompter,
            services=_services(),
            config_path=config,
            subscription=subscription,
            resource_group=resource_group,
            account=account,
            deployments=deployment,
            days=days,
            all_subscriptions=all_subscriptions or None,
            configure_business_workloads=workloads,
        )
    except (WorkflowError, NoninteractiveError, WorkloadMappingError) as exc:
        _fail(exc)
        return
    typer.echo(f"configuration={safe_display_path(config or '.tokenlens.yml')}")
    typer.echo(f"scope={saved.foundry.scope}")
    typer.echo(f"accounts={', '.join(item.account for item in saved.foundry.targets)}")
    typer.echo(f"account={saved.foundry.account} region={saved.foundry.region or 'unknown'}")
    for item in saved.foundry.all_deployments:
        typer.echo(
            f"deployment={item.name} model={item.model or 'unknown'} "
            f"version={item.model_version or 'unknown'} sku={item.sku or 'unknown'} "
            f"mode={DEPLOYMENT_MODE_LABELS[item.deployment_mode]} "
            f"family={item.provider_family} api={item.inference_api}"
        )
    typer.echo(f"lookback-days={saved.collection.lookback_days}")
    typer.echo(f"pricing-resolved={sum(1 for item in readiness if item.resolved)}/{len(readiness)}")
    typer.echo("credentials-stored=none")


@foundry_app.command("collect")
def foundry_collect(
    subscription: str | None = typer.Option(None, "--subscription", help="Explicit subscription ID."),
    all_subscriptions: bool = typer.Option(
        False,
        "--all-subscriptions",
        help="Collect every Foundry account in every accessible subscription.",
    ),
    resource_group: str | None = typer.Option(None, "--resource-group", help="Resource group of the account."),
    account: str | None = typer.Option(None, "--account", help="Foundry/Azure OpenAI account name."),
    deployment: list[str] = typer.Option([], "--deployment", help="Deployment to collect; repeat for more."),
    days: int | None = typer.Option(None, "--days", help="Lookback window, 1-90 days."),
    output_format: str = typer.Option("html", "--format", help="html or json."),
    output_dir: str | None = typer.Option(None, "--output-dir", help="Directory for the generated report."),
    open_report: bool = typer.Option(
        False, "--open/--no-open", help="Open the HTML report. Off by default in automation."
    ),
    task_events: str | None = typer.Option(
        None,
        "--task-events",
        help=(
            "Local directory of task-economics events. Task metrics drill down beneath the "
            "workload each event is explicitly tagged with."
        ),
    ),
    noninteractive: bool = typer.Option(True, "--noninteractive/--interactive", help="Never prompt."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Collect the selected deployments and generate a report.

    This command contacts Azure Resource Manager and Azure Monitor. It collects
    aggregate counters only: no prompts, responses, headers, user identifiers, or
    IP addresses.
    """
    typer.echo("azure-access=this command queries Azure Resource Manager and Azure Monitor")
    prompter = _prompter(noninteractive)
    try:
        saved, _readiness = run_configure(
            prompter,
            services=_services(),
            config_path=config,
            subscription=subscription,
            resource_group=resource_group,
            account=account,
            deployments=deployment,
            days=days,
            all_subscriptions=all_subscriptions or None,
            configure_business_workloads=False,
        )
        if output_dir:
            saved.report.output_dir = output_dir
        if task_events:
            saved.collection.task_events_dir = task_events
        saved.report.format = output_format.casefold()  # type: ignore[assignment]
        saved.report.open = open_report
        summary, result = collect_and_report(
            prompter,
            saved,
            services=_services(),
            open_report=open_report,
            output_format=output_format.casefold(),
        )
    except (WorkflowError, NoninteractiveError, WorkloadMappingError) as exc:
        _fail(exc)
        return
    _print_summary(summary, result)
    _exit_for(summary)


@foundry_app.command("refresh")
def foundry_refresh(
    open_report: bool | None = typer.Option(None, "--open/--no-open", help="Open the HTML report."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Re-collect the configured window idempotently and regenerate the report."""
    typer.echo("azure-access=this command queries Azure Resource Manager and Azure Monitor")
    try:
        saved, _raw = load_config(config)
        if not saved.configured:
            raise WorkflowError(
                "No saved Foundry configuration was found. Run `tokenlens-azure foundry configure` first."
            )
        summary, result = collect_and_report(
            _prompter(noninteractive=True), saved, services=_services(), open_report=open_report
        )
    except (WorkflowError, WorkloadMappingError) as exc:
        _fail(exc)
        return
    _print_summary(summary, result)
    _exit_for(summary)


@foundry_app.command("status")
def foundry_status(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable status."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Report configuration, readiness, coverage, and the last run.

    No credential, access token, tenant secret, full endpoint, or absolute local
    path is printed.
    """
    try:
        if as_json:
            typer.echo(json.dumps(status_payload(path=config), indent=2))
            return
        for line in status_lines(path=config):
            typer.echo(line)
    except (WorkflowError, WorkloadMappingError) as exc:
        _fail(exc)


@foundry_app.command("pricing")
def foundry_pricing(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable pricing readiness."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Show exact pricing status per selected deployment and safe remediation."""
    try:
        saved, _raw = load_config(config)
    except (WorkflowError, WorkloadMappingError) as exc:
        _fail(exc)
        return
    customer = load_customer_catalog()
    readiness = pricing_readiness(saved.foundry.all_deployments, customer_catalog=customer)
    if as_json:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in readiness], indent=2))
        return
    if not readiness:
        typer.echo("deployments=none selected; run tokenlens-azure foundry configure")
        return
    for item in readiness:
        marker = symbol("✓") if item.resolved else symbol("⚠")
        typer.echo(
            f"{marker} {item.deployment} · {item.model}"
            + (f" v{item.model_version}" if item.model_version else "")
            + f" · {DEPLOYMENT_MODE_LABELS.get(item.deployment_mode, 'Unknown')} · {item.label}"
        )
    unresolved = [item for item in readiness if not item.resolved]
    if not unresolved:
        typer.echo("pricing=every selected deployment has an exact rate")
        return
    typer.echo("")
    typer.echo(f"unresolved={len(unresolved)} of {len(readiness)} deployment(s)")
    typer.echo(
        "reason=no packaged verified rate or customer rate matches that exact model, version, and "
        "deployment mode. TokenLens never guesses a rate, so the assessment continues with cost "
        "withheld for those deployments only."
    )
    typer.echo(
        "remediation=tokenlens-azure pricing set-rate --model MODEL --input-per-million X "
        "--output-per-million Y --effective-from YYYY-MM-DD"
    )
    typer.echo("public-sync=tokenlens-azure pricing sync (official sources, bounded and cached)")
    for item in unresolved:
        if item.state == "identity_unresolved":
            typer.echo(
                f"  {item.deployment}: collection identity must be fixed first — "
                "`unknown` is never a pricing override key"
            )
        elif item.suggested_override_key:
            typer.echo(f"  {item.deployment}: suggested override key {item.suggested_override_key}")


@workloads_app.command("list")
def workloads_list(config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml.")) -> None:
    """List every technical and business workload with its configuration state."""
    try:
        saved, _raw = load_config(config)
    except (WorkflowError, WorkloadMappingError) as exc:
        _fail(exc)
        return
    if not saved.workloads.identities:
        typer.echo("workloads=none; run tokenlens-azure foundry configure to discover deployments")
        return
    for identity in saved.workloads.identities:
        typer.echo(
            f"workload={identity.workload_id} name={identity.workload_name} "
            f"scope={identity.workload_scope} type={identity.workload_type} "
            f"source={identity.source} status={identity.configuration_status} "
            f"allocation={identity.allocation} "
            f"deployments={','.join(identity.deployment_names) or 'none'}"
            + (" stale=yes" if identity.stale else "")
        )


@workloads_app.command("configure")
def workloads_configure(
    deployment: list[str] = typer.Option([], "--deployment", help="Limit configuration to these deployments."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Enrich the default technical workloads with business identity."""
    prompter = _prompter()
    if not prompter.interactive:
        typer.echo(
            "error=workload configuration is interactive. Edit the `workloads.mappings` block in "
            ".tokenlens.yml, or start from `tokenlens-azure foundry workloads export-template`.",
            err=True,
        )
        raise typer.Exit(code=EXIT_FAILED)
    try:
        saved, raw = load_config(config)
        if not saved.foundry.deployments:
            raise WorkflowError(
                "No deployment is configured. Run `tokenlens-azure foundry configure` first."
            )
        updated = configure_workloads(prompter, saved, only=deployment or None)
        problems = validate_mappings(
            updated.workloads.mappings,
            known_deployments=[item.name for item in updated.foundry.deployments],
        )
        if problems:
            raise WorkloadMappingError("; ".join(problems))
        save_config(updated, raw, path=config)
    except (WorkflowError, WorkloadMappingError) as exc:
        _fail(exc)
        return
    for line in workload_summary_lines(updated):
        typer.echo(line)
    typer.echo(f"configuration={safe_display_path(config or '.tokenlens.yml')}")


@workloads_app.command("status")
def workloads_status(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable workload status."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Report technical coverage, business identity coverage, and gaps."""
    try:
        if as_json:
            typer.echo(json.dumps(workloads_payload(path=config), indent=2))
            return
        for line in workloads_lines(path=config):
            typer.echo(line)
    except (WorkflowError, WorkloadMappingError) as exc:
        _fail(exc)


@workloads_app.command("export-template")
def workloads_export_template(
    output: str = typer.Option("workloads-template.yml", "--output", help="Template destination."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Write a credential-free workload mapping template for review or automation.

    The template contains deployment names and workload attributes only: never a
    cost, prompt, request ID, tenant ID, or subscription ID.
    """
    try:
        saved, _raw = load_config(config)
    except (WorkflowError, WorkloadMappingError) as exc:
        _fail(exc)
        return
    names = saved.foundry.deployment_names or ["example-deployment"]
    lines = [
        "# TokenLens workload mapping template.",
        "# Copy the `workloads` block into .tokenlens.yml and edit the values.",
        "# A deployment may be dedicated to one workload only. Shared deployments",
        "# require request-level `workload` tags before cost can be allocated.",
        "workloads:",
        "  defaults:",
        "    create_for_each_deployment: true",
        "    scope: technical",
        "    type: ai_deployment",
        "  mappings:",
    ]
    for name in names:
        identifier = canonical_workload_id(name)
        lines.extend(
            [
                f"    - id: {identifier}",
                f"      name: {name}",
                "      type: agent",
                "      environment: production",
                "      allocation: dedicated",
                "      deployments:",
                f"        - {name}",
            ]
        )
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    typer.echo(f"template={safe_display_path(destination)}")
    typer.echo("contains=deployment names and workload attributes only")


@pricing_app.command("status")
def pricing_status() -> None:
    """Show which pricing catalogs are available. This command is offline."""
    for key, value in catalog_status().items():
        typer.echo(f"{key}={value}")


@pricing_app.command("verify")
def pricing_verify() -> None:
    """Validate local pricing catalogs offline: currency, expiry, and confidence."""
    problems = verify_catalogs()
    for problem in problems:
        typer.echo(f"problem={problem}")
    typer.echo(f"catalogs={'valid' if not problems else 'invalid'}")
    if problems:
        raise typer.Exit(code=EXIT_FAILED)


@pricing_app.command("sync")
def pricing_sync(
    currency: str = typer.Option("USD", "--currency", help="Billing currency to request. Never converted."),
    region: str | None = typer.Option(
        None, "--region", help="Restrict the Azure Retail Prices query to one ARM region."
    ),
    deployment_mode: list[str] = typer.Option(
        [], "--deployment-mode", help="global, data_zone, or regional. Defaults to the assumed global mode."
    ),
    context: list[str] = typer.Option(
        [], "--context", help="Context window to price: short or long. Defaults to the assumed short."
    ),
    include_claude: bool = typer.Option(
        True, "--claude/--no-claude", help="Also parse Anthropic's official published Claude pricing."
    ),
) -> None:
    """Synchronize verified public pricing from official sources, then cache it.

    Only two hosts are ever contacted, both allow-listed by exact host and path
    on the first request, on every redirect hop, and on every pagination
    continuation. Requests are bounded in time, pages, items, size, and
    retries; a feed that hits a ceiling is reported as truncated and is never
    cached over a complete snapshot. The result is stored user-locally with
    ``0600`` permissions and every later analysis reads it offline.
    """
    try:
        modes = [canonical_deployment(item) for item in deployment_mode]
        contexts = [canonical_context(item) for item in context]
    except PricingDimensionError as exc:
        typer.echo(f"error={exc}", err=True)
        raise typer.Exit(code=EXIT_FAILED) from exc
    typer.echo(f"assumptions={ASSUMPTIONS_BANNER}")
    typer.echo(f"assumption-warning={ASSUMPTIONS_WARNING}")
    report = sync_public_pricing(
        currency=currency.upper(),
        region=region,
        account_region=region,
        deployments=modes or None,
        contexts=contexts or None,
        include_claude=include_claude,
        claude_deployment_modes=modes or None,
    )
    for outcome in report.outcomes:
        typer.echo(
            f"source={outcome.source} status={outcome.status} "
            f"entries={outcome.entries} quarantined={outcome.quarantined}"
        )
        if outcome.content_hash:
            typer.echo(f"  content-hash={outcome.content_hash}")
        if outcome.retrieved_at:
            typer.echo(f"  retrieved-at={outcome.retrieved_at.isoformat()}")
        if outcome.path is not None:
            typer.echo(f"  snapshot={safe_display_path(outcome.path)} mode=0600")
        if not outcome.feed_complete:
            typer.echo("  feed=truncated; nothing was cached and no meter is reported as missing")
        if outcome.ok and not outcome.skipped and outcome.entries == 0:
            typer.echo("  published=none; the read completed and nothing matched the request")
        if outcome.reason:
            typer.echo(f"  reason={outcome.reason}")
        if outcome.error:
            typer.echo(f"  error={outcome.error}", err=True)
            typer.echo(
                "  fallback="
                + (
                    "previous cached snapshot retained"
                    if outcome.used_cache
                    else "no cached snapshot; packaged catalog and customer rates still apply"
                )
            )
    typer.echo(f"pricing-sync={'ok' if report.ok else 'partial'}")
    typer.echo("analysis=offline; the cached snapshot is read without any network request")
    if not report.ok:
        raise typer.Exit(code=EXIT_PARTIAL)


@pricing_app.command("set-rate")
def pricing_set_rate(
    model: str = typer.Option(..., "--model", help="Exact model name, for example gpt-4.1."),
    input_per_million: float = typer.Option(..., "--input-per-million", help="Input rate per 1M tokens."),
    output_per_million: float = typer.Option(..., "--output-per-million", help="Output rate per 1M tokens."),
    cached_input_per_million: float | None = typer.Option(
        None, "--cached-input-per-million", help="Cached input rate per 1M tokens, when contracted."
    ),
    currency: str = typer.Option("USD", "--currency", help="Reporting currency. No conversion is ever performed."),
    deployment_mode: str = typer.Option("unknown", "--deployment-mode", help="Deployment mode this rate applies to."),
    effective_from: str = typer.Option(..., "--effective-from", help="ISO date the rate takes effect."),
    billing_basis: str = typer.Option("token_rate", "--billing-basis", help="token_rate, claude_ccu_equivalent, or marketplace_partner_token_rate."),
    note: str | None = typer.Option(None, "--note", help="Optional discount or agreement note."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Record one exact customer/contracted rate in the user-local catalog.

    Rates are stored outside the repository with user-only permissions and are
    always labelled customer-provided.
    """
    try:
        rate = CustomerRate(
            model=model,
            currency=currency.upper(),
            input_per_million=input_per_million,
            cached_input_per_million=cached_input_per_million,
            output_per_million=output_per_million,
            deployment_mode=deployment_mode,
            effective_from=date.fromisoformat(effective_from),
            billing_basis=billing_basis,  # type: ignore[arg-type]
            note=note,
        )
    except ValueError as exc:
        typer.echo(f"error={exc}", err=True)
        raise typer.Exit(code=EXIT_FAILED) from exc
    for line in rate.summary_lines():
        typer.echo(line)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and not yes and not typer.confirm("Write this exact rate?", default=False):
        typer.echo("cancelled=no rate was written")
        return
    try:
        path = write_customer_rate(rate)
    except WorkflowError as exc:
        typer.echo(f"error={exc}", err=True)
        raise typer.Exit(code=EXIT_FAILED) from exc
    typer.echo(f"customer-catalog={safe_display_path(path)}")
    typer.echo("confidence=customer_override")


def register(app: typer.Typer) -> None:
    """Attach the workflow sub-applications to the root CLI."""
    app.add_typer(foundry_app, name="foundry")
    app.add_typer(pricing_app, name="pricing")


__all__ = ["foundry_app", "pricing_app", "register", "workloads_app"]
