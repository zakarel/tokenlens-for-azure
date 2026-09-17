from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)


class TraceRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    timestamp: str | None = None
    request_id: str | None = None
    model: str = "unknown"
    deployment_name: str = "unknown"
    model_name: str = "unknown"
    provider: str = "unknown"
    service_tier: str = "standard"
    deployment_mode: str = "unknown"
    resource_name: str | None = None
    project_name: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    max_output_tokens: int | None = Field(default=None, ge=0)
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float | None = Field(default=None, ge=0)
    status_code: int | None = None
    retry_of: str | None = None
    observed_cost_usd: float | None = Field(default=None, ge=0)
    retrieved_chunks: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    response: dict[str, Any] | None = None


class Estimate(BaseModel):
    min_tokens: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    unit: Literal["tokens", "calls", "none"] = "tokens"
    note: str | None = None


class AzureRecommendation(BaseModel):
    service: str
    capability: str
    action: str


class Finding(BaseModel):
    rule_id: str
    severity: Literal["high", "medium", "low", "info"]
    title: str
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    estimated_savings: Estimate = Field(default_factory=Estimate)
    confidence: Literal["high", "medium", "low"]
    impact_min_percent: float | None = Field(default=None, ge=0, le=100)
    impact_max_percent: float | None = Field(default=None, ge=0, le=100)
    azure_recommendation: AzureRecommendation


class AggregateAnalysisSummary(BaseModel):
    """Aggregate telemetry facts, with every count kept distinct.

    ``None`` always means the source did not report the metric. Zero means the
    source reported it and it was genuinely zero.
    """

    model_config = ConfigDict(extra="forbid")

    metric_buckets_read: int = Field(default=0, ge=0)
    elapsed_buckets: int = Field(default=0, ge=0)
    observed_buckets: int = Field(default=0, ge=0)
    active_buckets: int = Field(default=0, ge=0)
    idle_buckets: int = Field(default=0, ge=0)
    requests_observed: int | None = Field(default=None, ge=0)
    successful_requests: int | None = Field(default=None, ge=0)
    rate_limited_requests: int | None = Field(default=None, ge=0)
    failed_requests: int | None = Field(default=None, ge=0)
    outcome_coverage: Literal["complete", "partial", "unavailable"] = "unavailable"
    status_codes: dict[str, int] = Field(default_factory=dict)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    latency_coverage_percent: float = Field(default=0, ge=0, le=100)
    bucket_minutes: int = Field(default=5, gt=0)
    window_start: datetime | None = None
    window_end: datetime | None = None
    observed_days: float = Field(default=0, ge=0)
    active_days: int = Field(default=0, ge=0)
    missing_metrics: list[str] = Field(default_factory=list)

    @property
    def collection_completeness_percent(self) -> float:
        """Observed versus expected buckets — never the activity rate."""
        if not self.elapsed_buckets:
            return 0.0
        return round(min(100.0, self.observed_buckets / self.elapsed_buckets * 100), 1)

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class AnalysisSummary(BaseModel):
    #: ``requests`` for request telemetry, ``metric_buckets`` for aggregate
    #: telemetry. Every rendered label is chosen from this, never guessed.
    analysis_unit: Literal["requests", "metric_buckets"] = "requests"
    requests_analyzed: int
    #: Actual requests. ``None`` when the source reports no request metric — it
    #: is never replaced with a bucket count.
    requests_observed: int | None = None
    requests_available: bool = True
    retries_available: bool = True
    cached_tokens_available: bool = True
    input_tokens_available: bool = True
    output_tokens_available: bool = True
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    total_tokens: int = 0
    retries: int
    findings: int
    high_findings: int
    medium_findings: int
    low_findings: int
    info_findings: int
    not_evaluated_rules: int = 0
    evaluated_rules: int = 0
    addressable_min_tokens: int
    addressable_max_tokens: int
    addressable_min_percent: float = 0
    addressable_max_percent: float = 0
    addressable_aggregation: Literal["largest_individual_opportunity", "not_combined_due_to_overlap"] = "largest_individual_opportunity"
    average_tokens_per_request: float | None = 0
    average_latency_ms: float | None = None
    aggregate: AggregateAnalysisSummary | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    fresh_input_cost_usd: float | None = Field(default=None, ge=0)
    cached_input_cost_usd: float | None = Field(default=None, ge=0)
    output_cost_usd: float | None = Field(default=None, ge=0)
    pricing_coverage_requests_percent: float = Field(default=0, ge=0, le=100)
    pricing_coverage_tokens_percent: float = Field(default=0, ge=0, le=100)
    pricing_coverage_basis: Literal["requests", "metric_buckets"] = "requests"
    pricing_status: Literal[
        "priced",
        "partial",
        "catalog_missing",
        "identity_unresolved",
        "currency_mismatch",
    ] = "priced"
    pricing_complete: bool = False
    pricing_currency: str = "USD"
    pricing_catalog_name: str | None = None
    service_tier: str = "standard"
    pricing_billing_basis: str | None = None
    pricing_publisher: str | None = None
    pricing_confidence: str | None = None
    pricing_source: str = "unresolved"
    unresolved_requests: int = Field(default=0, ge=0)
    unresolved_tokens: int = Field(default=0, ge=0)
    unresolved_reasons: list[str] = Field(default_factory=list)
    suggested_override_keys: list[str] = Field(default_factory=list)


