from __future__ import annotations

from datetime import UTC, datetime
from statistics import mean

from . import __version__
from .aggregate import MixedTelemetryError, aggregate_summary, field_coverage, source_kinds, telemetry_kind
from .economics import TaskEconomicsReport, build_savings_scenarios, calculate_task_economics
from .events import TaskEvent
from .models import (
    AggregateAnalysisSummary,
    AnalysisReport,
    AnalysisSummary,
    DataQualityIssue,
    DeploymentAnalysis,
    DeploymentSummary,
    DiagnosticCoverage,
    Finding,
    RuleEvaluation,
    TraceRecord,
)
from .pricing import PricingCatalog, PricingResolution, canonical_model_name, resolve_trace_cost
from .ptu import analyze_ptu
from .pricing_sources.assumptions import (
    ASSUMPTIONS_BANNER,
    ASSUMPTIONS_WARNING,
    DEFAULT_PRICING_ASSUMPTIONS,
)
from .reference import load_effective_reference_catalog
from .tasks import reconstruct_tasks
from .rules import RULES, evaluate_rules
from .workloads import (
    WorkloadIdentity,
    WorkloadMapping,
    build_portfolio,
    merge_identities,
    task_metrics_by_workload,
)


DEFAULT_REPORT_CONFIG = {
    "overview_min_impact_percent": 1.0,
    "overview_min_impact_tokens": 100000,
    "overview_max_findings": 3,
}


def _parse_when(value: str | None) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _common_value(resolutions: list[PricingResolution], field: str):
    values = {getattr(item, field) for item in resolutions if getattr(item, field) is not None}
    return values.pop() if len(values) == 1 else None


def _cost_summary(
    records: list[TraceRecord],
    *,
    when: datetime,
    customer_catalog: PricingCatalog | None,
    reference_catalog: PricingCatalog | None,
    required_currency: str,
    applied_assumptions: set[str] | None = None,
) -> dict[str, object]:
    resolutions = [
        resolve_trace_cost(
            record,
            when=_parse_when(record.timestamp) if record.timestamp else when,
            customer_catalog=customer_catalog,
            reference_catalog=reference_catalog,
            required_currency=required_currency,
        )
        for record in records
    ]
    resolved = [item for item in resolutions if item.resolved and item.cost_usd is not None]
    unresolved = [item for item in resolutions if not (item.resolved and item.cost_usd is not None)]
    if applied_assumptions is not None:
        # Only a rate that actually priced an analyzed record can contribute an
        # assumption. An observed cost, or a customer rate with no defaults,
        # contributes none — which is exactly what the report must show.
        applied_assumptions.update(
            dimension for item in resolved for dimension in item.assumed_dimensions
        )
    resolved_tokens = sum(
        record.usage.input_tokens + record.usage.output_tokens
        for record, resolution in zip(records, resolutions)
        if resolution.resolved
    )
    unresolved_tokens = sum(
        record.usage.input_tokens + record.usage.output_tokens
        for record, resolution in zip(records, resolutions)
        if not resolution.resolved
    )
    total_tokens = sum(record.usage.input_tokens + record.usage.output_tokens for record in records)
    all_components = bool(resolved) and all(
        item.fresh_input_cost_usd is not None
        and item.cached_input_cost_usd is not None
        and item.output_cost_usd is not None
        for item in resolved
    )
    sources = {item.source for item in resolved}
    catalogs = {item.catalog_name for item in resolved if item.catalog_name}
    effective_dates = {item.effective_from.isoformat() for item in resolved if item.effective_from}
    billing_bases = {item.billing_basis for item in resolved if item.billing_basis}
    publishers = {item.publisher for item in resolved if item.publisher}
    confidences = {item.confidence for item in resolved if item.confidence}
    unresolved_reasons = sorted({item.unresolved_reason for item in unresolved if item.unresolved_reason})
    suggested_override_keys = sorted({item.suggested_override_key for item in unresolved if item.suggested_override_key})
    return {
        "estimated_cost_usd": sum(item.cost_usd or 0 for item in resolved) if resolved else None,
        "fresh_input_cost_usd": (
            sum(item.fresh_input_cost_usd or 0 for item in resolved) if all_components else None
        ),
        "cached_input_cost_usd": (
            sum(item.cached_input_cost_usd or 0 for item in resolved) if all_components else None
        ),
        "output_cost_usd": (
            sum(item.output_cost_usd or 0 for item in resolved) if all_components else None
        ),
        "pricing_coverage_requests_percent": round(len(resolved) / max(1, len(records)) * 100, 1),
        "pricing_coverage_tokens_percent": round(
            resolved_tokens
            / max(1, total_tokens)
            * 100,
            1,
        ),
        "pricing_complete": len(resolved) == len(records) and resolved_tokens == total_tokens,
        "pricing_currency": required_currency,
        "pricing_source": sources.pop() if len(sources) == 1 else "mixed" if sources else "unresolved",
        "pricing_catalog_name": catalogs.pop() if len(catalogs) == 1 else "mixed" if catalogs else None,
        "pricing_effective_from": (
            effective_dates.pop() if len(effective_dates) == 1 else "mixed" if effective_dates else None
        ),
        "input_price_per_million": _common_value(resolved, "input_per_million"),
        "cached_input_price_per_million": _common_value(resolved, "cached_input_per_million"),
        "output_price_per_million": _common_value(resolved, "output_per_million"),
        "pricing_billing_basis": billing_bases.pop() if len(billing_bases) == 1 else "mixed" if billing_bases else None,
        "pricing_publisher": publishers.pop() if len(publishers) == 1 else "mixed" if publishers else None,
        "pricing_confidence": confidences.pop() if len(confidences) == 1 else "mixed" if confidences else None,
        "unresolved_requests": len(unresolved),
        "unresolved_tokens": unresolved_tokens,
        "unresolved_reasons": unresolved_reasons,
        "suggested_override_keys": suggested_override_keys,
    }


