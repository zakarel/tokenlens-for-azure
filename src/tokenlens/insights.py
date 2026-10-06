"""Deterministic aggregate operational insights."""

from __future__ import annotations

from .models import AggregateAnalysisSummary, HelpLink, Insight


def _materiality(*, impact_percent: float | None, priority: int) -> str:
    if impact_percent is not None and impact_percent >= 10:
        return "material"
    return "advisory" if priority <= 1 else "observation"


def build_aggregate_insights(summary: AggregateAnalysisSummary | None, *, scope_id: str = "portfolio") -> list[Insight]:
    if summary is None:
        return []
    insights: list[Insight] = []
    if summary.elapsed_buckets and summary.observed_buckets < summary.elapsed_buckets:
        coverage = summary.collection_completeness_percent
        insights.append(Insight(
            insight_id="AGG-COVERAGE",
            rule_version="1.0",
            category="data_quality",
            scope_type="portfolio",
            scope_id=scope_id,
            title="Collection coverage is incomplete",
            observation=f"Azure Monitor returned {coverage:.1f}% of the requested window; older data is outside the available retention window.",
            evidence=[{"observed_buckets": summary.observed_buckets, "expected_buckets": summary.elapsed_buckets}],
            impact={"coverage_percent": coverage},
            recommendation="Use a window inside retention, or export metrics to Log Analytics/storage for longer history.",
            verification="Rerun collection and confirm returned coverage matches the analysis window.",
            confidence="high",
            applicability="ACTIONABLE_FINDING",
            caveats=["Returned telemetry may be retention-limited."],
            priority=0,
            materiality="material",
            help_links=[HelpLink(title="Azure Monitor metrics retention", url="https://learn.microsoft.com/azure/azure-monitor/essentials/data-platform-metrics", source="microsoft_learn")],
        ))
    if summary.requests_observed and summary.rate_limited_requests:
        rate = summary.rate_limited_requests / summary.requests_observed * 100
        if rate >= 5:
            insights.append(Insight(
                insight_id="AGG-RATE-LIMIT",
                rule_version="1.0",
                category="reliability",
                scope_type="portfolio",
                scope_id=scope_id,
                title="Rate-limit pressure is material",
                observation=f"{summary.rate_limited_requests:,} of {summary.requests_observed:,} requests were rate limited ({rate:.1f}%).",
                evidence=[{"rate_limited_requests": summary.rate_limited_requests, "requests": summary.requests_observed}],
                impact={"rate_percent": round(rate, 1)},
                recommendation="Review quota and concurrency, smooth bursts, and verify retry backoff before considering capacity changes.",
                verification="Compare 429 rate and burst distribution after concurrency/backoff changes.",
                confidence="high" if summary.outcome_coverage == "complete" else "medium",
                applicability="ACTIONABLE_FINDING",
                caveats=["PTU is not implied by rate-limit pressure alone."],
                priority=1,
                materiality="material" if rate >= 10 else "advisory",
                help_links=[HelpLink(title="Azure OpenAI monitoring reference", url="https://learn.microsoft.com/azure/ai-foundry/openai/monitor-openai-reference", source="microsoft_learn")],
            ))
    if summary.requests_observed and summary.failed_requests:
        non_rate_failures = max(0, summary.failed_requests - (summary.rate_limited_requests or 0))
        if non_rate_failures:
            rate = non_rate_failures / summary.requests_observed * 100
            insights.append(Insight(
                insight_id="AGG-FAILURE-RATE",
                rule_version="1.0",
                category="reliability",
                scope_type="portfolio",
                scope_id=scope_id,
                title="Non-rate-limit failure pressure is present",
                observation=f"{non_rate_failures:,} of {summary.requests_observed:,} requests failed with non-429 responses ({rate:.1f}%): inspect HTTP 400/404 causes.",
                evidence=[{"non_rate_failures": non_rate_failures, "requests": summary.requests_observed}],
                impact={"failure_percent": round(rate, 1)},
                recommendation="Fix invalid requests and deployment/endpoint references before adding retries.",
                verification="Rerun the workload and confirm 400/404 rates fall.",
                confidence="high" if summary.outcome_coverage == "complete" else "medium",
                applicability="ACTIONABLE_FINDING",
                caveats=["Do not estimate retry waste without explicit retry identity."],
                priority=1,
                materiality="material" if rate >= 10 else "advisory",
                help_links=[HelpLink(title="Azure OpenAI monitoring reference", url="https://learn.microsoft.com/azure/ai-foundry/openai/monitor-openai-reference", source="microsoft_learn")],
            ))
    if summary.requests_observed and summary.total_tokens is not None:
        insights.append(Insight(
            insight_id="AGG-TOKENS-PER-REQUEST",
            rule_version="1.0",
            category="token_efficiency",
            scope_type="portfolio",
            scope_id=scope_id,
            title="Request token footprint is measurable",
            observation=f"Requests average {summary.total_tokens / summary.requests_observed:,.0f} measured tokens; aggregate telemetry cannot explain why.",
            evidence=[{"total_tokens": summary.total_tokens, "requests": summary.requests_observed}],
            impact=None,
            recommendation="Enable request telemetry, then inspect prompt growth, repeated context, retrieval duplication, output limits, and cache usage.",
            verification="Compare per-request token distributions before and after the change.",
            confidence="medium",
            applicability="EVALUATED_NO_ISSUE",
            missing_evidence=["request-level message and retrieval structure"],
            caveats=["A large footprint is an investigation trigger, not proof of waste."],
            priority=2,
            materiality="observation",
            help_links=[HelpLink(title="Prompt caching guidance", url="https://learn.microsoft.com/azure/foundry/openai/how-to/prompt-caching", source="microsoft_learn")],
        ))
    if summary.requests_observed and summary.total_tokens is None:
        insights.append(Insight(
            insight_id="AGG-NATIVE-USAGE-GAP",
            rule_version="1.0",
            category="data_quality",
            scope_type="portfolio",
            scope_id=scope_id,
            title="Activity exists without compatible usage telemetry",
            observation=f"{summary.requests_observed:,} requests were observed, but token-compatible usage is unavailable.",
            evidence=[{"requests": summary.requests_observed, "total_tokens": None}],
            recommendation="Collect the model's native billable unit before calculating cost, token efficiency, or PTU.",
            verification="Repeat collection with the provider-specific usage metric enabled.",
            confidence="high",
            applicability="NOT_EVALUATED_MISSING_EVIDENCE",
            missing_evidence=["compatible native usage metric"],
            caveats=["Requests are activity evidence, not token evidence."],
            priority=1,
        ))
    if summary.cached_tokens_state == "unavailable" and summary.total_tokens is not None:
        insights.append(Insight(
            insight_id="AGG-CACHED-TOKEN-GAP",
            rule_version="1.0",
            category="pricing_readiness",
            scope_type="portfolio",
            scope_id=scope_id,
            title="Cached-input telemetry is unavailable",
            observation="Token usage is present, but cached-input usage is not measured.",
            evidence=[{"cached_tokens_state": summary.cached_tokens_state}],
            recommendation="For Standard GPT-5.6 PAYG, collect response usage through SDK, OpenTelemetry, Application Insights, or JSONL; Azure Monitor does not expose cached-token counts.",
            verification="Repeat analysis with per-request prompt_tokens_details.cached_tokens and cache_write_tokens present.",
            confidence="high",
            applicability="NOT_EVALUATED_MISSING_EVIDENCE",
            missing_evidence=["per-request cached_tokens and cache_write_tokens"],
            priority=2,
        ))
    if summary.outcome_coverage == "unavailable" and summary.requests_observed:
        insights.append(Insight(
            insight_id="AGG-OUTCOME-GAP",
            rule_version="1.0",
            category="data_quality",
            scope_type="portfolio",
            scope_id=scope_id,
            title="Request outcome coverage is unavailable",
            observation="Activity and usage are present, but success/failure outcome metrics were not returned.",
            evidence=[{"requests": summary.requests_observed, "outcome_coverage": summary.outcome_coverage}],
            recommendation="Collect status or outcome dimensions before evaluating reliability or retry behavior.",
            verification="Confirm outcome coverage is complete or explicitly partial on the next run.",
            confidence="high",
            applicability="NOT_EVALUATED_MISSING_EVIDENCE",
            missing_evidence=["request outcomes"],
            priority=1,
        ))
    if summary.requests_observed and summary.latency_coverage_percent == 0:
        insights.append(Insight(
            insight_id="DQ-LATENCY-GAP",
            rule_version="1.0",
            category="data_quality",
            scope_type="portfolio",
            scope_id=scope_id,
            title="Latency telemetry is unavailable",
            observation="Requests and usage are present, but no compatible Azure OpenAI latency metric was returned.",
            evidence=[{"requests": summary.requests_observed, "latency_coverage_percent": 0}],
            recommendation="No user action is required. TokenLens queried the documented Azure OpenAI latency metrics automatically; none was returned for this resource/window, so latency findings are withheld.",
            verification="Rerun collection and confirm latency point coverage is non-zero.",
            confidence="high",
            applicability="NOT_EVALUATED_MISSING_EVIDENCE",
            missing_evidence=["documented Azure OpenAI latency metric"],
            priority=1,
            help_links=[HelpLink(title="Azure OpenAI monitoring reference", url="https://learn.microsoft.com/azure/ai-foundry/openai/monitor-openai-reference", source="microsoft_learn")],
        ))
    return sorted(insights, key=lambda item: (item.priority, item.insight_id))
