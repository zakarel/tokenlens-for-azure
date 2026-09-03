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
    messages: list[dict[str, Any]] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    max_output_tokens: int | None = Field(default=None, ge=0)
    usage: Usage = Field(default_factory=Usage)
    latency_ms: float | None = Field(default=None, ge=0)
    status_code: int | None = None
    retry_of: str | None = None
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
    confidence: Literal["high", "medium", "low", "not_evaluated"]
    evaluated: bool = True
    azure_recommendation: AzureRecommendation


class AnalysisSummary(BaseModel):
    requests_analyzed: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    retries: int
    findings: int
    high_findings: int
    medium_findings: int
    low_findings: int
    info_findings: int
    not_evaluated: int
    addressable_min_tokens: int
    addressable_max_tokens: int
    addressable_min_percent: float
    addressable_max_percent: float


class AnalysisReport(BaseModel):
    tool: str = "TokenLens for Azure"
    version: str
    generated_at: str
    source: str
    summary: AnalysisSummary
    findings: list[Finding]
    rules: list[dict[str, Any]]

