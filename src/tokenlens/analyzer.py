from __future__ import annotations

from datetime import UTC, datetime
from statistics import mean

from . import __version__
from .models import (
    AnalysisReport,
    AnalysisSummary,
    DeploymentAnalysis,
    DeploymentSummary,
    Finding,
    TraceRecord,
)
from .rules import RULES, run_rules


DEFAULT_REPORT_CONFIG = {
    "overview_min_impact_percent": 1.0,
    "overview_min_impact_tokens": 100000,
    "overview_max_findings": 3,
}


def _impact(findings: list[Finding], input_tokens: int, request_count: int) -> tuple[int, int]:
    estimates = [finding.estimated_savings for finding in findings if finding.estimated_savings.unit == "tokens"]
    minimum = min(sum(estimate.min_tokens or 0 for estimate in estimates), input_tokens)
    maximum = min(max(sum(estimate.max_tokens or 0 for estimate in estimates), minimum), input_tokens)
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
        "average_tokens_per_request": round((input_tokens + output_tokens) / max(1, len(records)), 1),
        "average_latency_ms": (
            round(mean([record.latency_ms for record in records if record.latency_ms is not None]), 1)
            if any(record.latency_ms is not None for record in records)
            else None
        ),
    }
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
        resource_name=resource_name,
        project_name=project_name,
        request_share_percent=round(len(records) / max(1, request_total) * 100, 1),
        token_share_percent=round((input_tokens + output_tokens) / max(1, token_total) * 100, 1),
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
) -> AnalysisReport:
    """Analyze all records once and expose the same rule engine per deployment."""
    overall_findings = _with_impact(run_rules(records), sum(r.usage.input_tokens for r in records), len(records))
    applied_report_config = DEFAULT_REPORT_CONFIG | {
        key: value for key, value in (report_config or {}).items() if key in DEFAULT_REPORT_CONFIG
    }
    summary = _summary(records, overall_findings)
    total_requests = len(records)
    total_tokens = summary.total_tokens
    grouped: dict[str, list[TraceRecord]] = {}
    for record in records:
        grouped.setdefault(record.deployment_name or "unknown", []).append(record)
    deployments: list[DeploymentAnalysis] = []
    for name, deployment_records in sorted(
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
                    model_name=first.model_name,
                    canonical_model_key=first.model_name.casefold().strip(),
                    provider=first.provider,
                    resource_name=first.resource_name,
                    project_name=first.project_name,
                    total_requests=total_requests,
                    total_tokens=total_tokens,
                ),
                findings=_sorted_findings(findings),
            )
        )
    return AnalysisReport(
        version=__version__,
        generated_at=generated_at or datetime.now(UTC).isoformat(),
        source=source,
        summary=summary,
        findings=_sorted_findings(overall_findings),
        deployments=deployments,
        rules=RULES,
        report_metadata={"materiality": applied_report_config},
    )