def _impact(findings: list[Finding], input_tokens: int, request_count: int) -> tuple[int, int]:
    estimates = [finding.estimated_savings for finding in findings if finding.estimated_savings.unit == "tokens"]
    # Independent findings overlap (for example, cached prefixes and repeated
    # context). Never add them into a portfolio total; expose the largest
    # individual opportunity until a sequential replay exists.
    best = max(
        estimates,
        key=lambda estimate: estimate.max_tokens if estimate.max_tokens is not None else estimate.min_tokens or 0,
        default=None,
    )
    minimum = min((best.min_tokens if best and best.min_tokens is not None else 0), input_tokens)
    maximum = min(
        (best.max_tokens if best and best.max_tokens is not None else minimum),
        input_tokens,
    )
    maximum = max(minimum, maximum)
    return minimum, maximum


def _with_impact(findings: list[Finding], input_tokens: int, request_count: int) -> list[Finding]:
    denominator = max(1, input_tokens)
    request_denominator = max(1, request_count)
    result = []
    for finding in findings:
        estimate = finding.estimated_savings
        divisor = denominator if estimate.unit == "tokens" else request_denominator
        minimum = estimate.min_tokens
        maximum = estimate.max_tokens if estimate.max_tokens is not None else minimum
        result.append(
            finding.model_copy(
                update={
                    "impact_min_percent": (
                        min(100.0, round((minimum or 0) / divisor * 100, 1))
                        if minimum is not None
                        else None
                    ),
                    "impact_max_percent": (
                        min(100.0, round((maximum or 0) / divisor * 100, 1))
                        if maximum is not None
                        else None
                    ),
                }
            )
        )
    return result


