"""Typed presentation helpers for the executive and analytics report views."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable

from .models import AnalysisReport, DeploymentAnalysis, Finding


@dataclass(frozen=True)
class MaterialityConfig:
    overview_min_impact_percent: float = 1.0
    overview_min_impact_tokens: int = 100000
    overview_max_findings: int = 3


@dataclass(frozen=True)
class ModelRollup:
    canonical_model_key: str
    model_name: str
    requests: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    retries: int
    addressable_min_tokens: int
    addressable_max_tokens: int
    token_share_percent: float


@dataclass(frozen=True)
class DeploymentRollup:
    deployment_name: str
    model_name: str
    canonical_model_key: str
    requests: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int
    retries: int
    addressable_min_tokens: int
    addressable_max_tokens: int
    token_share_percent: float
    average_tokens_per_request: float


def materiality_config(report: AnalysisReport) -> MaterialityConfig:
    values = report.report_metadata.get("materiality", {})
    return MaterialityConfig(
        overview_min_impact_percent=float(values.get("overview_min_impact_percent", 1.0)),
        overview_min_impact_tokens=int(values.get("overview_min_impact_tokens", 100000)),
        overview_max_findings=int(values.get("overview_max_findings", 3)),
    )


def impact_category(finding: Finding) -> str:
    maximum = finding.impact_max_percent
    if maximum is None:
        return "Evaluation opportunity"
    if maximum >= 10:
        return "Major"
    if maximum >= 5:
        return "High"
    if maximum >= 1:
        return "Moderate"
    return "Low materiality"


def _absolute_impact(finding: Finding) -> int:
    estimate = finding.estimated_savings
    return int(estimate.max_tokens or estimate.min_tokens or 0)


def is_material(finding: Finding, config: MaterialityConfig) -> bool:
    """Apply executive filtering without removing findings from the analysis."""
    if finding.impact_max_percent is not None and finding.impact_max_percent >= config.overview_min_impact_percent:
        return True
    if finding.estimated_savings.unit == "calls":
        # Call findings are measured in requests, not tokens; any observed call
        # volume is retained when the percentage threshold is not applicable.
        return _absolute_impact(finding) > 0
    return _absolute_impact(finding) >= config.overview_min_impact_tokens


def finding_rank(finding: Finding) -> tuple[float, int, int, str]:
    return (
        -(finding.impact_max_percent if finding.impact_max_percent is not None else 0.0),
        -_absolute_impact(finding),
        {"high": 0, "medium": 1, "low": 2, "info": 3}.get(finding.severity, 4),
        finding.rule_id,
    )


def overview_findings(report: AnalysisReport) -> list[Finding]:
    config = materiality_config(report)
    return sorted(
        [finding for finding in report.findings if is_material(finding, config)],
        key=finding_rank,
    )[: config.overview_max_findings]


def additional_opportunities(report: AnalysisReport) -> list[Finding]:
    config = materiality_config(report)
    visible = set(id(finding) for finding in overview_findings(report))
    return [
        finding
        for finding in sorted(report.findings, key=finding_rank)
        if id(finding) not in visible and not is_material(finding, config)
    ]


def top_recommendations(findings: Iterable[Finding], limit: int = 3) -> list[Finding]:
    chosen: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for finding in sorted(findings, key=finding_rank):
        key = (finding.azure_recommendation.service, finding.azure_recommendation.capability)
        if key in seen:
            continue
        chosen.append(finding)
        seen.add(key)
        if len(chosen) == limit:
            break
    return chosen


def _deployment_rollup(deployment: DeploymentAnalysis, portfolio_total: int) -> DeploymentRollup:
    summary = deployment.summary
    return DeploymentRollup(
        deployment_name=summary.deployment_name,
        model_name=summary.model_name,
        canonical_model_key=summary.canonical_model_key,
        requests=summary.requests_analyzed,
        input_tokens=summary.input_tokens,
        output_tokens=summary.output_tokens,
        cached_tokens=summary.cached_tokens,
        total_tokens=summary.total_tokens,
        retries=summary.retries,
        addressable_min_tokens=summary.addressable_min_tokens,
        addressable_max_tokens=summary.addressable_max_tokens,
        token_share_percent=round(summary.total_tokens / max(1, portfolio_total) * 100, 1),
        average_tokens_per_request=summary.average_tokens_per_request,
    )


def deployment_rollups(report: AnalysisReport) -> list[DeploymentRollup]:
    total = report.summary.total_tokens
    return sorted(
        [_deployment_rollup(item, total) for item in report.deployments],
        key=lambda item: (-item.total_tokens, item.deployment_name.casefold()),
    )


def model_rollups(report: AnalysisReport) -> list[ModelRollup]:
    total = report.summary.total_tokens
    grouped: OrderedDict[str, dict[str, object]] = OrderedDict()
    for deployment in report.deployments:
        item = _deployment_rollup(deployment, total)
        current = grouped.setdefault(
            item.canonical_model_key,
            {
                "model_name": item.model_name,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "total_tokens": 0,
                "retries": 0,
                "addressable_min_tokens": 0,
                "addressable_max_tokens": 0,
            },
        )
        for key in (
            "requests",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "total_tokens",
            "retries",
            "addressable_min_tokens",
            "addressable_max_tokens",
        ):
            current[key] = int(current[key]) + int(getattr(item, key))
    return sorted(
        [
            ModelRollup(
                canonical_model_key=key,
                model_name=str(values["model_name"]),
                token_share_percent=round(int(values["total_tokens"]) / max(1, total) * 100, 1),
                **{name: int(values[name]) for name in (
                    "requests",
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "total_tokens",
                    "retries",
                    "addressable_min_tokens",
                    "addressable_max_tokens",
                )},
            )
            for key, values in grouped.items()
        ],
        key=lambda item: (-item.total_tokens, item.model_name.casefold()),
    )


def chart_percent(value: int, total: int) -> float:
    return round(value / max(1, total) * 100, 1)


def chart_label(name: str, value: int, percent: float) -> str:
    return f"{name}: {value:,} tokens ({percent:.1f}% of portfolio)"
