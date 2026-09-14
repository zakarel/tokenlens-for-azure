from __future__ import annotations

from datetime import UTC, datetime
from statistics import mean

from . import __version__
from .economics import TaskEconomicsReport, build_savings_scenarios, calculate_task_economics
from .events import TaskEvent
from .models import (
    AnalysisReport,
    AnalysisSummary,
    DeploymentAnalysis,
    DeploymentSummary,
    Finding,
    TraceRecord,
)
from .pricing import PricingCatalog, PricingResolution, canonical_model_name, resolve_trace_cost
from .ptu import analyze_ptu
from .reference import load_bundled_reference_catalog
from .tasks import reconstruct_tasks
from .rules import RULES, run_rules


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
    resolved_tokens = sum(
        record.usage.input_tokens + record.usage.output_tokens
        for record, resolution in zip(records, resolutions)
        if resolution.resolved
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
) -> AnalysisSummary | DeploymentSummary:
    input_tokens = sum(record.usage.input_tokens for record in records)
    output_tokens = sum(record.usage.output_tokens for record in records)
    cached_tokens = sum(record.usage.cached_tokens for record in records)
    minimum, maximum = _impact(findings, input_tokens, len(records))
    denominator = max(1, input_tokens)
    high = sum(finding.severity == "high" for finding in findings)
    medium = sum(finding.severity == "medium" for finding in findings)
    low = sum(finding.severity == "low" for finding in findings)
    info = sum(finding.severity == "info" for finding in findings)
    common = {
        "requests_analyzed": len(records),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": input_tokens + output_tokens,
        "retries": sum(bool(record.retry_of) for record in records),
        "findings": len(findings),
        "high_findings": high,
        "medium_findings": medium,
        "low_findings": low,
        "info_findings": info,
        "addressable_min_tokens": minimum,
        "addressable_max_tokens": maximum,
        "addressable_min_percent": round(minimum / denominator * 100, 1),
        "addressable_max_percent": round(maximum / denominator * 100, 1),
        "addressable_aggregation": "largest_individual_opportunity",
        "average_tokens_per_request": round((input_tokens + output_tokens) / max(1, len(records)), 1),
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
            )
        }
    )
    if deployment_name is None:
        return AnalysisSummary(**common)
    request_total = total_requests or 0
    token_total = total_tokens or 0
    return DeploymentSummary(
        **common,
        deployment_name=deployment_name,
        model_name=model_name or "unknown",
        canonical_model_key=canonical_model_key,
        provider=provider,
        deployment_mode=records[0].deployment_mode if records else "unknown",
        resource_name=resource_name,
        project_name=project_name,
        request_share_percent=round(len(records) / max(1, request_total) * 100, 1),
        token_share_percent=round((input_tokens + output_tokens) / max(1, token_total) * 100, 1),
        pricing_source=str(pricing["pricing_source"]),
        pricing_catalog_name=pricing["pricing_catalog_name"],
        pricing_effective_from=pricing["pricing_effective_from"],
        input_price_per_million=pricing["input_price_per_million"],
        cached_input_price_per_million=pricing["cached_input_price_per_million"],
        output_price_per_million=pricing["output_price_per_million"],
    )


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
) -> AnalysisReport:
    """Analyze all records once and expose the same rule engine per deployment."""
    overall_findings = _with_impact(run_rules(records), sum(r.usage.input_tokens for r in records), len(records))
    applied_report_config = DEFAULT_REPORT_CONFIG | {
        key: value for key, value in (report_config or {}).items() if key in DEFAULT_REPORT_CONFIG
    }
    generated = generated_at or datetime.now(UTC).isoformat()
    pricing_when = _parse_when(generated)
    if reference_catalog is None and use_bundled_reference:
        reference_catalog = load_bundled_reference_catalog()
    pricing_currency = (
        "USD"
        if any(record.observed_cost_usd is not None for record in records)
        else customer_catalog.currency.upper()
        if customer_catalog is not None
        else reference_catalog.currency.upper()
        if reference_catalog is not None
        else "USD"
    )
    summary = _summary(
        records,
        overall_findings,
        pricing_when=pricing_when,
        customer_catalog=customer_catalog,
        reference_catalog=reference_catalog,
        pricing_currency=pricing_currency,
    )
    total_requests = len(records)
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
        findings = _with_impact(
            run_rules(deployment_records),
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
                ),
                findings=_sorted_findings(findings),
            )
        )
    report = AnalysisReport(
        version=__version__,
        generated_at=generated,
        source=source,
        summary=summary,
        findings=_sorted_findings(overall_findings),
        deployments=deployments,
        rules=RULES,
        report_metadata={
            "materiality": applied_report_config,
            "scenario_aggregation": "not_combined_due_to_overlap",
            "scenarios": [scenario.model_dump(mode="json") for scenario in build_savings_scenarios(findings=overall_findings)],
            "pricing": {
                "currency": summary.pricing_currency,
                "catalog_name": reference_catalog.catalog_name if reference_catalog else None,
                "customer_catalog_name": customer_catalog.catalog_name if customer_catalog else None,
                "source_url": reference_catalog.source_url if reference_catalog else None,
                "retrieved_at": (
                    reference_catalog.retrieved_at.isoformat()
                    if reference_catalog and reference_catalog.retrieved_at
                    else None
                ),
                "coverage_requests_percent": summary.pricing_coverage_requests_percent,
                "coverage_tokens_percent": summary.pricing_coverage_tokens_percent,
                "estimate_only": True,
                "pricing_basis": reference_catalog.pricing_basis if reference_catalog else None,
                "currency_policy": "single_currency_no_conversion",
            },
        },
    )
    report.ptu_analysis = analyze_ptu(records, deployments)
    return report


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
