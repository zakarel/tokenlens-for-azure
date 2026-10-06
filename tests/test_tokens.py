from __future__ import annotations

import io
import json

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.tokens import _encoding, count_text, count_tools, tokenizer_provenance


def test_token_counting_is_deterministic_with_an_empty_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty-tokenizer-cache"))
    _encoding.cache_clear()
    try:
        assert count_text("Aé", "unknown-model-for-offline-test") == 3
        assert count_tools([{"name": "lookup"}], "unknown-model-for-offline-test") > 0
        assert tokenizer_provenance("unknown-model-for-offline-test") == {
            "model": "unknown-model-for-offline-test",
            "encoding": "cl100k_base",
            "method": "utf8_byte_heuristic",
            "confidence": "low",
        }
    finally:
        _encoding.cache_clear()


def test_report_discloses_when_request_tokenizer_data_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty-tokenizer-cache"))
    _encoding.cache_clear()
    raw = {
        "timestamp": "2026-10-01T12:00:00Z",
        "deployment_name": "synthetic-chat",
        "model_name": "gpt-4o",
        "messages": [{"role": "user", "content": "synthetic question"}],
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }
    try:
        record = next(iter_records(io.StringIO(json.dumps(raw) + "\n")))
        report = analyze([record], "synthetic fixture")
        assert report.report_metadata["tokenizer"] == [
            {
                "model": "gpt-4o",
                "encoding": "o200k_base",
                "method": "utf8_byte_heuristic",
                "confidence": "low",
            }
        ]
        assert any(issue.code == "tokenizer_approximate" for issue in report.data_quality)
    finally:
        _encoding.cache_clear()


def test_aggregate_report_does_not_claim_request_tokenizer_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "empty-tokenizer-cache"))
    _encoding.cache_clear()
    raw = {
        "schema_version": 3,
        "record_type": "foundry_metric_bucket",
        "source": "azure_monitor",
        "event_id": "synthetic-bucket-1",
        "timestamp": "2026-10-01T12:00:00Z",
        "deployment_name": "synthetic-chat",
        "model_name": "gpt-4o",
        "metrics": {"input_tokens": 0, "output_tokens": 0, "requests": 0},
        "outcome_coverage": "complete",
    }
    try:
        record = next(iter_records(io.StringIO(json.dumps(raw) + "\n")))
        report = analyze([record], "synthetic aggregate")
        assert report.report_metadata["tokenizer"] == []
        assert not any(issue.code == "tokenizer_approximate" for issue in report.data_quality)
    finally:
        _encoding.cache_clear()
