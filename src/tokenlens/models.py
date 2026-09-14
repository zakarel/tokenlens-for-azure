from __future__ import annotations

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


class AnalysisSummary(BaseModel):
    requests_analyzed: int
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
    addressable_min_tokens: int
    addressable_max_tokens: int
    addressable_min_percent: float = 0
    addressable_max_percent: float = 0
    addressable_aggregation: Literal["largest_individual_opportunity", "not_combined_due_to_overlap"] = "largest_individual_opportunity"
    average_tokens_per_request: float = 0
    average_latency_ms: float | None = None
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    fresh_input_cost_usd: float | None = Field(default=None, ge=0)
    cached_input_cost_usd: float | None = Field(default=None, ge=0)
    output_cost_usd: float | None = Field(default=None, ge=0)
    pricing_coverage_requests_percent: float = Field(default=0, ge=0, le=100)
    pricing_coverage_tokens_percent: float = Field(default=0, ge=0, le=100)
    pricing_complete: bool = False
    pricing_currency: str = "USD"
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


class DeploymentAnalysis(BaseModel):
    summary: DeploymentSummary
    findings: list[Finding]


class AnalysisReport(BaseModel):
    tool: str = "TokenLens for Azure"
    version: str
    generated_at: str
    source: str
    summary: AnalysisSummary
    findings: list[Finding]
    deployments: list[DeploymentAnalysis] = Field(default_factory=list)
    rules: list[dict[str, Any]]
    report_metadata: dict[str, Any] = Field(default_factory=dict)
    task_economics: Any | None = None
    ptu_analysis: Any | None = None