def _summary(
    records: list[TraceRecord],
    findings: list[Finding],
    *,
    deployment_name: str | None = None,
    model_name: str | None = None,
    provider: str = "unknown",
    canonical_model_key: str = "unknown",
    resource_name: str | None = None,
    project_name: str | None = None,
    total_requests: int | None = None,
    total_tokens: int | None = None,
    pricing_when: datetime,
    customer_catalog: PricingCatalog | None,
    reference_catalog: PricingCatalog | None,
    pricing_currency: str,
    evaluations: list[RuleEvaluation] | None = None,
    applied_assumptions: set[str] | None = None,
) -> AnalysisSummary | DeploymentSummary:
    aggregate_source = bool(records) and all(telemetry_kind(record) == "aggregate" for record in records)
    aggregate: AggregateAnalysisSummary | None = aggregate_summary(records) if aggregate_source else None
    coverage = field_coverage(aggregate) if aggregate else {}
    input_tokens = sum(record.usage.input_tokens for record in records)
    output_tokens = sum(record.usage.output_tokens for record in records)
    cached_tokens = sum(record.usage.cached_tokens for record in records)
    minimum, maximum = _impact(findings, input_tokens, len(records))
    denominator = max(1, input_tokens)
    high = sum(finding.severity == "high" for finding in findings)
    medium = sum(finding.severity == "medium" for finding in findings)
    low = sum(finding.severity == "low" for finding in findings)
    info = sum(finding.severity == "info" for finding in findings)
    statuses = evaluations or []

    if aggregate is not None:
        requests_observed = aggregate.requests_observed
        requests_available = requests_observed is not None
        analysis_unit = "metric_buckets"
        # Request counts come from the request metric. A bucket is an interval,
        # not a request, so it is never used as a denominator.
        requests_analyzed = requests_observed or 0
        retries_available = coverage.get("retries", False)
        cached_available = coverage.get("cached_tokens", False)
        input_available = coverage.get("input_tokens", False)
        output_available = coverage.get("output_tokens", False)
        cached_tokens = aggregate.cached_tokens or 0
        average_tokens = (
            round((input_tokens + output_tokens) / requests_observed, 2)
            if requests_observed
            else None
        )
    else:
        requests_observed = len(records)
        requests_available = True
        analysis_unit = "requests"
        requests_analyzed = len(records)
        retries_available = True
        cached_available = True
        input_available = True
        output_available = True
        average_tokens = round((input_tokens + output_tokens) / max(1, len(records)), 1) if records else 0.0

    common = {
        "analysis_unit": analysis_unit,
        "requests_analyzed": requests_analyzed,
        "requests_observed": requests_observed,
        "requests_available": requests_available,
        "retries_available": retries_available,
        "cached_tokens_available": cached_available,
        "input_tokens_available": input_available,
        "output_tokens_available": output_available,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": input_tokens + output_tokens,
        "retries": sum(bool(record.retry_of) for record in records) if retries_available else 0,
        "findings": len(findings),
        "high_findings": high,
        "medium_findings": medium,
        "low_findings": low,
        "info_findings": info,
        "evaluated_rules": sum(item.status in {"finding", "no_issue"} for item in statuses),
        "not_evaluated_rules": sum(item.status == "not_evaluated" for item in statuses),
        "addressable_min_tokens": minimum,
        "addressable_max_tokens": maximum,
        "addressable_min_percent": round(minimum / denominator * 100, 1),
        "addressable_max_percent": round(maximum / denominator * 100, 1),
        "addressable_aggregation": "largest_individual_opportunity",
        "average_tokens_per_request": average_tokens,
        "aggregate": aggregate,
        "average_latency_ms": (
            round(mean([record.latency_ms for record in records if record.latency_ms is not None]), 1)
            if any(record.latency_ms is not None for record in records)
            else None
        ),
    }
    pricing = _cost_summary(
        records,
        when=pricing_when,
        customer_catalog=customer_catalog,
        reference_catalog=reference_catalog,
        required_currency=pricing_currency,
        applied_assumptions=applied_assumptions,
    )
    common.update(
        {
            key: pricing[key]
            for key in (
                "estimated_cost_usd",
                "fresh_input_cost_usd",
                "cached_input_cost_usd",
                "output_cost_usd",
                "pricing_coverage_requests_percent",
                "pricing_coverage_tokens_percent",
                "pricing_complete",
                "pricing_currency",
                "pricing_catalog_name",
                "pricing_source",
                "pricing_billing_basis",
                "pricing_publisher",
                "pricing_confidence",
                "unresolved_requests",
                "unresolved_tokens",
                "unresolved_reasons",
                "suggested_override_keys",
            )
        }
    )
    common["pricing_coverage_basis"] = "metric_buckets" if aggregate is not None else "requests"
    common["pricing_status"] = _pricing_status(
        records,
        estimated_cost=pricing["estimated_cost_usd"],
        unresolved_reasons=list(pricing["unresolved_reasons"]),
        coverage_tokens_percent=float(pricing["pricing_coverage_tokens_percent"]),
    )
    service_tiers = {record.service_tier for record in records}
    common["service_tier"] = service_tiers.pop() if len(service_tiers) == 1 else "mixed"
    common["pricing_source"] = str(common["pricing_source"])
    if deployment_name is None:
        return AnalysisSummary(**common)
    request_total = total_requests or 0
    token_total = total_tokens or 0
    return DeploymentSummary(
        **common,
        deployment_name=deployment_name,
        model_name=model_name or "unknown",
        model_version=next(
            (
                str(version)
                for record in records
                if (version := (record.metadata or {}).get("model_version"))
            ),
            None,
        ),
        canonical_model_key=canonical_model_key,
        provider=provider,
        deployment_mode=records[0].deployment_mode if records else "unknown",
        resource_name=resource_name,
        project_name=project_name,
        request_share_percent=round((requests_observed or 0) / max(1, request_total) * 100, 1),
        token_share_percent=round((input_tokens + output_tokens) / max(1, token_total) * 100, 1),
        pricing_effective_from=pricing["pricing_effective_from"],
        input_price_per_million=pricing["input_price_per_million"],
        cached_input_price_per_million=pricing["cached_input_price_per_million"],
        output_price_per_million=pricing["output_price_per_million"],
    )


