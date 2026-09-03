from __future__ import annotations

from datetime import UTC, datetime

from . import __version__
from .models import AnalysisReport, AnalysisSummary, Finding, TraceRecord
from .rules import RULES, run_rules


def analyze(records: list[TraceRecord], source: str) -> AnalysisReport:
    findings = run_rules(records)
    input_tokens = sum(record.usage.input_tokens for record in records)
    output_tokens = sum(record.usage.output_tokens for record in records)
    cached_tokens = sum(record.usage.cached_tokens for record in records)
    estimates = [
        finding.estimated_savings
        for finding in findings
        if finding.evaluated and finding.estimated_savings.unit == "tokens"
    ]
    addressable_min = sum(estimate.min_tokens or 0 for estimate in estimates)
    addressable_max = sum(estimate.max_tokens or 0 for estimate in estimates)
    # Rule opportunities can overlap; never report more addressable input than
    # the trace set actually contained.
    addressable_min = min(addressable_min, input_tokens)
    addressable_max = min(max(addressable_max, addressable_min), input_tokens)
    high = sum(finding.severity == "high" for finding in findings)
    medium = sum(finding.severity == "medium" for finding in findings)
    low = sum(finding.severity == "low" for finding in findings)
    info = sum(finding.severity == "info" for finding in findings)
    not_evaluated = sum(not finding.evaluated for finding in findings)
    denominator = max(1, input_tokens)
    summary = AnalysisSummary(
        requests_analyzed=len(records),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        retries=sum(bool(record.retry_of) for record in records),
        findings=len(findings),
        high_findings=high,
        medium_findings=medium,
        low_findings=low,
        info_findings=info,
        not_evaluated=not_evaluated,
        addressable_min_tokens=addressable_min,
        addressable_max_tokens=addressable_max,
        addressable_min_percent=round(addressable_min / denominator * 100, 1),
        addressable_max_percent=round(addressable_max / denominator * 100, 1),
    )
    return AnalysisReport(
        version=__version__,
        generated_at=datetime.now(UTC).isoformat(),
        source=source,
        summary=summary,
        findings=sorted(
            findings,
            key=lambda finding: (
                {"high": 0, "medium": 1, "low": 2, "info": 3}[finding.severity],
                finding.rule_id,
            ),
        ),
        rules=RULES,
    )
