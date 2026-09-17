"""Collection and report orchestration over the existing TokenLens services.

No collection or pricing logic is re-implemented here. The orchestrator reuses
:mod:`tokenlens.foundry.monitor`, :mod:`tokenlens.telemetry.writer`,
:mod:`tokenlens.analyzer`, and :mod:`tokenlens.reports`, and adds only the
per-deployment isolation, progress, idempotency, and run-state behaviour the
guided workflow needs.

Every Azure boundary is injected through :class:`WorkflowServices`, so the whole
workflow is testable offline with fake clients.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from ..analyzer import analyze
from ..ingest import InputError, load_records_many, load_task_events_many
from ..models import AnalysisReport
from ..output import timestamped_path
from ..pricing import PricingCatalog
from ..reference import load_bundled_reference_catalog
from ..reports import report_html, report_json, write_output
from ..telemetry import TelemetryConfig, TelemetryWriter
from ..workloads import WorkloadMapping, canonical_workload_id, merge_identities, safe_account_scope
from . import discovery
from .configuration import safe_display_path
from .models import (
    CollectionSummary,
    DeploymentOutcome,
    DeploymentRecord,
    FoundryAccountTarget,
    FoundryWorkflowConfig,
    RunState,
    WorkflowError,
)
from .pricing import PricingReadiness, load_customer_catalog, pricing_readiness

__all__ = [
    "COLLECTOR_PACKAGES",
    "ReportResult",
    "WorkflowServices",
    "collect_deployments",
    "dedicated_workload_assignments",
    "dependency_status",
    "generate_report",
    "install_command",
    "missing_collector_packages",
    "run_directory",
    "run_state_from",
]

#: Optional packages the Azure Monitor collection path needs.
COLLECTOR_PACKAGES = (
    "azure.identity",
    "azure.monitor.querymetrics",
    "azure.mgmt.cognitiveservices",
    "azure.mgmt.monitor",
)

#: Error classes reported per deployment. Nothing is caught broadly: each entry
#: maps to an explicit, actionable remediation in the summary.
ERROR_CATEGORIES = (
    "authentication",
    "authorization",
    "missing_dependency",
    "account_not_found",
    "deployment_not_found",
    "unsupported_provider_api",
    "metric_unavailable",
    "incompatible_dimension",
    "throttled",
    "azure_transient",
    "invalid_pricing_source",
    "file_write_failed",
    "unexpected",
)


def _has_package(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def missing_collector_packages() -> list[str]:
    return [name for name in COLLECTOR_PACKAGES if not _has_package(name)]


def install_command() -> str:
    """Return the exact install command for *this* interpreter."""
    import sys

    executable = Path(sys.executable)
    try:
        prefix = str(executable.resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        prefix = "python"
    return f'{prefix} -m pip install -e ".[foundry,foundry-claude,foundry-monitor]"'


def dependency_status() -> dict[str, str]:
    missing = missing_collector_packages()
    return {
        "collector-extras": "ready" if not missing else "missing: " + ", ".join(missing),
        "install-command": install_command() if missing else "",
    }


def _default_resource_client(subscription_id: str) -> Any:
    from ..foundry.azure_clients import AzureResourceClient

    return AzureResourceClient.create(subscription_id)


def _default_metrics_client(endpoint: str) -> Any:
    from ..foundry.azure_clients import AzureMonitorMetricsClient

    return AzureMonitorMetricsClient.create(endpoint=endpoint)


def _default_collect(**kwargs: Any) -> Any:
    from ..foundry.monitor import collect_metrics

    return collect_metrics(**kwargs)


def _default_open(path: Path) -> bool:
    import webbrowser

    try:
        return bool(webbrowser.open(path.resolve().as_uri()))
    except (OSError, webbrowser.Error):
        return False


def _default_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class WorkflowServices:
    """Every external boundary the workflow can touch, in one injectable place."""

    resource_client: Callable[[str], Any] = _default_resource_client
    metrics_client: Callable[[str], Any] = _default_metrics_client
    collect: Callable[..., Any] = _default_collect
    subscriptions: Callable[[], list[Any]] = discovery.list_subscriptions
    open_report: Callable[[Path], bool] = _default_open
    now: Callable[[], datetime] = _default_now
    missing_packages: Callable[[], list[str]] = missing_collector_packages


def _classify(exc: Exception) -> tuple[str, str]:
    """Classify a collection failure without echoing a response body."""
    from ..foundry.monitor import AuthorizationError, CollectorError, IdentityError

    name = type(exc).__name__
    if isinstance(exc, AuthorizationError):
        return "authorization", (
            "Azure rejected the request for this resource. Confirm the signed-in principal has "
            "Monitoring Reader on the account."
        )
    if isinstance(exc, IdentityError):
        return "deployment_not_found", (
            "The metric response could not be attributed to this deployment. Confirm the exact "
            "deployment name in the account inventory."
        )
    if isinstance(exc, CollectorError):
        message = str(exc)
        if "not installed" in message:
            return "missing_dependency", message
        if "429" in message or "throttl" in message.casefold():
            return "throttled", "Azure Monitor throttled this request. Retry with fewer deployments."
        if "status 5" in message:
            return "azure_transient", "Azure Monitor returned a transient service error."
        return "metric_unavailable", message
    if isinstance(exc, OSError):
        return "file_write_failed", "The collected records could not be written to the output directory."
    return "unexpected", f"Collection failed for this deployment ({name})."


def dedicated_workload_assignments(config: FoundryWorkflowConfig) -> dict[str, str]:
    """Deployment -> business workload, for *dedicated* mappings only.

    Azure Monitor reports one aggregate per deployment. A shared deployment
    therefore cannot be allocated between workloads and is never tagged.
    """
    assignments: dict[str, str] = {}
    dedicated: dict[str, str] = {}
    shared: set[str] = set()
    for mapping in config.workloads.mappings:
        for deployment in mapping.deployments:
            key = canonical_workload_id(deployment)
            if mapping.allocation == "dedicated":
                dedicated[key] = mapping.name
            else:
                shared.add(key)
    for deployment in config.foundry.all_deployments:
        key = canonical_workload_id(deployment.name)
        if key in shared:
            continue
        name = dedicated.get(key)
        if name:
            assignments[deployment.name] = name
    return assignments


def _readiness_by_deployment(
    deployments: Sequence[DeploymentRecord],
    *,
    customer_catalog: PricingCatalog | None,
) -> dict[str, PricingReadiness]:
    return {
        item.deployment: item
        for item in pricing_readiness(deployments, customer_catalog=customer_catalog)
    }


def run_directory(config: FoundryWorkflowConfig, *, now: datetime, base: Path | None = None) -> tuple[Path, str]:
    """Return the directory this run writes to, plus its run identifier.

    Every guided run is isolated in ``<output_dir>/runs/run-<UTC stamp>``. The
    report is then built from that directory alone, so a stale file left in the
    base directory by an earlier run can never re-enter a fresh report. Nothing
    is ever deleted: historical runs stay exactly where they were written.
    """
    root = Path(base if base is not None else config.collection.output_dir)
    if not config.collection.isolate_runs:
        return root, ""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = root / "runs" / f"run-{stamp}"
    suffix = 2
    while candidate.exists():
        candidate = root / "runs" / f"run-{stamp}-{suffix}"
        suffix += 1
    return candidate, candidate.name


def _ensure_private_directory(path: Path) -> None:
    """Create a directory chain that only the current user can read.

    Only levels this call creates are tightened; an existing directory's
    permissions are never changed underneath the user.
    """
    current = Path(path.anchor) if path.anchor else Path()
    for part in path.parts[1:] if path.anchor else path.parts:
        current = current / part
        if current.exists():
            continue
        current.mkdir(mode=0o700)


def _account_targets(
    config: FoundryWorkflowConfig,
    deployments: Sequence[DeploymentRecord] | None,
) -> list[FoundryAccountTarget]:
    """Every configured account, optionally narrowed to explicit deployments."""
    targets = config.foundry.targets
    if deployments is None:
        return targets
    wanted = {item.name.casefold() for item in deployments}
    narrowed: list[FoundryAccountTarget] = []
    for target in targets:
        selected = [item for item in target.deployments if item.name.casefold() in wanted]
        if selected:
            narrowed.append(target.model_copy(update={"deployments": selected}))
    return narrowed


def collect_deployments(
    config: FoundryWorkflowConfig,
    *,
    services: WorkflowServices | None = None,
    deployments: Sequence[DeploymentRecord] | None = None,
    progress: Callable[[str, str], None] | None = None,
    customer_catalog: PricingCatalog | None = None,
) -> CollectionSummary:
    """Collect Azure Monitor aggregates for every selected deployment.

    Accounts are collected in order, and one deployment's — or one account's —
    failure never stops the others. The exit status is derived from the
    outcomes: ``failed`` when nothing succeeded, ``partial`` when some did,
    ``succeeded`` when all did.
    """
    services = services or WorkflowServices()
    targets = _account_targets(config, deployments)
    if not targets or not any(target.deployments for target in targets):
        raise WorkflowError("No deployment is selected. Run `tokenlens-azure foundry configure` first.")
    incomplete = [target for target in targets if not (target.subscription_id and target.resource_group and target.account)]
    if incomplete:
        raise WorkflowError(
            "The subscription, resource group, and account must be configured before collection."
        )
    missing = services.missing_packages()
    if missing:
        raise WorkflowError(
            "The Azure Monitor collector extras are not installed ("
            + ", ".join(missing)
            + f"). Install them with: {install_command()}"
        )

    from ..foundry.monitor import CollectionWindow

    started = services.now()
    window = CollectionWindow.for_days(config.collection.lookback_days, now=started)
    assignments = dedicated_workload_assignments(config)
    destination, run_id = run_directory(config, now=started)
    _ensure_private_directory(destination)
    writer = TelemetryWriter(TelemetryConfig.from_env(output_dir=destination))
    summary = CollectionSummary(
        lookback_days=config.collection.lookback_days,
        window_start=window.start.isoformat().replace("+00:00", "Z"),
        window_end=window.end.isoformat().replace("+00:00", "Z"),
        output_dir=safe_display_path(config.collection.output_dir),
        run_dir=str(destination),
        run_id=run_id,
        accounts=[target.account for target in targets],
    )
    order = [
        f"{target.account}/{item.name}"
        for target in targets
        for item in target.deployments
    ]
    for target in targets:
        _collect_account(
            target,
            config=config,
            services=services,
            window=window,
            assignments=assignments,
            customer_catalog=customer_catalog,
            writer=writer,
            summary=summary,
            progress=progress,
        )
    summary.outcomes.sort(
        key=lambda item: order.index(item.key) if item.key in order else len(order)
    )
    summary.unresolved_pricing = [
        item.deployment
        for item in summary.outcomes
        if item.status == "succeeded" and item.pricing_status not in {"exact_public_rate", "customer_override"}
    ]
    return summary


def _collect_account(
    target: FoundryAccountTarget,
    *,
    config: FoundryWorkflowConfig,
    services: WorkflowServices,
    window: Any,
    assignments: dict[str, str],
    customer_catalog: PricingCatalog | None,
    writer: TelemetryWriter,
    summary: CollectionSummary,
    progress: Callable[[str, str], None] | None,
) -> None:
    """Collect one account's deployments into the shared run summary."""
    from ..foundry.monitor import resource_uri

    selected = list(target.deployments)
    try:
        resources = services.resource_client(target.subscription_id)
        account_metadata = resources.get_account(target.resource_group, target.account)
        region = str(account_metadata.get("location") or target.region or "")
        endpoint = discovery.metrics_endpoint_for_region(region, override=target.metrics_endpoint)
        uri = resource_uri(target.subscription_id, target.resource_group, target.account)
        available = list(resources.list_metric_definitions(uri))
    except Exception as exc:  # noqa: BLE001 - one account never hides the others
        category, message = _classify(exc)
        for deployment in selected:
            if progress is not None:
                progress(deployment.name, "failed")
            summary.outcomes.append(
                DeploymentOutcome(
                    deployment=deployment.name,
                    account=target.account,
                    resource_group=target.resource_group,
                    status="failed",
                    error_category=category,
                    message=message,
                    identity_resolved=bool(deployment.model),
                )
            )
        return

    inventory = [
        {"name": item.name, "model": item.model, "model_version": item.model_version, "sku": item.sku}
        for item in target.deployments
    ]
    # A deployment that disappeared from the account is reported explicitly
    # rather than failing later as an opaque empty metric response.
    stale: set[str] = set()
    try:
        live = {
            item.name.casefold()
            for item in discovery.discover_deployments(
                resources, target.subscription_id, target.resource_group, target.account
            )
        }
    except Exception:  # noqa: BLE001 - discovery is best effort, never fatal
        live = set()
    if live:
        stale = {item.name for item in selected if item.name.casefold() not in live}
        selected = [item for item in selected if item.name not in stale]
    readiness = _readiness_by_deployment(selected, customer_catalog=customer_catalog)

    def _collect_one(deployment: DeploymentRecord) -> tuple[DeploymentRecord, Any, Exception | None]:
        try:
            client = services.metrics_client(endpoint)
            result = services.collect(
                metrics_client=client,
                subscription_id=target.subscription_id,
                resource_group=target.resource_group,
                account=target.account,
                window=window,
                available_metrics=available,
                deployments=[deployment.name],
                deployment_inventory=inventory,
                family=_family_for(deployment),
                deployment_modes={deployment.name: deployment.deployment_mode},
                default_deployment_mode=deployment.deployment_mode,
                workload_assignments=(
                    {deployment.name: assignments[deployment.name]}
                    if deployment.name in assignments
                    else None
                ),
            )
            return deployment, result, None
        except Exception as exc:  # noqa: BLE001 - classified per deployment below
            # KeyboardInterrupt and SystemExit deliberately propagate: an abort
            # must never be reported as a per-deployment collection failure.
            return deployment, None, exc

    workers = max(1, min(int(config.collection.max_concurrency), len(selected) or 1))
    if workers == 1:
        results = [_collect_one(deployment) for deployment in selected]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_collect_one, selected))

    for name in sorted(stale):
        summary.outcomes.append(
            DeploymentOutcome(
                deployment=name,
                account=target.account,
                resource_group=target.resource_group,
                status="skipped",
                error_category="deployment_not_found",
                message=(
                    "This deployment is no longer in the account inventory. Run "
                    "`tokenlens-azure foundry configure` to update the selection."
                ),
            )
        )

    for deployment, result, error in results:
        if progress is not None:
            progress(deployment.name, "failed" if error is not None else "collected")
        readiness_state = readiness.get(deployment.name)
        pricing_status = readiness_state.state if readiness_state else "identity_unresolved"
        if error is not None:
            category, message = _classify(error)
            summary.outcomes.append(
                DeploymentOutcome(
                    deployment=deployment.name,
                    account=target.account,
                    resource_group=target.resource_group,
                    status="failed",
                    error_category=category,
                    message=message,
                    identity_resolved=bool(deployment.model),
                    pricing_status=pricing_status,
                )
            )
            continue
        try:
            before = writer.skipped_duplicates
            written = writer.write_all(result.records)
            duplicates = writer.skipped_duplicates - before
        except OSError:
            summary.outcomes.append(
                DeploymentOutcome(
                    deployment=deployment.name,
                    account=target.account,
                    resource_group=target.resource_group,
                    status="failed",
                    error_category="file_write_failed",
                    message="The collected records could not be written to the output directory.",
                    identity_resolved=bool(deployment.model),
                    pricing_status=pricing_status,
                )
            )
            continue
        tokens = sum(
            (record.metrics.input_tokens or 0) + (record.metrics.output_tokens or 0)
            for record in result.records
        )
        request_values = [
            record.metrics.requests for record in result.records if record.metrics.requests is not None
        ]
        active = getattr(result, "active_buckets", 0)
        summary.records_written += written
        summary.duplicates_skipped += duplicates
        for conflict in getattr(result, "identity_conflicts", []) or []:
            if conflict not in summary.identity_conflicts:
                summary.identity_conflicts.append(conflict)
        summary.outcomes.append(
            DeploymentOutcome(
                deployment=deployment.name,
                account=target.account,
                resource_group=target.resource_group,
                status="succeeded",
                identity_resolved=any(
                    record.model_name not in {"", "unknown"} for record in result.records
                )
                or bool(deployment.model),
                metrics_available=bool(result.records),
                active_buckets=active,
                requests=sum(request_values) if request_values else None,
                tokens=tokens,
                pricing_status=pricing_status,
                # PTU needs *active* buckets, not elapsed time. The threshold is
                # reported honestly rather than implied by the window length.
                ptu_evidence="sufficient" if active >= 2016 else "insufficient",
                records_written=written,
                duplicates_skipped=duplicates,
            )
        )


