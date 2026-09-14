"""Contentless diagnostics: fingerprint evidence and explicit non-evaluation.

Synthetic fixtures only. No prompt text is used as diagnostic evidence.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.reports import report_html
from tokenlens.telemetry import (
    ContentFeatures,
    Fingerprints,
    ModelRequestRecord,
    TelemetryUsage,
    record_json,
)

FINGERPRINT = "hmac-sha256:" + "a" * 64
OTHER_FINGERPRINT = "hmac-sha256:" + "b" * 64


def canonical(index: int, *, fingerprint: str | None = FINGERPRINT, system_tokens: int | None = 1200):
    record = ModelRequestRecord(
        source="sdk_wrapper",
        event_id=f"event-{index}",
        timestamp=datetime(2026, 9, 14, 12, 0, tzinfo=UTC) + timedelta(minutes=index),
        provider="azure_foundry",
        deployment_name="example-support-prod",
        model_name="gpt-4.1",
        deployment_mode="global",
        usage=TelemetryUsage(input_tokens=4200, cached_tokens=0, output_tokens=380),
        latency_ms=900,
        status_code=200,
        content_features=ContentFeatures(system_prompt_tokens=system_tokens, message_count=6, max_output_tokens=1000),
        fingerprints=Fingerprints(system_prompt=fingerprint) if fingerprint else Fingerprints(),
    )
    return next(iter_records(io.StringIO(json.dumps(record_json(record)) + "\n")))


def test_repeated_prefix_is_detected_from_fingerprints_alone():
    records = [canonical(index) for index in range(10)]
    report = analyze(records, "contentless", generated_at="2026-09-14T13:00:00Z")
    prefix = next(item for item in report.findings if item.rule_id == "TL001")
    assert prefix.evidence["evidence_source"] == "hmac_fingerprint"
    assert prefix.evidence["affected_requests"] == 10
    assert prefix.evidence["prefix_tokens"] == 1200
    assert prefix.evidence["repeated_tokens"] == 1200 * 9
    assert prefix.severity == "high"
    rendered = report_html(report)
    assert FINGERPRINT not in rendered


def test_distinct_fingerprints_do_not_produce_a_repetition_finding():
    records = [
        canonical(index, fingerprint=FINGERPRINT if index % 2 else OTHER_FINGERPRINT)
        for index in range(2)
    ]
    report = analyze(records, "contentless", generated_at="2026-09-14T13:00:00Z")
    assert not [item for item in report.findings if item.rule_id == "TL001" and item.severity == "high"]


def test_missing_telemetry_is_reported_as_not_evaluated_not_as_efficiency():
    records = [canonical(index, fingerprint=None, system_tokens=None) for index in range(5)]
    report = analyze(records, "contentless", generated_at="2026-09-14T13:00:00Z")
    unevaluated = {item.rule_id: item for item in report.findings if item.evidence.get("evaluation_status") == "not_evaluated"}
    assert set(unevaluated) == {"TL001", "TL002", "TL003", "TL004", "TL007"}
    for finding in unevaluated.values():
        assert finding.severity == "info"
        assert finding.estimated_savings.unit == "none"
        assert "not evidence that the workload is efficient" in finding.detail
    rendered = report_html(report)
    assert "not evaluated" in rendered.casefold()


def test_fingerprint_evidence_suppresses_the_not_evaluated_notice_for_that_rule():
    records = [canonical(index) for index in range(10)]
    report = analyze(records, "contentless", generated_at="2026-09-14T13:00:00Z")
    statuses = {item.rule_id: item.evidence.get("evaluation_status") for item in report.findings}
    assert statuses.get("TL001") != "not_evaluated"
    assert statuses.get("TL002") == "not_evaluated"


def test_content_bearing_traces_keep_their_existing_diagnostics():
    raw = {
        "timestamp": "2026-09-14T12:00:00Z",
        "deployment_name": "example-support-prod",
        "model_name": "gpt-4.1",
        "deployment_mode": "global",
        "messages": [{"role": "system", "content": "policy " * 400}, {"role": "user", "content": "hello"}],
        "usage": {"input_tokens": 4200, "output_tokens": 380},
    }
    records = [next(iter_records(io.StringIO(json.dumps(raw) + "\n"))) for _ in range(4)]
    report = analyze(records, "content", generated_at="2026-09-14T13:00:00Z")
    assert not [item for item in report.findings if item.evidence.get("evaluation_status") == "not_evaluated"]
    prefix = next(item for item in report.findings if item.rule_id == "TL001")
    assert prefix.evidence.get("evidence_source") is None