class DeploymentSummary(AnalysisSummary):
    deployment_name: str
    model_name: str
    #: Exact model version when the source reported it. Never concatenated into
    #: the model name, so catalog lookups stay exact.
    model_version: str | None = None
    canonical_model_key: str = "unknown"
    provider: str = "unknown"
    deployment_mode: str = "unknown"
    resource_name: str | None = None
    project_name: str | None = None
    request_share_percent: float = 0
    token_share_percent: float = 0
    pricing_catalog_name: str | None = None
    pricing_effective_from: str | None = None
    input_price_per_million: float | None = Field(default=None, ge=0)
    cached_input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)


class RuleEvaluation(BaseModel):
    """One diagnostic rule's outcome for this analysis.

    ``not_evaluated`` is a coverage statement, not a finding: it never increases
    a finding count and never appears as an opportunity.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    title: str
    status: Literal["finding", "no_issue", "not_evaluated"]
    applicable_sources: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    detail: str = ""


class DiagnosticCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["request", "aggregate", "mixed", "none"] = "none"
    evaluations: list[RuleEvaluation] = Field(default_factory=list)

    @property
    def findings(self) -> int:
        return sum(item.status == "finding" for item in self.evaluations)

    @property
    def no_issue(self) -> int:
        return sum(item.status == "no_issue" for item in self.evaluations)

    @property
    def not_evaluated(self) -> int:
        return sum(item.status == "not_evaluated" for item in self.evaluations)


class DeploymentAnalysis(BaseModel):
    summary: DeploymentSummary
    findings: list[Finding]
    diagnostics: DiagnosticCoverage = Field(default_factory=DiagnosticCoverage)


class DataQualityIssue(BaseModel):
    """One blocking or advisory limitation of the analyzed telemetry."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["blocker", "warning", "info"]
    title: str
    detail: str


class AnalysisReport(BaseModel):
    tool: str = "TokenLens for Azure"
    version: str
    generated_at: str
    source: str
    data_classification: Literal["synthetic", "local_real", "unknown"] = "unknown"
    summary: AnalysisSummary
    findings: list[Finding]
    deployments: list[DeploymentAnalysis] = Field(default_factory=list)
    diagnostics: DiagnosticCoverage = Field(default_factory=DiagnosticCoverage)
    data_quality: list[DataQualityIssue] = Field(default_factory=list)
    rules: list[dict[str, Any]]
    report_metadata: dict[str, Any] = Field(default_factory=dict)
    task_economics: Any | None = None
    ptu_analysis: Any | None = None
