from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from importlib.resources import files
from pathlib import Path

import typer

from .analyzer import analyze, analyze_task_events
from .economics import TaskEconomicsReport
from .ingest import InputError, load_records_many, load_task_events_many
from .output import choose_output, economics_text, pricing_audit_text
from .pricing import PricingCatalog
from .reference import load_effective_reference_catalog
from .presentation import impact_category, is_material, materiality_config
from .reports import report_html, report_json, report_sarif, write_output
from .telemetry import TelemetryConfig, TelemetryWriter

app = typer.Typer(
    help=(
        "Offline LLM token-efficiency diagnostics with Azure-first guidance.\n\n"
        "Start here:\n"
        "  tokenlens-azure foundry             Guided setup, collection, and report\n\n"
        "Three collection paths:\n"
        "  1. Smoke test one deployment       tokenlens-azure smoke-test-foundry\n"
        "  2. Collect application telemetry   instrument once with tokenlens.integrations, then analyze\n"
        "  3. Collect Azure Monitor metrics   tokenlens-azure foundry collect\n\n"
        "Collection commands contact Azure explicitly. Analysis is always offline."
    ),
    invoke_without_command=True,
)

from .foundry_cli import register as _register_workflow  # noqa: E402 - app must exist first

_register_workflow(app)


@app.callback()
def _entry(ctx: typer.Context) -> None:
    """Start the bounded first-run wizard only for an interactive bare invocation."""
    if ctx.invoked_subcommand is not None:
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        typer.echo(ctx.get_help())
        return
    _guided_setup()