def _family_for(deployment: DeploymentRecord) -> str:
    if deployment.provider_family == "claude_foundry":
        return "claude_foundry"
    if deployment.provider_family == "partner_model":
        return "partner_model"
    return "azure_openai"



def _load_task_events(config: FoundryWorkflowConfig) -> list:
    """Load task-economics events when the user pointed at a local stream.

    Task economics is optional. When the events exist and carry an explicit
    ``workload``, they drill down beneath that workload; otherwise task metrics
    stay ``Not measured`` and workload economics remains available.
    """
    directory = config.collection.task_events_dir
    if not directory or not Path(directory).exists():
        return []
    try:
        events, _source = load_task_events_many([directory])
    except (InputError, ValueError):
        return []
    return list(events)


@dataclass
class ReportResult:
    report: AnalysisReport
    path: Path | None
    opened: bool = False
    display_path: str = ""


def generate_report(
    config: FoundryWorkflowConfig,
    *,
    services: WorkflowServices | None = None,
    inputs: Sequence[str] | None = None,
    open_report: bool | None = None,
    output_format: str | None = None,
    customer_catalog: PricingCatalog | None = None,
) -> ReportResult:
    """Analyze the collected telemetry and write the report.

    The analysis stage is offline. Workload mappings are applied here so the
    Workloads tab and the Cost analysis workload view reconcile with the
    deployment and model totals.
    """
    services = services or WorkflowServices()
    sources = list(inputs or [config.collection.output_dir])
    try:
        records, source = load_records_many(sources)
    except InputError as exc:
        raise WorkflowError(str(exc)) from exc
    if not records:
        raise WorkflowError(
            "No telemetry was found to analyze. Run `tokenlens-azure foundry collect` first."
        )
    # The provenance line states how many files were actually read, not how
    # many directories were named, so a run-scoped report never over- or
    # under-states its evidence.
    file_count = len([part for part in str(source).split(",") if part.strip()]) or len(sources)
    catalog = customer_catalog if customer_catalog is not None else load_customer_catalog()
    reference = load_bundled_reference_catalog() if config.pricing.use_reference_catalog else None
    task_events = _load_task_events(config)
    identities = merge_identities(
        config.foundry.deployment_names,
        mappings=config.workloads.mappings,
        existing=config.workloads.identities,
        account_scope=safe_account_scope(config.foundry.account, config.foundry.resource_group),
    )
    report = analyze(
        records,
        f"{file_count} local telemetry file" + ("" if file_count == 1 else "s"),
        customer_catalog=catalog,
        reference_catalog=reference,
        use_bundled_reference=config.pricing.use_reference_catalog,
        data_classification="local_real",
        source_files=file_count,
        workload_mappings=list(config.workloads.mappings),
        workload_identities=identities,
        task_events=task_events or None,
    )
    chosen_format = (output_format or config.report.format).casefold()
    if chosen_format not in {"html", "json"}:
        raise WorkflowError("The workflow report format must be html or json.")
    content = report_html(report) if chosen_format == "html" else report_json(report)
    destination = timestamped_path(report_format=chosen_format, output_dir=config.report.output_dir)
    write_output(content, str(destination))
    wants_open = config.report.open if open_report is None else open_report
    opened = bool(wants_open and chosen_format == "html" and services.open_report(destination))
    return ReportResult(
        report=report,
        path=destination,
        opened=opened,
        display_path=safe_display_path(destination),
    )