def _model_identity_resolved(records: list[TraceRecord]) -> bool:
    return bool(records) and any(
        (record.model_name or "").strip().casefold() not in {"", "unknown", "none"} for record in records
    )


def _pricing_status(
    records: list[TraceRecord],
    *,
    estimated_cost: float | None,
    unresolved_reasons: list[str],
    coverage_tokens_percent: float,
) -> str:
    """Separate identity failure from catalog coverage.

    "0% priced" is a symptom. The report must say whether the model was never
    identified, whether the catalog has no exact entry, or whether a currency
    policy blocked the match.
    """
    if not _model_identity_resolved(records):
        return "identity_unresolved"
    if "currency-conversion-required" in unresolved_reasons:
        return "currency_mismatch"
    if estimated_cost is None:
        return "catalog_missing"
    if coverage_tokens_percent < 100:
        return "partial"
    return "priced"


def _sorted_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda finding: (
            {"high": 0, "medium": 1, "low": 2, "info": 3}[finding.severity],
            finding.rule_id,
        ),
    )


def analyze(
    records: list[TraceRecord],
    source: str,
    *,
    generated_at: str | None = None,
    report_config: dict[str, object] | None = None,
    customer_catalog: PricingCatalog | None = None,
    reference_catalog: PricingCatalog | None = None,
    use_bundled_reference: bool = True,
    mixed_source_policy: str = "reject",
    data_classification: str = "unknown",
    source_files: int | None = None,
    workload_mappings: list[WorkloadMapping] | None = None,
    workload_identities: list[WorkloadIdentity] | None = None,
    task_events: list[TaskEvent | dict[str, object]] | None = None,
) -> AnalysisReport:
    """Analyze all records once and expose the same rule engine per deployment.

    ``mixed_source_policy`` defaults to ``reject``: request telemetry and Azure
    Monitor buckets usually describe the same traffic, so analyzing them together
    double counts tokens, requests, and cost. ``allow`` is an explicit merge
    policy for callers that have proven the two sources do not overlap.
    """
    kinds = source_kinds(records)
    if len(kinds) > 1 and mixed_source_policy != "allow":
        raise MixedTelemetryError(
            "This input mixes request-level telemetry with aggregate Azure Monitor buckets. "
            "The same traffic would be counted twice, so analysis stopped. Analyze each source "
            "separately, or pass an explicit merge policy once you have proven they do not overlap."
        )
    rule_run = evaluate_rules(records)
    overall_findings = _with_impact(
        rule_run.findings, sum(r.usage.input_tokens for r in records), len(records)
    )
    applied_report_config = DEFAULT_REPORT_CONFIG | {
        key: value for key, value in (report_config or {}).items() if key in DEFAULT_REPORT_CONFIG
    }
    generated = generated_at or datetime.now(UTC).isoformat()
    pricing_when = _parse_when(generated)
    if reference_catalog is None and use_bundled_reference:
        # The cached official snapshot is preferred over the packaged
        # catalog; both are read from disk, never from a network.
        reference_catalog = load_effective_reference_catalog()
    pricing_currency = (
        "USD"
        if any(record.observed_cost_usd is not None for record in records)
        else customer_catalog.currency.upper()
        if customer_catalog is not None
        else reference_catalog.currency.upper()
        if reference_catalog is not None
        else "USD"
    )
    # Only the defaults that priced an analyzed record are reported as applied.
    applied_assumptions: set[str] = set()
    summary = _summary(
        records,
        overall_findings,
        pricing_when=pricing_when,
        customer_catalog=customer_catalog,
        reference_catalog=reference_catalog,
        pricing_currency=pricing_currency,
        evaluations=rule_run.evaluations,
        applied_assumptions=applied_assumptions,
    )
    total_requests = summary.requests_observed or 0
    total_tokens = summary.total_tokens
    grouped: dict[tuple[str, str, str, str, str], list[TraceRecord]] = {}
    for record in records:
        grouped.setdefault(
            (
                record.resource_name or "",
                record.project_name or "",
                record.deployment_name or "unknown",
                record.model_name or "unknown",
                record.deployment_mode or "unknown",
            ),
            [],
        ).append(record)
    deployments: list[DeploymentAnalysis] = []
    for (_resource, _project, name, model_name, _mode), deployment_records in sorted(
        grouped.items(),
        key=lambda item: sum(r.usage.input_tokens + r.usage.output_tokens for r in item[1]),
        reverse=True,
    ):
        first = deployment_records[0]
        deployment_run = evaluate_rules(deployment_records)
        findings = _with_impact(
            deployment_run.findings,
            sum(r.usage.input_tokens for r in deployment_records),
            len(deployment_records),
        )
        deployments.append(
            DeploymentAnalysis(
                summary=_summary(
                    deployment_records,
                    findings,
                    deployment_name=name,
                    model_name=model_name,
                    canonical_model_key=canonical_model_name(model_name),
                    provider=first.provider,
                    resource_name=first.resource_name,
                    project_name=first.project_name,
                    total_requests=total_requests,
                    total_tokens=total_tokens,
                    pricing_when=pricing_when,
                    customer_catalog=customer_catalog,
                    reference_catalog=reference_catalog,
                    pricing_currency=pricing_currency,
                    evaluations=deployment_run.evaluations,
                ),
                findings=_sorted_findings(findings),
                diagnostics=DiagnosticCoverage(
                    source=deployment_run.source,  # type: ignore[arg-type]
                    evaluations=deployment_run.evaluations,
                ),
            )
        )
    report = AnalysisReport(
        version=__version__,
        generated_at=generated,
        source=source,
        data_classification=data_classification,  # type: ignore[arg-type]
        summary=summary,
        findings=_sorted_findings(overall_findings),
        deployments=deployments,
        diagnostics=DiagnosticCoverage(
            source=rule_run.source,  # type: ignore[arg-type]
            evaluations=rule_run.evaluations,
        ),
        data_quality=_data_quality(summary, deployments),
        rules=RULES,
        report_metadata={
            "materiality": applied_report_config,
            "scenario_aggregation": "not_combined_due_to_overlap",
            "scenarios": [scenario.model_dump(mode="json") for scenario in build_savings_scenarios(findings=overall_findings)],
            "telemetry_source": rule_run.source,
            "source_files": source_files,
            "pricing": _pricing_metadata(
                summary,
                customer_catalog=customer_catalog,
                reference_catalog=reference_catalog,
                applied_assumptions=applied_assumptions,
            ),
        },
    )
    # The PTU dashboard's daily cost must agree with Cost analysis exactly, so
    # it reuses the same resolver rather than recomputing any rate.
    def _ptu_cost_resolver(record: TraceRecord) -> PricingResolution:
        return resolve_trace_cost(
            record,
            when=_parse_when(record.timestamp) if record.timestamp else pricing_when,
            customer_catalog=customer_catalog,
            reference_catalog=reference_catalog,
            required_currency=pricing_currency,
        )

    report.ptu_analysis = analyze_ptu(records, deployments, cost_resolver=_ptu_cost_resolver)
    # Workload economics reuses the same resolver, so workload totals reconcile
    # exactly with the deployment and model totals rendered elsewhere.
    observed_deployments = [item.summary.deployment_name for item in deployments]
    identities = (
        list(workload_identities)
        if workload_identities is not None
        else merge_identities(observed_deployments, mappings=workload_mappings or ())
    )
    task_metrics = {}
    if task_events:
        trajectories = reconstruct_tasks(
            task_events,
            customer_catalog=customer_catalog,
            reference_catalog=reference_catalog,
            required_currency=pricing_currency,
        )
        task_metrics = task_metrics_by_workload(trajectories)
        report.task_economics = calculate_task_economics(trajectories)
    report.workloads = build_portfolio(
        records,
        cost_resolver=_ptu_cost_resolver,
        identities=identities,
        mappings=workload_mappings or (),
        reporting_currency=pricing_currency,
        task_metrics=task_metrics,
    )
    return report