def _has_package(name: str) -> bool:
    """Safely probe an optional dependency without importing it.

    ``find_spec`` raises when a parent package is absent, so a dotted name like
    ``opentelemetry.sdk`` must be probed defensively.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _load_config(path: str | None) -> dict:
    if not path:
        candidate = Path(".tokenlens.yml")
        if not candidate.is_file():
            return {}
        path = str(candidate)
    import yaml

    with open(path, encoding="utf-8") as source:
        return yaml.safe_load(source) or {}


def _render(report, output_format: str) -> str:
    if isinstance(report, TaskEconomicsReport):
        if output_format == "json":
            return report.model_dump_json(indent=2) + "\n"
        if output_format == "html":
            from .reports import task_economics_html

            return task_economics_html(report)
        if output_format == "sarif":
            raise typer.BadParameter("SARIF is available for legacy request analysis, not task economics")
        return economics_text(report)
    if output_format == "json":
        return report_json(report)
    if output_format == "sarif":
        return report_sarif(report)
    if output_format == "html":
        return report_html(report)
    summary = report.summary
    requests = f"{summary.requests_observed:,} requests" if summary.requests_available and summary.requests_observed is not None else "requests unavailable"
    unit = (
        f" · {summary.aggregate.active_buckets:,} active of {summary.aggregate.elapsed_buckets:,} elapsed buckets"
        if summary.aggregate is not None
        else ""
    )
    lines = [
        "TokenLens for Azure",
        "─" * 68,
        f"Analyzed {requests} · {summary.total_tokens:,} total tokens · {len(report.deployments):,} deployments{unit}",
        "",
    ]
    for issue in report.data_quality:
        lines.append(f"{issue.severity.upper():<8} {issue.title}: {issue.detail}")
    if report.data_quality:
        lines.append("")
    for deployment in report.deployments:
        item = deployment.summary
        deployment_requests = (
            f"{item.requests_observed:,} requests"
            if item.requests_available and item.requests_observed is not None
            else "requests unavailable"
        )
        lines.append(
            f"Deployment: {item.deployment_name} · model type: {item.model_name} · "
            f"{deployment_requests} · {item.total_tokens:,} tokens"
        )
    if report.deployments:
        lines.append("")
    for finding in report.findings:
        estimate = finding.estimated_savings
        if estimate.unit == "calls":
            impact = f"{estimate.min_tokens or 0:,} calls"
        elif estimate.min_tokens is None:
            impact = "Opportunity"
        else:
            impact = f"{estimate.min_tokens:,}–{estimate.max_tokens or estimate.min_tokens:,} tokens"
        if finding.impact_min_percent is None:
            impact_percent = "Opportunity"
        elif finding.impact_min_percent == finding.impact_max_percent:
            impact_percent = f"{finding.impact_min_percent:.1f}%"
        else:
            impact_percent = f"{finding.impact_min_percent:.1f}–{finding.impact_max_percent:.1f}%"
        lines.extend(
            [
                f"{finding.severity.upper():<6} {finding.rule_id}  {finding.title}",
                f"       Materiality: {impact_category(finding)}"
                + ("" if is_material(finding, materiality_config(report)) else " · Additional opportunity"),
                f"       {finding.detail}",
                f"       Impact: {impact_percent} ({impact})",
                f"       Azure action: {finding.azure_recommendation.action}",
                "",
            ]
        )
    lines.append(
        f"{len(report.rules)} rules processed · {summary.findings} evaluated finding(s) · "
        f"{report.diagnostics.not_evaluated} not evaluated · advisory result"
    )
    return "\n".join(lines) + "\n"


def _open_report(path: Path) -> bool:
    try:
        return bool(webbrowser.open(path.resolve().as_uri()))
    except (OSError, webbrowser.Error):
        return False


def _validate_format(output_format: str) -> None:
    if output_format not in {"text", "json", "sarif", "html"}:
        raise typer.BadParameter("must be text, json, sarif, or html")


def _write_report(content: str, output: str | None, *, output_format: str, output_dir: str | None, comparison: bool, open_report: bool, quiet: bool, report) -> Path | None:
    if open_report and output_format != "html":
        raise typer.BadParameter("--open requires --format html")
    destination = choose_output(output, report_format=output_format, output_dir=output_dir or ".", comparison=comparison)
    write_output(content, str(destination) if destination else None)
    if destination:
        destination = destination.resolve()
        if not quiet:
            typer.echo(f"report-path={destination}")
            typer.echo(
                (
                    f"traces={report.summary.requests_observed if report.summary.requests_available else 'unavailable'} deployments={len(report.deployments)}"
                    if hasattr(report, "summary")
                    else f"tasks={report.total_attempted_tasks} cohorts={len(report.task_types)}"
                )
            )
        if open_report:
            opened = _open_report(destination)
            if not quiet:
                typer.echo(f"browser-open={'succeeded' if opened else 'failed'}")
    return destination


def _safe_source(source: str, *, include_paths: bool) -> tuple[str, int]:
    """Summarize the input without leaking local paths into a report.

    Source paths can carry account, project, and directory names. The default
    report states how many files were read; the full paths are available only
    behind an explicit opt-in.
    """
    parts = [part.strip() for part in str(source).split(",") if part.strip()]
    count = len(parts) or 1
    if include_paths:
        return source, count
    if source == "stdin":
        return "stdin", 1
    unit = "file" if count == 1 else "files"
    return f"{count} local telemetry {unit}", count


@app.command("analyze")
def analyze_command(
    input_paths: list[str] = typer.Argument(..., metavar="INPUT", help="JSONL path, directory, glob, or - for stdin."),
    output_format: str = typer.Option("text", "--format", case_sensitive=False, help="text, json, sarif, or html."),
    output: str | None = typer.Option(None, "--output", "-o", help="Write to this exact file."),
    output_dir: str | None = typer.Option(None, "--output-dir", help="Directory for generated timestamped reports."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
    open_report: bool = typer.Option(False, "--open", help="Open a generated HTML report."),
    include_source_paths: bool = typer.Option(
        False,
        "--include-source-paths",
        help=(
            "Write local input paths into the report. Off by default: paths can contain account, "
            "project, and directory names that should not be shared."
        ),
    ),
    allow_mixed_sources: bool = typer.Option(
        False,
        "--allow-mixed-sources",
        help=(
            "Analyze request telemetry and Azure Monitor buckets together. Off by default because "
            "the same traffic would be counted twice."
        ),
    ),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Analyze one or more OpenAI-compatible JSONL traces."""
    _validate_format(output_format)
    if include_source_paths and not quiet:
        typer.echo("privacy=source paths will be written into this report")
    try:
        settings = _load_config(config)
        aliases = settings.get("task_aliases") if isinstance(settings.get("task_aliases"), dict) else None
        pricing = settings.get("pricing", {}) if isinstance(settings.get("pricing", {}), dict) else {}
        customer = PricingCatalog.model_validate(pricing["customer_catalog"]) if pricing.get("customer_catalog") else None
        reference = (
            PricingCatalog.model_validate(pricing["reference_catalog"])
            if pricing.get("reference_catalog")
            else load_effective_reference_catalog() if pricing.get("use_reference_catalog", True) else None
        )
        events, event_source = load_task_events_many(input_paths, aliases=aliases)
        if events:
            report = analyze_task_events(
                events,
                event_source,
                customer_catalog=customer,
                reference_catalog=reference,
                provisional_closed_tasks=int(settings.get("economics", {}).get("provisional_closed_tasks", 30)),
                ranked_closed_tasks=int(settings.get("economics", {}).get("ranked_closed_tasks", 100)),
            )
        else:
            records, source = load_records_many(input_paths)
            label, file_count = _safe_source(source, include_paths=include_source_paths)
            report = analyze(
                records,
                label,
                report_config=settings.get("report"),
                customer_catalog=customer,
                reference_catalog=reference,
                use_bundled_reference=pricing.get("use_reference_catalog", True),
                mixed_source_policy="allow" if allow_mixed_sources else "reject",
                # Real collection is never labelled synthetic; only the demo
                # generator may claim synthetic provenance.
                data_classification="local_real",
                source_files=file_count,
            )
        _write_report(
            _render(report, output_format),
            output,
            output_format=output_format,
            output_dir=output_dir,
            comparison=False,
            open_report=open_report,
            quiet=quiet,
            report=report,
        )
    except (InputError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("pricing-audit")
def pricing_audit(
    input_paths: list[str] = typer.Argument(..., metavar="INPUT", help="JSONL path, directory, glob, or - for stdin."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
    output: str | None = typer.Option(None, "--output", "-o", help="Write to this exact file instead of stdout."),
) -> None:
    """Report pricing coverage and unresolved reasons per model without any network access.

    Prints unique returned model IDs, deployment mode, service tier, request
    and token coverage, the selected catalog/source, the unresolved reason per
    model, and a suggested local override key. It never prints endpoints,
    resource IDs, tenant values, request IDs, or prompt/response content.
    """
    try:
        settings = _load_config(config)
        pricing = settings.get("pricing", {}) if isinstance(settings.get("pricing", {}), dict) else {}
        customer = PricingCatalog.model_validate(pricing["customer_catalog"]) if pricing.get("customer_catalog") else None
        reference = (
            PricingCatalog.model_validate(pricing["reference_catalog"])
            if pricing.get("reference_catalog")
            else load_effective_reference_catalog() if pricing.get("use_reference_catalog", True) else None
        )
        records, source = load_records_many(input_paths)
        report = analyze(
            records,
            source,
            report_config=settings.get("report"),
            customer_catalog=customer,
            reference_catalog=reference,
            use_bundled_reference=pricing.get("use_reference_catalog", True),
        )
    except (InputError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    write_output(pricing_audit_text(report), output)


@app.command()
def compare(
    baseline: str = typer.Argument(..., help="Baseline JSONL path."),
    candidate: str = typer.Argument(..., help="Candidate JSONL path."),
    output_format: str = typer.Option("text", "--format", case_sensitive=False, help="text, json, sarif, or html."),
    output: str | None = typer.Option(None, "--output", "-o", help="Write to this exact file."),
    output_dir: str | None = typer.Option(None, "--output-dir", help="Directory for generated timestamped reports."),
    fail_on_regression: float | None = typer.Option(None, "--fail-on-regression", help="Fail when addressable percentage increases by this amount."),
    open_report: bool = typer.Option(False, "--open", help="Open a generated HTML report."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Compare candidate traces against a baseline."""
    _validate_format(output_format)
    try:
        baseline_records, baseline_source = load_records_many([baseline])
        candidate_records, candidate_source = load_records_many([candidate])
        baseline_report = analyze(baseline_records, baseline_source)
        candidate_report = analyze(candidate_records, candidate_source)
    except InputError as exc:
        raise typer.BadParameter(str(exc)) from exc
    delta = candidate_report.summary.addressable_max_percent - baseline_report.summary.addressable_max_percent
    text = _render(candidate_report, output_format)
    if output_format != "html":
        text += (
            f"\nBaseline addressable range: {baseline_report.summary.addressable_max_percent:.1f}%\n"
            f"Candidate addressable range: {candidate_report.summary.addressable_max_percent:.1f}%\n"
            f"Change: {delta:+.1f} percentage points\n"
        )
    _write_report(
        text,
        output,
        output_format=output_format,
        output_dir=output_dir,
        comparison=True,
        open_report=open_report,
        quiet=quiet,
        report=candidate_report,
    )
    if fail_on_regression is not None and delta > fail_on_regression:
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Check local readiness for offline analysis, instrumentation, and collection.

    This command never makes an inference call and never prints token material.
    """
    typer.echo(f"python={sys.version.split()[0]}")
    typer.echo(f"python-supported={'yes' if sys.version_info >= (3, 11) else 'no'}")
    typer.echo(f"cwd-writable={'yes' if os.access('.', os.W_OK) else 'no'}")
    typer.echo("offline-analyzer=ready")
    extras = {
        "sdk-instrumentation (tokenlens-azure[foundry])": ("openai",),
        "claude-instrumentation (anthropic)": ("anthropic",),
        "opentelemetry (tokenlens-azure[otel])": ("opentelemetry.sdk",),
        "foundry-monitor (tokenlens-azure[foundry-monitor])": (
            "azure.identity",
            "azure.monitor.querymetrics",
            "azure.mgmt.cognitiveservices",
        ),
        "app-insights-collector (tokenlens-azure[foundry-monitor])": ("azure.monitor.query",),
    }
    for label, packages in extras.items():
        missing = [name for name in packages if not _has_package(name)]
        typer.echo(f"{label}={'ready' if not missing else 'missing: ' + ', '.join(missing)}")
    env_present = any(os.getenv(name) for name in ("AZURE_OPENAI_ENDPOINT", "FOUNDRY_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT"))
    typer.echo(f"foundry-endpoint={'configured' if env_present else 'not configured'}")
    typer.echo(f"azure-subscription-env={'configured' if os.getenv('AZURE_SUBSCRIPTION_ID') else 'not configured'}")
    typer.echo(f"fingerprint-key={'configured' if os.getenv('TOKENLENS_FINGERPRINT_KEY') else 'not configured (fingerprints disabled)'}")
    settings = {}
    config_path = Path(".tokenlens.yml")
    if config_path.is_file():
        try:
            settings = _load_config(None)
            typer.echo("configuration=valid")
        except (ValueError, OSError) as exc:
            typer.echo(f"configuration=invalid ({type(exc).__name__})")
    else:
        typer.echo("configuration=not found (run tokenlens-azure connect-foundry)")
    try:
        telemetry = TelemetryConfig.from_mapping(settings.get("telemetry") if isinstance(settings, dict) else None)
        directory = telemetry.output_dir
        writable = os.access(directory, os.W_OK) if directory.exists() else os.access(directory.parent if str(directory.parent) else ".", os.W_OK)
        typer.echo(f"telemetry-output-dir={directory} ({'writable' if writable else 'not writable'})")
        typer.echo(f"telemetry-rotation=max {telemetry.max_mb} MB · retention {telemetry.retention_days} day(s)")
        typer.echo(f"telemetry-content-capture={'disabled' if not telemetry.content_capture else 'enabled'}")
    except ValueError as exc:
        typer.echo(f"telemetry-config=invalid ({exc})")
    if _has_package("azure.identity"):
        try:
            from azure.identity import DefaultAzureCredential

            DefaultAzureCredential(exclude_interactive_browser_credential=True)
            typer.echo("entra-credential=available via DefaultAzureCredential chain")
        except Exception:
            typer.echo("entra-credential=not available; run az login and verify your role")
    else:
        typer.echo("entra-credential=install tokenlens-azure[foundry-monitor]")


@app.command("import-otel")
def import_otel(
    input_path: str = typer.Argument(..., metavar="INPUT", help="OTLP/JSON, JSON array, or JSONL span export."),
    output_dir: str = typer.Option("tokenlens-traces", "--output-dir", help="Private directory for canonical JSONL."),
    max_spans: int = typer.Option(1_000_000, "--max-spans", help="Bounded import limit."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Import GenAI OpenTelemetry spans into canonical, contentless telemetry.

    This command is offline: it reads a local export only. Content-bearing span
    attributes are counted and discarded, never written.
    """
    from .telemetry.otel import OtelImportError, import_file, write_import

    try:
        result = import_file(input_path, max_spans=max_spans)
    except OtelImportError as exc:
        raise typer.BadParameter(str(exc)) from exc
    writer = TelemetryWriter(TelemetryConfig.from_env(output_dir=Path(output_dir)))
    written = write_import(result, writer)
    if quiet:
        return
    for key, value in result.summary().items():
        typer.echo(f"{key}={value}")
    typer.echo(f"records-written={written}")
    typer.echo(f"records-already-present={writer.skipped_duplicates}")
    typer.echo(f"output-dir={Path(output_dir).resolve()}")
    typer.echo(f"dropped-events={writer.dropped_events}")


@app.command("import")
def import_generic(
    input_path: str = typer.Argument(..., metavar="INPUT", help="Existing JSONL or JSON array log export."),
    mapping_path: str = typer.Option(..., "--mapping", help="Declarative YAML mapping file (see examples/generic-log-mapping-example.yml)."),
    output_dir: str = typer.Option("tokenlens-traces", "--output-dir", help="Private directory for canonical JSONL."),
    max_rows: int = typer.Option(1_000_000, "--max-rows", help="Bounded import limit."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Import existing JSONL logs using a declarative YAML field mapping.

    Offline and safe by construction: the mapping file is parsed with
    ``yaml.safe_load`` only (no code execution), and it can only target
    TokenLens's contentless schema fields, so it has nowhere to put prompt,
    response, tool, or credential content even if a source path pointed at
    one. A source path that looks like content or a credential is rejected
    before any row is read.
    """
    from .telemetry.generic_import import GenericImportError, MappingError, import_file, write_import

    try:
        result = import_file(input_path, mapping_path, max_rows=max_rows)
    except (GenericImportError, MappingError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    writer = TelemetryWriter(TelemetryConfig.from_env(output_dir=Path(output_dir)))
    written = write_import(result, writer)
    if quiet:
        return
    for key, value in result.summary().items():
        typer.echo(f"{key}={value}")
    typer.echo(f"records-written={written}")
    typer.echo(f"records-already-present={writer.skipped_duplicates}")
    typer.echo(f"output-dir={Path(output_dir).resolve()}")
    typer.echo(f"dropped-events={writer.dropped_events}")


@app.command("import-app-insights")
def import_app_insights(
    input_path: str = typer.Argument(..., metavar="INPUT", help="Application Insights/Log Analytics export or query result (JSON or JSONL)."),
    output_dir: str = typer.Option("tokenlens-traces", "--output-dir", help="Private directory for canonical JSONL."),
    max_rows: int = typer.Option(1_000_000, "--max-rows", help="Bounded import limit."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Import an already-exported Application Insights/Log Analytics result.

    Offline: reads a local export only. Both the Logs query-result shape
    (``{"tables": [...]}}``) and the flat continuous-export shape are
    supported. This reuses the OpenTelemetry GenAI mapper, so the same
    attribute allow list and content rejection apply as ``import-otel``.
    """
    from .telemetry.appinsights import import_file
    from .telemetry.otel import OtelImportError, write_import

    try:
        result = import_file(input_path, max_rows=max_rows)
    except OtelImportError as exc:
        raise typer.BadParameter(str(exc)) from exc
    writer = TelemetryWriter(TelemetryConfig.from_env(output_dir=Path(output_dir)))
    written = write_import(result, writer)
    if quiet:
        return
    for key, value in result.summary().items():
        typer.echo(f"{key}={value}")
    typer.echo(f"records-written={written}")
    typer.echo(f"records-already-present={writer.skipped_duplicates}")
    typer.echo(f"output-dir={Path(output_dir).resolve()}")
    typer.echo(f"dropped-events={writer.dropped_events}")


@app.command("collect-app-insights")
def collect_app_insights(
    workspace_id: str = typer.Option(..., "--workspace-id", help="Log Analytics workspace ID linked to the Application Insights resource."),
    days: int = typer.Option(14, "--days", help="Lookback window in days."),
    output_dir: str = typer.Option("tokenlens-traces", "--output-dir", help="Private directory for canonical JSONL."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Query Application Insights GenAI traces and import them offline.

    This command contacts Azure Monitor Logs — it is the only Application
    Insights path that does. It runs one fixed, reviewable KQL query that
    selects GenAI usage dimensions only (never a request/response body
    column) and maps results with the same OpenTelemetry-reusing import path
    as ``import-app-insights``. Install the optional dependency with
    ``pip install 'tokenlens-azure[foundry-monitor]'``.
    """
    from .foundry.appinsights_client import AppInsightsLogsClient, tables_from_response, timespan_for_days
    from .foundry.monitor import CollectorError
    from .telemetry.appinsights import import_export
    from .telemetry.otel import write_import

    typer.echo("network=this command contacts Azure Monitor Logs")
    try:
        client = AppInsightsLogsClient.create()
        response = client.query_genai_traces(workspace_id, days=days, timespan=timespan_for_days(days))
    except CollectorError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = tables_from_response(response)
    result = import_export(payload)
    writer = TelemetryWriter(TelemetryConfig.from_env(output_dir=Path(output_dir)))
    written = write_import(result, writer)
    if quiet:
        return
    for key, value in result.summary().items():
        typer.echo(f"{key}={value}")
    typer.echo(f"records-written={written}")
    typer.echo(f"records-already-present={writer.skipped_duplicates}")
    typer.echo(f"output-dir={Path(output_dir).resolve()}")
    typer.echo(f"dropped-events={writer.dropped_events}")


def _foundry_resource_client(subscription_id: str):
    from .foundry.azure_clients import AzureResourceClient
    from .foundry.monitor import CollectorError

    try:
        return AzureResourceClient.create(subscription_id)
    except CollectorError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _foundry_metrics_client(endpoint: str):
    from .foundry.azure_clients import AzureMonitorMetricsClient
    from .foundry.monitor import CollectorError

    try:
        return AzureMonitorMetricsClient.create(endpoint=endpoint)
    except CollectorError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _azure_cli_subscription() -> str | None:
    """Read the Azure CLI's explicitly selected subscription without scanning."""
    if not shutil.which("az"):
        return None
    try:
        result = subprocess.run(
            ["az", "account", "show", "--query", "id", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    subscription = result.stdout.strip()
    return subscription if result.returncode == 0 and subscription else None


def _resolved_subscription(explicit: str | None, settings: dict) -> str:
    foundry = settings.get("foundry", {}) if isinstance(settings.get("foundry", {}), dict) else {}
    env_name = str(foundry.get("subscription_id_env") or "AZURE_SUBSCRIPTION_ID")
    configured = explicit or os.getenv(env_name)
    if configured:
        return configured
    subscription = _azure_cli_subscription()
    if not subscription:
        raise typer.BadParameter(
            f"No subscription selected. Pass --subscription, set {env_name}, or run "
            "`az account set --subscription <name-or-id>`. TokenLens uses only the "
            "Azure CLI's selected subscription and never scans every accessible subscription."
        )
    typer.echo(
        "subscription-source=azure-cli-active "
        "(verify with `az account show`; change with `az account set --subscription ...`)"
    )
    return subscription


@app.command("list-foundry-resources")
def list_foundry_resources(
    subscription: str | None = typer.Option(None, "--subscription", help="Explicit subscription ID."),
    resource_group: str | None = typer.Option(None, "--resource-group", help="Limit discovery to one resource group."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """List Foundry/Azure OpenAI accounts in one explicitly selected subscription.

    This command contacts Azure using your existing `az login` credential.
    """
    settings = _load_config(config)
    subscription_id = _resolved_subscription(subscription, settings)
    typer.echo("azure-access=this command queries Azure Resource Manager")
    resources = _foundry_resource_client(subscription_id)
    for account in resources.list_accounts(subscription_id, resource_group):
        typer.echo(f"account={account['name']} resource-group={account['resource_group']} kind={account['kind']}")


@app.command("list-foundry-deployments")
def list_foundry_deployments(
    resource_group: str = typer.Option(..., "--resource-group", help="Resource group of the account."),
    account: str = typer.Option(..., "--account", help="Foundry/Azure OpenAI account name."),
    subscription: str | None = typer.Option(None, "--subscription", help="Explicit subscription ID."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """List deployments for one account. This command contacts Azure."""
    settings = _load_config(config)
    subscription_id = _resolved_subscription(subscription, settings)
    typer.echo("azure-access=this command queries Azure Resource Manager")
    resources = _foundry_resource_client(subscription_id)
    for deployment in resources.list_deployments(subscription_id, resource_group, account):
        typer.echo(
            f"deployment={deployment['name']} model={deployment['model']} "
            f"version={deployment['model_version']} sku={deployment['sku']}"
        )


@app.command("collect-foundry-metrics")
def collect_foundry_metrics(
    resource_group: str | None = typer.Option(None, "--resource-group", help="Resource group of the account."),
    account: str | None = typer.Option(None, "--account", help="Foundry/Azure OpenAI account name."),
    subscription: str | None = typer.Option(None, "--subscription", help="Explicit subscription ID."),
    days: int = typer.Option(14, "--days", help="Lookback window in days."),
    granularity: str = typer.Option("5m", "--granularity", help="Bucket size; only 5m is supported."),
    deployment: list[str] = typer.Option([], "--deployment", help="Restrict collection to these deployments."),
    deployment_mode: str = typer.Option(
        "unknown",
        "--deployment-mode",
        help=(
            "Global or Regional, applied to every collected deployment. Unknown stays "
            "unknown; PTU sizing requires an explicit mode."
        ),
    ),
    family: str = typer.Option("azure_openai", "--family", help="azure_openai, claude_foundry, or partner_model."),
    output_dir: str = typer.Option("local-traces/foundry-metrics", "--output-dir", help="Private directory for aggregate JSONL."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Collect five-minute Azure Monitor aggregates for PTU analysis.

    This command contacts Azure Monitor. It collects aggregate counters only:
    no request bodies, prompts, responses, user IDs, IP addresses, or headers.
    """
    from .foundry.monitor import CollectionWindow, CollectorError, collect_metrics

    if granularity != "5m":
        raise typer.BadParameter("only 5m granularity is supported")
    if deployment_mode.casefold() not in {"global", "regional", "unknown"}:
        raise typer.BadParameter("deployment mode must be global, regional, or unknown")
    settings = _load_config(config)
    foundry = settings.get("foundry", {}) if isinstance(settings.get("foundry", {}), dict) else {}
    monitor_settings = settings.get("monitor", {}) if isinstance(settings.get("monitor", {}), dict) else {}
    resource_group = resource_group or foundry.get("resource_group")
    account = account or foundry.get("account")
    if not resource_group or not account:
        raise typer.BadParameter(
            "A resource group and account are required. Pass --resource-group/--account or run connect-foundry."
        )
    deployments = list(deployment) or [str(item) for item in (foundry.get("deployments") or [])]
    subscription_id = _resolved_subscription(subscription, settings)
    lookback = days or int(monitor_settings.get("lookback_days", 14))
    typer.echo("azure-access=this command queries Azure Monitor metrics")
    resources = _foundry_resource_client(subscription_id)
    from .foundry.monitor import resource_uri

    uri = resource_uri(subscription_id, resource_group, account)
    try:
        account_metadata = resources.get_account(resource_group, account)
        from .foundry.azure_clients import metrics_endpoint_for_location

        configured_endpoint = (
            monitor_settings.get("metrics_endpoint")
            or os.getenv("TOKENLENS_METRICS_ENDPOINT")
        )
        metrics_endpoint = str(
            configured_endpoint
            or metrics_endpoint_for_location(str(account_metadata.get("location") or ""))
        )
        if not quiet:
            typer.echo(
                "metrics-endpoint-source="
                + ("configured-override" if configured_endpoint else "account-location")
            )
        metrics_client = _foundry_metrics_client(metrics_endpoint)
        available = list(resources.list_metric_definitions(uri))
        inventory = _deployment_inventory(resources, subscription_id, resource_group, account, quiet=quiet)
        result = collect_metrics(
            metrics_client=metrics_client,
            subscription_id=subscription_id,
            resource_group=resource_group,
            account=account,
            window=CollectionWindow.for_days(lookback),
            available_metrics=available,
            deployments=deployments or None,
            deployment_inventory=inventory,
            family=family,
            # The confirmed mode applies to every collected deployment, including
            # an unrestricted collection where the deployment names are only
            # discovered from the returned metric dimensions.
            deployment_modes={name: deployment_mode for name in deployments} if deployments else None,
            default_deployment_mode=deployment_mode,
        )
    except CollectorError as exc:
        raise typer.BadParameter(str(exc)) from exc
    writer = TelemetryWriter(
        TelemetryConfig.from_env(output_dir=Path(output_dir)),
    )
    written = writer.write_all(result.records)
    if quiet:
        return
    for key, value in result.summary().items():
        typer.echo(f"{key}={value}")
    for metric_name, reason in getattr(metrics_client, "rejected_metrics", {}).items():
        typer.echo(f"metric-rejected={metric_name} reason={reason}")
    for conflict in result.identity_conflicts:
        typer.echo(f"identity-conflict={conflict}")
    typer.echo(f"records-written={written}")
    typer.echo(f"records-already-present={writer.skipped_duplicates}")
    typer.echo(f"output-dir={Path(output_dir).resolve()}")
    typer.echo(f"next: tokenlens-azure analyze {output_dir} --format html --open")


def _deployment_inventory(resources, subscription_id: str, resource_group: str, account: str, *, quiet: bool):
    """Read the deployment inventory used to resolve exact model identity.

    Discovery is best effort: a principal that can read metrics but not the
    account's deployments must still be able to collect, with identity resolved
    from metric dimensions alone.
    """
    lister = getattr(resources, "list_deployments", None)
    if lister is None:
        return []
    try:
        inventory = list(lister(subscription_id, resource_group, account))
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        if not quiet:
            typer.echo(f"deployment-inventory=unavailable ({type(exc).__name__})")
        return []
    if not quiet:
        typer.echo(f"deployment-inventory={len(inventory)} deployment(s)")
    return inventory


@app.command("connect-foundry")
def connect_foundry(
    resource_group: str | None = typer.Option(None, "--resource-group", help="Resource group to record in the config."),
    account: str | None = typer.Option(None, "--account", help="Foundry/Azure OpenAI account to record in the config."),
    deployment: list[str] = typer.Option([], "--deployment", help="Deployments to collect."),
    output_dir: str = typer.Option("tokenlens-traces", "--output-dir", help="Private directory for request telemetry."),
    lookback_days: int = typer.Option(14, "--lookback-days", help="Default Azure Monitor lookback."),
    noninteractive: bool = typer.Option(False, "--noninteractive", help="Never prompt; fail on ambiguity."),
) -> None:
    """Create a credential-free local setup for collection.

    Nothing here contacts Azure and no credential, token, key, or connection
    string is ever written. `az login` remains an environment prerequisite.
    """
    interactive = not noninteractive and sys.stdin.isatty() and sys.stdout.isatty()
    missing = [
        name
        for name in ("azure.identity", "azure.monitor.querymetrics", "azure.mgmt.cognitiveservices")
        if not _has_package(name)
    ]
    typer.echo(
        "collector-extras=" + ("ready" if not missing else "missing: " + ", ".join(missing))
    )
    if interactive and not resource_group:
        resource_group = typer.prompt("Resource group", default="", show_default=False) or None
    if interactive and not account:
        account = typer.prompt("Foundry account", default="", show_default=False) or None
    deployments = list(deployment)
    if interactive and not deployments:
        answer = typer.prompt("Deployments (comma separated)", default="", show_default=False)
        deployments = [item.strip() for item in answer.split(",") if item.strip()]
    if noninteractive and not (resource_group and account):
        raise typer.BadParameter("Noninteractive setup requires --resource-group and --account.")

    telemetry_dir = Path(output_dir)
    telemetry_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (telemetry_dir / ".gitignore").write_text("*\n", encoding="utf-8")
    metrics_dir = Path("local-traces/foundry-metrics")
    metrics_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (metrics_dir / ".gitignore").write_text("*\n", encoding="utf-8")

    import yaml

    existing = {}
    target = Path(".tokenlens.yml")
    if target.is_file():
        existing = _load_config(str(target)) or {}
    existing.setdefault("version", 1)
    existing["telemetry"] = {
        "output_dir": str(telemetry_dir),
        "content_capture": False,
        "rotation": {"max_mb": 50, "retention_days": 30},
        "fingerprints": {"enabled": True, "key_env": "TOKENLENS_FINGERPRINT_KEY"},
    }
    existing["foundry"] = {
        "subscription_id_env": "AZURE_SUBSCRIPTION_ID",
        **({"resource_group": resource_group} if resource_group else {}),
        **({"account": account} if account else {}),
        **({"deployments": deployments} if deployments else {}),
    }
    existing["monitor"] = {"lookback_days": lookback_days, "granularity_minutes": 5}
    target.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")
    typer.echo(f"configuration={target.resolve()}")
    typer.echo(f"telemetry-dir={telemetry_dir.resolve()}")
    typer.echo(f"metrics-dir={metrics_dir.resolve()}")
    typer.echo("credentials-stored=none")
    if resource_group and account:
        typer.echo(
            "next: tokenlens-azure collect-foundry-metrics "
            f"--resource-group {resource_group} --account {account} --days {lookback_days}"
        )
    else:
        typer.echo("next: tokenlens-azure list-foundry-resources --subscription <subscription-id>")


def _smoke_endpoint(api: str, explicit: str | None) -> str:
    """Resolve and validate the endpoint for one provider-specific smoke test."""
    if api == "anthropic":
        endpoint = explicit or (
            os.getenv("FOUNDRY_ENDPOINT")
            or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
            or os.getenv("AZURE_OPENAI_ENDPOINT")
        )
        if not endpoint:
            raise typer.BadParameter(
                "Pass --endpoint or set FOUNDRY_ENDPOINT before smoke testing."
            )
        endpoint = endpoint.rstrip("/")
        if endpoint.endswith(".services.ai.azure.com"):
            endpoint += "/anthropic"
        if ".services.ai.azure.com" not in endpoint or not endpoint.endswith("/anthropic"):
            raise typer.BadParameter(
                "Claude smoke tests require a Foundry services endpoint, for example "
                "https://RESOURCE.services.ai.azure.com or "
                "https://RESOURCE.services.ai.azure.com/anthropic."
            )
        return endpoint
    endpoint = explicit or (
        os.getenv("AZURE_OPENAI_ENDPOINT")
        or os.getenv("FOUNDRY_ENDPOINT")
        or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    )
    if not endpoint:
        raise typer.BadParameter(
            "Pass --endpoint or set AZURE_OPENAI_ENDPOINT before smoke testing."
        )
    return endpoint


@app.command("smoke-test-foundry")
def smoke_test_foundry(
    deployment: str = typer.Option(..., "--deployment", help="Deployment name to call exactly once."),
    prompt: str = typer.Option("Reply with the single word: ok.", "--prompt", help="Prompt for the single test call."),
    api: str = typer.Option("openai", "--api", help="openai (Chat Completions/Responses) or anthropic (Messages)."),
    endpoint: str | None = typer.Option(
        None,
        "--endpoint",
        help="Provider endpoint. Overrides AZURE_OPENAI_ENDPOINT or FOUNDRY_ENDPOINT.",
    ),
    output_dir: str = typer.Option("foundry-traces", "--output-dir", help="Private directory for the captured record."),
    max_output_tokens: int = typer.Option(64, "--max-output-tokens", help="Bound on the single response."),
    yes: bool = typer.Option(False, "--yes", help="Skip the billable-request confirmation."),
) -> None:
    """Make exactly one billable request to verify connectivity and normalization.

    This is a manual smoke test, not production instrumentation. Applications
    should instrument their client once with `tokenlens.integrations` instead of
    running a command per prompt.
    """
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    typer.echo("billable=this command makes exactly one billable model request")
    if interactive and not yes and not typer.confirm("Send one billable request now?", default=False):
        typer.echo("cancelled=no request was made")
        return
    writer = TelemetryWriter(TelemetryConfig.from_env(output_dir=Path(output_dir)))
    api = api.casefold()
    if api not in {"openai", "anthropic"}:
        raise typer.BadParameter("api must be openai or anthropic")
    resolved_endpoint = _smoke_endpoint(api, endpoint)
    if api == "anthropic":
        if not _has_package("anthropic") or not _has_package("azure.identity"):
            raise typer.BadParameter(
                "Install tokenlens-azure[foundry-claude] to smoke test a Claude deployment."
            )
        from anthropic import AnthropicFoundry  # type: ignore[attr-defined]
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        from .integrations.anthropic_foundry import instrument_anthropic_foundry

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://ai.azure.com/.default"
        )
        client = instrument_anthropic_foundry(
            AnthropicFoundry(
                azure_ad_token_provider=token_provider,
                base_url=resolved_endpoint,
            ),
            writer=writer,
        )
        client.messages.create(
            model=deployment,
            max_tokens=max_output_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
    elif api == "openai":
        if not _has_package("openai") or not _has_package("azure.identity"):
            raise typer.BadParameter("Install tokenlens-azure[foundry] to smoke test an Azure OpenAI deployment.")
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI

        from .integrations.openai import instrument_openai

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        client = instrument_openai(
            AzureOpenAI(
                azure_endpoint=resolved_endpoint,
                azure_ad_token_provider=token_provider,
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            ),
            writer=writer,
        )
        client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=max_output_tokens,
        )
    typer.echo(f"trace-file={writer.current_path().resolve()}")
    typer.echo(f"records-written={writer.written_events} dropped={writer.dropped_events}")
    typer.echo("note=one call per deployment cannot support PTU analysis; collect Azure Monitor metrics for that")


@app.command("init-foundry")
def init_foundry(
    directory: str = typer.Option("foundry-traces", "--directory", help="Private directory for captured JSONL traces."),
) -> None:
    """Create a private trace directory and a copyable Entra-authenticated capture example."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / ".gitignore").write_text("*.jsonl\n", encoding="utf-8")
    source = Path(__file__).resolve().parents[2] / "examples" / "capture_foundry.py"
    destination = target / "capture_foundry.py"
    if not source.is_file():
        packaged = files("tokenlens").joinpath("assets/capture_foundry.py")
        destination_content = packaged.read_text(encoding="utf-8")
        if not destination.exists():
            destination.write_text(destination_content, encoding="utf-8")
    elif not destination.exists():
        shutil.copyfile(source, destination)
    typer.echo(f"trace-directory={target.resolve()}")
    typer.echo(f"capture-example={destination.resolve()}")
    typer.echo("Run the capture example only after supplying an explicit deployment and prompt.")


@app.command("configure")
def configure(
    advanced: bool = typer.Option(False, "--advanced", help="Expose advanced local economics settings."),
) -> None:
    """Create a local, credential-free TokenLens configuration."""
    target = Path(".tokenlens.yml")
    if target.exists() and not typer.confirm("Overwrite .tokenlens.yml?", default=False):
        typer.echo("Configuration unchanged.")
        return
    payload = """version: 1
analysis:
  redact_content: true
economics:
  provisional_closed_tasks: 30
  ranked_closed_tasks: 100
pricing:
  use_reference_catalog: true
"""
    if advanced:
        payload += """report:
  overview_min_impact_percent: 1.0
  overview_min_impact_tokens: 100000
"""
    target.write_text(payload, encoding="utf-8")
    typer.echo(f"configuration={target.resolve()}")


@app.command("instrument")
def instrument(
    language: str = typer.Option("python", "--language", case_sensitive=False),
) -> None:
    """Print a local instrumentation example for the selected language."""
    examples = {
        "python": (
            "from tokenlens.instrumentation import task\n\n"
            "with task(task_id=correlation_id, task_type='ticket-classification', "
            "execution_strategy='default', strategy_version='v1') as run:\n"
            "    result = run.record_model_call(deployment='general-prod', model='example-model', call=call_model)\n"
            "    run.complete(outcome='solved', automated_check='passed')\n"
        ),
        "node": "See examples/instrumentation_node.js; append JSONL locally with explicit task metadata.\n",
        "dotnet": "See examples/Instrumentation.cs; append JSONL locally with explicit task metadata.\n",
    }
    key = language.casefold()
    if key not in examples:
        raise typer.BadParameter("language must be python, node, or dotnet")
    typer.echo(examples[key], nl=False)


def _guided_setup() -> None:
    candidates = sorted(
        path for path in Path(".").glob("**/*.jsonl")
        if path.is_file() and ".git" not in path.parts and "local-traces" not in path.parts
    )[:10]
    if not candidates:
        typer.echo("No local JSONL traces discovered. Run `tokenlens-azure analyze INPUT` or `tokenlens-azure instrument --language python`.")
        return
    selected = typer.prompt(
        "Trace input",
        default=str(candidates[0]),
        show_default=True,
    )
    use_reference = typer.confirm("Use bundled dated reference prices when customer prices do not resolve?", default=True)
    open_report = typer.confirm("Open the generated HTML report?", default=True)
    reference = load_effective_reference_catalog() if use_reference else None
    # The wizard intentionally asks no cleanup, threshold, or identity questions.
    try:
        events, source = load_task_events_many([selected])
        if events:
            report = analyze_task_events(events, source, reference_catalog=reference)
        else:
            records, source = load_records_many([selected])
            report = analyze(
                records,
                source,
                reference_catalog=reference,
                use_bundled_reference=False,
            )
        output_dir = Path("tokenlens-reports")
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / "tokenlens-report-latest.html"
        from .reports import report_html, task_economics_html

        destination.write_text(
            task_economics_html(report) if isinstance(report, TaskEconomicsReport) else report_html(report),
            encoding="utf-8",
        )
        typer.echo(f"report-path={destination.resolve()}")
        if open_report:
            _open_report(destination)
        typer.echo("Reference pricing fallback is " + ("enabled." if use_reference else "disabled."))
    except InputError as exc:
        typer.echo(f"Unable to analyze selected input: {exc}", err=True)


if __name__ == "__main__":
    app()
