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
    deployment_mode: str
    service_tier: str
    estimated_cost_usd: float | None
    pricing_currency: str
    pricing_coverage_requests_percent: float
    input_price_per_million: float | None
    cached_input_price_per_million: float | None
    output_price_per_million: float | None
    pricing_source: str
    pricing_billing_basis: str | None
    pricing_publisher: str | None
    pricing_confidence: str | None
    unresolved_requests: int
    unresolved_tokens: int
    unresolved_reasons: list[str]
    suggested_override_keys: list[str]


@dataclass(frozen=True)
class DeploymentRollup:
    deployment_name: str
    model_name: str
    canonical_model_key: str
    resource_name: str | None
    project_name: str | None
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
    deployment_mode: str
    service_tier: str
    estimated_cost_usd: float | None
    pricing_currency: str
    pricing_coverage_requests_percent: float
    input_price_per_million: float | None
    cached_input_price_per_million: float | None
    output_price_per_million: float | None
    pricing_source: str
    pricing_billing_basis: str | None
    pricing_publisher: str | None
    pricing_confidence: str | None
    unresolved_requests: int
    unresolved_tokens: int
    unresolved_reasons: list[str]
    suggested_override_keys: list[str]


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
        resource_name=summary.resource_name,
        project_name=summary.project_name,
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
        deployment_mode=summary.deployment_mode,
        service_tier=summary.service_tier,
        estimated_cost_usd=summary.estimated_cost_usd,
        pricing_currency=summary.pricing_currency,
        pricing_coverage_requests_percent=summary.pricing_coverage_requests_percent,
        input_price_per_million=summary.input_price_per_million,
        cached_input_price_per_million=summary.cached_input_price_per_million,
        output_price_per_million=summary.output_price_per_million,
        pricing_source=summary.pricing_source,
        pricing_billing_basis=summary.pricing_billing_basis,
        pricing_publisher=summary.pricing_publisher,
        pricing_confidence=summary.pricing_confidence,
        unresolved_requests=summary.unresolved_requests,
        unresolved_tokens=summary.unresolved_tokens,
        unresolved_reasons=list(summary.unresolved_reasons),
        suggested_override_keys=list(summary.suggested_override_keys),
    )


def deployment_rollups(report: AnalysisReport) -> list[DeploymentRollup]:
    total = report.summary.total_tokens
    return sorted(
        [_deployment_rollup(item, total) for item in report.deployments],
        key=lambda item: (-item.total_tokens, item.deployment_name.casefold()),
    )


def model_rollups(report: AnalysisReport) -> list[ModelRollup]:
    total = report.summary.total_tokens
    grouped: OrderedDict[tuple[str, str, str], dict[str, object]] = OrderedDict()
    for deployment in report.deployments:
        item = _deployment_rollup(deployment, total)
        current = grouped.setdefault(
            (item.canonical_model_key, item.deployment_mode.casefold(), item.pricing_currency.casefold()),
            {
                "model_name": item.model_name,
                "deployment_mode": item.deployment_mode,
                "service_tier": item.service_tier,
                "requests": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "total_tokens": 0,
                "retries": 0,
                "addressable_min_tokens": 0,
                "addressable_max_tokens": 0,
                "estimated_cost_usd": 0.0,
                "pricing_currency": item.pricing_currency,
                "priced_requests": 0,
                "input_price_per_million": item.input_price_per_million,
                "cached_input_price_per_million": item.cached_input_price_per_million,
                "output_price_per_million": item.output_price_per_million,
                "pricing_source": item.pricing_source,
                "pricing_billing_basis": item.pricing_billing_basis,
                "pricing_publisher": item.pricing_publisher,
                "pricing_confidence": item.pricing_confidence,
                "unresolved_requests": 0,
                "unresolved_tokens": 0,
                "unresolved_reasons": set(),
                "suggested_override_keys": set(),
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
            "unresolved_requests",
            "unresolved_tokens",
        ):
            current[key] = int(current[key]) + int(getattr(item, key))
        current["unresolved_reasons"] = set(current["unresolved_reasons"]) | set(item.unresolved_reasons)
        current["suggested_override_keys"] = set(current["suggested_override_keys"]) | set(item.suggested_override_keys)
        for field in ("service_tier", "pricing_billing_basis", "pricing_publisher", "pricing_confidence"):
            existing = current[field]
            incoming = getattr(item, field)
            if existing is None:
                current[field] = incoming
            elif incoming is not None and existing != incoming:
                current[field] = "mixed"
        if item.estimated_cost_usd is not None:
            current["estimated_cost_usd"] = float(current["estimated_cost_usd"]) + item.estimated_cost_usd
            current["priced_requests"] = int(current["priced_requests"]) + round(
                item.requests * item.pricing_coverage_requests_percent / 100
            )
    return sorted(
        [
            ModelRollup(
                canonical_model_key=key[0],
                model_name=str(values["model_name"]),
                deployment_mode=str(values["deployment_mode"]),
                service_tier=str(values["service_tier"]),
                token_share_percent=round(int(values["total_tokens"]) / max(1, total) * 100, 1),
                estimated_cost_usd=(
                    float(values["estimated_cost_usd"]) if int(values["priced_requests"]) else None
                ),
                pricing_currency=str(values["pricing_currency"]),
                pricing_coverage_requests_percent=round(
                    int(values["priced_requests"]) / max(1, int(values["requests"])) * 100, 1
                ),
                input_price_per_million=values["input_price_per_million"],
                cached_input_price_per_million=values["cached_input_price_per_million"],
                output_price_per_million=values["output_price_per_million"],
                pricing_source=str(values["pricing_source"]),
                pricing_billing_basis=values["pricing_billing_basis"],  # type: ignore[arg-type]
                pricing_publisher=values["pricing_publisher"],  # type: ignore[arg-type]
                pricing_confidence=values["pricing_confidence"],  # type: ignore[arg-type]
                unresolved_reasons=sorted(values["unresolved_reasons"]),  # type: ignore[arg-type]
                suggested_override_keys=sorted(values["suggested_override_keys"]),  # type: ignore[arg-type]
                **{name: int(values[name]) for name in (
                    "requests",
                    "input_tokens",
                    "output_tokens",
                    "cached_tokens",
                    "total_tokens",
                    "retries",
                    "addressable_min_tokens",
                    "addressable_max_tokens",
                    "unresolved_requests",
                    "unresolved_tokens",
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