def _pricing_metadata(
    summary: AnalysisSummary,
    *,
    customer_catalog: PricingCatalog | None,
    reference_catalog: PricingCatalog | None,
    applied_assumptions: set[str] | None = None,
) -> dict[str, object]:
    """Separate catalogs *consulted* from the catalog entry actually selected."""
    consulted = [
        catalog.catalog_name
        for catalog in (customer_catalog, reference_catalog)
        if catalog is not None
    ]
    selected = summary.pricing_catalog_name
    selected_catalog = next(
        (
            catalog
            for catalog in (customer_catalog, reference_catalog)
            if catalog is not None and catalog.catalog_name == selected
        ),
        None,
    )
    matched = summary.estimated_cost_usd is not None
    # What the catalogs *could* have assumed, versus what actually priced a
    # record. Only the second is a claim about this report.
    catalog_available = sorted(
        {
            item
            for catalog in (customer_catalog, reference_catalog)
            if catalog is not None
            for item in catalog.assumed_dimensions()
        }
    )
    applied = sorted(applied_assumptions or set())
    provenance: list[dict[str, object]] = []
    try:
        from .pricing_sources.sync import public_pricing_provenance

        provenance = public_pricing_provenance(currency=summary.pricing_currency)
    except Exception:  # noqa: BLE001 - provenance is additive, never load-bearing
        provenance = []
    return {
        "currency": summary.pricing_currency,
        "catalogs_consulted": consulted,
        "catalog_selected": selected if matched else None,
        "catalog_name": selected,
        "customer_catalog_name": customer_catalog.catalog_name if customer_catalog else None,
        "source_url": selected_catalog.source_url if selected_catalog and matched else None,
        # A retrieval date is only meaningful for an entry that actually matched.
        "retrieved_at": (
            selected_catalog.retrieved_at.isoformat()
            if matched and selected_catalog and selected_catalog.retrieved_at
            else None
        ),
        "coverage_requests_percent": summary.pricing_coverage_requests_percent,
        "coverage_tokens_percent": summary.pricing_coverage_tokens_percent,
        "coverage_basis": summary.pricing_coverage_basis,
        "pricing_status": summary.pricing_status,
        "estimate_only": True,
        "pricing_basis": selected_catalog.pricing_basis if selected_catalog else None,
        "currency_policy": "single_currency_no_conversion",
        # The defaults are always stated, whether or not any of them was needed,
        # so a reader never has to infer which dimensions were assumed.
        "assumptions": DEFAULT_PRICING_ASSUMPTIONS.to_metadata(),
        "assumptions_banner": ASSUMPTIONS_BANNER,
        "assumptions_warning": ASSUMPTIONS_WARNING,
        "assumed_dimensions_applied": applied,
        "catalog_assumed_dimensions_available": catalog_available,
        "public_sources": provenance,
    }


