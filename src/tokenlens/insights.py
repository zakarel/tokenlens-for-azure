"""Deterministic aggregate operational insights."""

from __future__ import annotations

from .models import AggregateAnalysisSummary, Insight


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
            observation=f"Only {coverage:.1f}% of the requested collection buckets were returned.",
            evidence=[{"observed_buckets": summary.observed_buckets, "expected_buckets": summary.elapsed_buckets}],
            impact={"coverage_percent": coverage},
            recommendation="Use a narrower retained window or repair the collection source before making capacity or trend decisions.",
            verification="Repeat collection and confirm coverage approaches the requested window.",
            confidence="high",
            applicability="ACTIONABLE_FINDING",
            caveats=["Returned telemetry may be retention-limited."],
            priority=0,
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
                observation=f"{non_rate_failures:,} requests failed for reasons other than rate limiting ({rate:.1f}%).",
                evidence=[{"non_rate_failures": non_rate_failures, "requests": summary.requests_observed}],
                impact={"failure_percent": round(rate, 1)},
                recommendation="Separate client, server, and unknown error categories before changing retries or capacity.",
                verification="Compare categorized failure rates after correcting error handling and retry policy.",
                confidence="high" if summary.outcome_coverage == "complete" else "medium",
                applicability="ACTIONABLE_FINDING",
                caveats=["Do not estimate retry waste without explicit retry identity."],
                priority=1,
            ))
    if summary.requests_observed and summary.total_tokens is not None:
        insights.append(Insight(
            insight_id="AGG-TOKENS-PER-REQUEST",
            rule_version="1.0",
            category="token_efficiency",
            scope_type="portfolio",
            scope_id=scope_id,
            title="Request token footprint is measurable",
            observation=f"Average measured footprint is {summary.total_tokens / summary.requests_observed:,.0f} tokens per request.",
            evidence=[{"total_tokens": summary.total_tokens, "requests": summary.requests_observed}],
            impact=None,
            recommendation="Inspect request-level telemetry for prompt growth, retrieval duplication, and output budgets before claiming waste.",
            verification="Instrument request-level token structure and compare the per-request distribution.",
            confidence="medium",
            applicability="EVALUATED_NO_ISSUE",
            missing_evidence=["request-level message and retrieval structure"],
            caveats=["A large footprint is an investigation trigger, not proof of waste."],
            priority=2,
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
            recommendation="Enable cached-input usage collection before attributing prompt cost or cache savings.",
            verification="Repeat the analysis and confirm cached-input coverage is measured.",
            confidence="high",
            applicability="NOT_EVALUATED_MISSING_EVIDENCE",
            missing_evidence=["cached-input usage"],
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
    return sorted(insights, key=lambda item: (item.priority, item.insight_id))