def run_state_from(
    summary: CollectionSummary,
    result: ReportResult | None,
    *,
    now: Callable[[], datetime] = _default_now,
) -> RunState:
    """Derive safe run state. No endpoint, path, token, or identifier leaks."""
    report = result.report if result is not None else None
    portfolio = getattr(report, "workloads", None) if report is not None else None
    stamp = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return RunState(
        last_collection=stamp,
        lookback_days=summary.lookback_days,
        successful_deployments=len(summary.succeeded),
        failed_deployments=len(summary.failed),
        accounts_collected=len(summary.accounts),
        identity_coverage_percent=(
            portfolio.technical_workload_coverage_percent if portfolio is not None else 0.0
        ),
        pricing_coverage_percent=(
            portfolio.pricing_coverage_percent
            if portfolio is not None
            else (report.summary.pricing_coverage_tokens_percent if report is not None else 0.0)
        ),
        business_identity_coverage_percent=(
            portfolio.workload_identity_coverage_percent if portfolio is not None else 0.0
        ),
        workloads_needing_configuration=(
            sum(
                1
                for item in portfolio.workloads
                if item.configuration_status == "needs_configuration"
            )
            if portfolio is not None
            else 0
        ),
        report=result.display_path if result is not None else None,
        report_generated_at=stamp if result is not None else None,
        run_directory=safe_display_path(summary.run_dir) if summary.run_dir else None,
        collection_status=summary.status,
    )