_PRICING_STATUS_TITLES = {
    "identity_unresolved": "Pricing not attempted — model identity unresolved",
    "catalog_missing": "Model identified, exact rate missing",
    "currency_mismatch": "Pricing requires currency conversion",
    "partial": "Partial pricing coverage",
}


def _data_quality(summary: AnalysisSummary, deployments: list[DeploymentAnalysis]) -> list[DataQualityIssue]:
    """State plainly what in this report is trustworthy and what is not."""
    issues: list[DataQualityIssue] = []
    unresolved = [
        item.summary.deployment_name
        for item in deployments
        if item.summary.model_name.strip().casefold() in {"", "unknown", "none"}
    ]
    if unresolved:
        issues.append(
            DataQualityIssue(
                code="identity_unresolved",
                severity="blocker",
                title="Model identity unresolved",
                detail=(
                    f"{len(unresolved)} deployment(s) have no resolved model. Pricing, PTU capacity, and model "
                    "comparisons are withheld until collection resolves the exact model and version."
                ),
            )
        )
    if not summary.requests_available:
        issues.append(
            DataQualityIssue(
                code="requests_unavailable",
                severity="blocker",
                title="Request totals unavailable",
                detail=(
                    "The telemetry source reported no request metric, so request counts, averages per request, "
                    "and outcome rates are unavailable rather than zero."
                ),
            )
        )
    aggregate = summary.aggregate
    if aggregate is not None:
        if aggregate.outcome_coverage != "complete":
            issues.append(
                DataQualityIssue(
                    code="outcomes_incomplete",
                    severity="warning",
                    title="Request outcomes incomplete",
                    detail=(
                        "Status-code coverage is "
                        f"{aggregate.outcome_coverage}, so success, HTTP 429, and other-failure counts are not "
                        "derived. A zero rate-limit count is only reported with complete coverage."
                    ),
                )
            )
        for field_name, label in (("input_tokens", "Input-token"), ("output_tokens", "Output-token")):
            if getattr(aggregate, field_name) is None:
                issues.append(
                    DataQualityIssue(
                        code=f"{field_name}_unavailable",
                        severity="blocker",
                        title=f"{label} metric unavailable",
                        detail=(
                            f"The source reported no {label.lower()} series, so token totals, averages, and cost "
                            "for this field are unavailable rather than zero."
                        ),
                    )
                )
        if aggregate.cached_tokens is None:
            issues.append(
                DataQualityIssue(
                    code="cached_tokens_unavailable",
                    severity="warning",
                    title="Cached-token metric unavailable",
                    detail="Cached input is shown as unavailable, never as zero.",
                )
            )
        if aggregate.latency_coverage_percent == 0:
            issues.append(
                DataQualityIssue(
                    code="latency_unavailable",
                    severity="warning",
                    title="Latency metric unavailable",
                    detail="No latency series was returned for the selected window.",
                )
            )
    if summary.pricing_status != "priced":
        issues.append(
            DataQualityIssue(
                code=f"pricing_{summary.pricing_status}",
                severity="warning",
                title=_PRICING_STATUS_TITLES.get(summary.pricing_status, "Pricing unresolved"),
                detail=(
                    f"{summary.pricing_coverage_tokens_percent:.1f}% of observed tokens are priced. "
                    "Unpriced volume stays visible and is never folded into the cost total."
                ),
            )
        )
    return issues


def analyze_task_events(
    events: list[TaskEvent | dict[str, object]],
    source: str,
    *,
    generated_at: str | None = None,
    customer_catalog: PricingCatalog | None = None,
    reference_catalog: PricingCatalog | None = None,
    provisional_closed_tasks: int = 30,
    ranked_closed_tasks: int = 100,
) -> TaskEconomicsReport:
    """Analyze explicit v2 task events without retaining raw identifiers."""
    trajectories = reconstruct_tasks(
        events,
        customer_catalog=customer_catalog,
        reference_catalog=reference_catalog,
    )
    report = calculate_task_economics(
        trajectories,
        provisional_closed_tasks=provisional_closed_tasks,
        ranked_closed_tasks=ranked_closed_tasks,
    )
    report.report_period["source"] = "stdin" if source == "stdin" else "task-event-stream"
    report.report_period["generated_at"] = generated_at or datetime.now(UTC).isoformat()
    return report
