"""Canonical telemetry schema, privacy, and writer behaviour.

All fixtures are synthetic. No provider SDK, network call, or real deployment
name appears in this module.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from tokenlens.ingest import normalize_record
from tokenlens.telemetry import (
    BucketMetrics,
    ContentFeatures,
    Fingerprints,
    MetricBucketRecord,
    ModelRequestRecord,
    TelemetryConfig,
    TelemetryUsage,
    TelemetryWriter,
    parse_record,
    record_json,
)
from tokenlens.telemetry.privacy import ContentLeakError, FingerprintPolicy, assert_contentless, split_attributes
from tokenlens.telemetry.writer import TelemetryWriteError

CANARY_PROMPT = "CANARY-PROMPT-do-not-store-this-sentence"
CANARY_SECRET = "CANARY-SECRET-abc123"


def request_record(**overrides):
    payload = {
        "source": "sdk_wrapper",
        "event_id": "synthetic-event-1",
        "timestamp": datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        "provider": "azure_foundry",
        "deployment_name": "synthetic-deployment",
        "model_name": "gpt-4.1",
        "deployment_mode": "global",
        "usage": TelemetryUsage(input_tokens=4200, cached_tokens=1800, output_tokens=380),
        "latency_ms": 1450,
        "status_code": 200,
    }
    payload.update(overrides)
    return ModelRequestRecord(**payload)


def test_canonical_request_rejects_unknown_and_content_fields():
    with pytest.raises(ValidationError):
        ModelRequestRecord(
            source="sdk_wrapper",
            event_id="e",
            timestamp=datetime(2026, 9, 14, tzinfo=UTC),
            provider="azure_foundry",
            deployment_name="d",
            model_name="m",
            messages=[{"role": "user", "content": CANARY_PROMPT}],
        )


def test_canonical_request_serializes_without_any_content_key():
    payload = record_json(request_record(content_features=ContentFeatures(message_count=6, system_prompt_tokens=1200)))
    serialized = json.dumps(payload)
    assert CANARY_PROMPT not in serialized
    assert assert_contentless(payload) is payload
    assert payload["schema_version"] == 3
    assert payload["record_type"] == "model_request"
    assert payload["timestamp"].endswith("Z") or "+00:00" in payload["timestamp"]


def test_counters_must_be_nonnegative_and_timestamps_are_utc():
    with pytest.raises(ValidationError):
        TelemetryUsage(input_tokens=-1)
    naive = request_record(timestamp=datetime(2026, 9, 14, 12, 0))
    assert naive.timestamp.tzinfo is not None
    assert naive.timestamp.utcoffset() == timedelta(0)


def test_unknown_deployment_mode_never_defaults_to_global():
    assert request_record(deployment_mode="unknown").deployment_mode == "unknown"
    record = ModelRequestRecord(
        source="otel",
        event_id="e",
        timestamp=datetime(2026, 9, 14, tzinfo=UTC),
        provider="azure_foundry",
        deployment_name="d",
        model_name="gpt-4.1-2026-01-01",
    )
    assert record.deployment_mode == "unknown"
    # Dated model IDs are preserved exactly.
    assert record.model_name == "gpt-4.1-2026-01-01"


def test_bucket_metrics_distinguish_zero_from_unavailable():
    bucket = MetricBucketRecord(
        event_id="bucket-1",
        timestamp=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        deployment_name="synthetic-deployment",
        model_name="gpt-4.1",
        metrics=BucketMetrics(input_tokens=250_000, output_tokens=35_000, requests=480, throttled_requests=0),
        missing_metrics=["cached_tokens"],
    )
    assert bucket.metrics.throttled_requests == 0
    assert bucket.metrics.cached_tokens is None
    assert "cached_tokens" in bucket.missing_metrics
    assert parse_record(record_json(bucket)).record_type == "foundry_metric_bucket"


def test_fingerprints_require_keyed_hmac():
    with pytest.raises(ValidationError):
        Fingerprints(system_prompt="sha256:deadbeef")


def test_fingerprint_omitted_without_key_and_stable_with_key(monkeypatch):
    monkeypatch.delenv("TOKENLENS_FINGERPRINT_KEY", raising=False)
    inactive = FingerprintPolicy.from_env()
    assert inactive.active is False
    assert inactive.fingerprint(CANARY_PROMPT) is None

    monkeypatch.setenv("TOKENLENS_FINGERPRINT_KEY", "local-secret")
    active = FingerprintPolicy.from_env()
    first = active.fingerprint(CANARY_PROMPT)
    assert first is not None and first.startswith("hmac-sha256:")
    assert first == active.fingerprint(CANARY_PROMPT)
    assert CANARY_PROMPT not in first
    # A different key produces a different fingerprint, so fingerprints cannot
    # be precomputed from a public dictionary.
    monkeypatch.setenv("TOKENLENS_FINGERPRINT_KEY", "other-secret")
    assert FingerprintPolicy.from_env().fingerprint(CANARY_PROMPT) != first


def test_split_attributes_drops_content_bearing_keys():
    kept, rejected = split_attributes(
        {
            "gen_ai.request.model": "gpt-4.1",
            "gen_ai.prompt": CANARY_PROMPT,
            "gen_ai.completion": CANARY_PROMPT,
            "http.request.header.authorization": CANARY_SECRET,
        },
        allow={"gen_ai.request.model"},
    )
    assert kept == {"gen_ai.request.model": "gpt-4.1"}
    assert rejected == 3
    assert CANARY_PROMPT not in json.dumps(kept)


def test_assert_contentless_fails_closed():
    with pytest.raises(ContentLeakError):
        assert_contentless({"record_type": "model_request", "messages": [{"role": "user"}]})


def test_writer_rotates_daily_and_restricts_permissions(tmp_path):
    config = TelemetryConfig(output_dir=tmp_path / "traces", retention_days=0)
    writer = TelemetryWriter(config)
    assert writer.write(request_record()) is True
    assert writer.write(request_record(timestamp=datetime(2026, 9, 15, 9, 0, tzinfo=UTC))) is True
    files = sorted(path.name for path in (tmp_path / "traces").glob("*.jsonl"))
    assert files == ["tokenlens-2026-09-14.jsonl", "tokenlens-2026-09-15.jsonl"]
    mode = stat.S_IMODE((tmp_path / "traces" / files[0]).stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    assert writer.written_events == 2
    assert writer.dropped_events == 0


def test_writer_rotates_on_size(tmp_path):
    config = TelemetryConfig(output_dir=tmp_path / "traces", max_mb=0.001, retention_days=0)
    writer = TelemetryWriter(config)
    for _ in range(40):
        writer.write(request_record())
    names = sorted(path.name for path in (tmp_path / "traces").glob("*.jsonl"))
    assert "tokenlens-2026-09-14.jsonl" in names
    assert any(name.startswith("tokenlens-2026-09-14.0") for name in names)


def test_writer_retention_prunes_by_local_file_age_only(tmp_path):
    directory = tmp_path / "traces"
    directory.mkdir()
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    stale = directory / "tokenlens-2026-01-01.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    aged = (now - timedelta(days=30)).timestamp()
    os.utime(stale, (aged, aged))
    recent = directory / "tokenlens-2026-09-13.jsonl"
    recent.write_text("{}\n", encoding="utf-8")
    unrelated = directory / "customer-notes.jsonl"
    unrelated.write_text("{}\n", encoding="utf-8")
    os.utime(unrelated, (aged, aged))

    writer = TelemetryWriter(TelemetryConfig(output_dir=directory, retention_days=7), clock=lambda: now)
    writer.write(request_record())
    assert not stale.exists()
    assert recent.exists()
    # Retention only ever touches TokenLens's own dated files.
    assert unrelated.exists()


def test_backfilled_records_are_not_deleted_by_the_write_that_persisted_them(tmp_path):
    """An imported or collected window older than the retention period must survive.

    Retention describes how long telemetry is kept locally, not how old the
    observations inside it are. Pruning by the in-record date would delete a
    14-day Azure Monitor window the moment it was written while still reporting
    it as persisted.
    """
    directory = tmp_path / "traces"
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    writer = TelemetryWriter(TelemetryConfig(output_dir=directory, retention_days=3), clock=lambda: now)
    backfilled = request_record(timestamp=datetime(2026, 8, 20, 9, 0, tzinfo=UTC))
    assert writer.write(backfilled) is True
    path = directory / "tokenlens-2026-08-20.jsonl"
    assert path.exists()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    assert writer.written_events == 1
    assert writer.dropped_events == 0

    # A second backfilled day in the same run is equally safe.
    assert writer.write(request_record(timestamp=datetime(2026, 8, 21, 9, 0, tzinfo=UTC), event_id="synthetic-event-2")) is True
    assert (directory / "tokenlens-2026-08-21.jsonl").exists()
    assert path.exists()


def test_write_is_idempotent_against_records_already_on_disk(tmp_path):
    directory = tmp_path / "traces"
    config = TelemetryConfig(output_dir=directory, retention_days=0)
    first = TelemetryWriter(config)
    records = [request_record(event_id=f"synthetic-event-{index}") for index in range(5)]
    assert first.write_all(records) == 5

    # A second process re-running the same window writes nothing new.
    second = TelemetryWriter(config)
    assert second.write_all(records) == 0
    assert second.skipped_duplicates == 5
    assert second.written_events == 0
    lines = (directory / "tokenlens-2026-09-14.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5

    # New records in an overlapping window are still appended.
    extended = records + [request_record(event_id="synthetic-event-9")]
    third = TelemetryWriter(config)
    assert third.write_all(extended) == 1
    assert third.skipped_duplicates == 5
    assert len((directory / "tokenlens-2026-09-14.jsonl").read_text(encoding="utf-8").splitlines()) == 6


def test_known_event_ids_ignores_unrelated_and_malformed_files(tmp_path):
    directory = tmp_path / "traces"
    directory.mkdir()
    (directory / "tokenlens-2026-09-14.jsonl").write_text(
        '{"event_id":"kept"}\nnot json\n\n{"no_event_id":true}\n', encoding="utf-8"
    )
    (directory / "customer-notes.jsonl").write_text('{"event_id":"ignored"}\n', encoding="utf-8")
    writer = TelemetryWriter(TelemetryConfig(output_dir=directory, retention_days=0))
    assert writer.known_event_ids() == {"kept"}


def test_ingested_canonical_records_keep_their_event_id_and_dedupe(tmp_path):
    directory = tmp_path / "traces"
    writer = TelemetryWriter(TelemetryConfig(output_dir=directory, retention_days=0))
    record = request_record(event_id="synthetic-event-dedupe")
    writer.write(record)
    # The same window copied beside itself, as a rotated file would be.
    original = directory / "tokenlens-2026-09-14.jsonl"
    (directory / "tokenlens-2026-09-14.001.jsonl").write_text(
        original.read_text(encoding="utf-8"), encoding="utf-8"
    )
    from tokenlens.ingest import load_records_many

    records, _ = load_records_many([str(directory)])
    assert len(records) == 1
    assert records[0].metadata["event_id"] == "synthetic-event-dedupe"


def test_legacy_records_without_an_event_id_are_never_deduplicated(tmp_path):
    path = tmp_path / "legacy.jsonl"
    line = json.dumps(
        {
            "timestamp": "2026-09-14T12:00:00Z",
            "deployment_name": "legacy-deployment",
            "model_name": "gpt-4.1",
            "usage": {"input_tokens": 10, "output_tokens": 4},
        }
    )
    path.write_text(f"{line}\n{line}\n{line}\n", encoding="utf-8")
    from tokenlens.ingest import load_records

    records, _ = load_records(str(path))
    assert len(records) == 3


def test_writer_surfaces_errors_through_callback_and_strict_mode(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    seen: list[str] = []
    writer = TelemetryWriter(
        TelemetryConfig(output_dir=blocked / "traces", retention_days=0),
        on_error=lambda exc, reason: seen.append(reason),
    )
    assert writer.write(request_record()) is False
    assert seen == ["write-failed"]
    assert writer.dropped_events == 1
    assert writer.last_error == "write-failed"

    strict = TelemetryWriter(TelemetryConfig(output_dir=blocked / "traces", retention_days=0, strict=True))
    with pytest.raises(TelemetryWriteError):
        strict.write(request_record())


def test_written_records_are_ingestible_and_contentless(tmp_path):
    config = TelemetryConfig(output_dir=tmp_path / "traces", retention_days=0)
    writer = TelemetryWriter(config)
    writer.write(request_record())
    path = writer.current_path(datetime(2026, 9, 14, 12, 0, tzinfo=UTC))
    raw = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    record = normalize_record(raw)
    assert record.deployment_name == "synthetic-deployment"
    assert record.model_name == "gpt-4.1"
    assert record.usage.input_tokens == 4200
    assert record.usage.cached_tokens == 1800
    assert record.usage.output_tokens == 380
    assert record.messages == []
    assert record.metadata["record_type"] == "model_request"


def test_aggregate_bucket_ingests_with_explicit_outcome_metadata():
    bucket = MetricBucketRecord(
        event_id="bucket-1",
        timestamp=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        deployment_name="synthetic-deployment",
        model_name="gpt-4.1",
        deployment_mode="global",
        metrics=BucketMetrics(
            input_tokens=250_000,
            output_tokens=35_000,
            requests=480,
            successful_requests=472,
            throttled_requests=8,
            p50_latency_ms=420,
        ),
        missing_metrics=["cached_tokens"],
    )
    record = normalize_record(record_json(bucket))
    assert record.usage.input_tokens == 250_000
    assert record.metadata["metrics"]["throttled_requests"] == 8
    assert record.metadata["metrics"]["cached_tokens"] is None
    assert record.metadata["missing_metrics"] == ["cached_tokens"]
    assert record.latency_ms == 420


def test_schema_v1_and_v2_fixtures_still_normalize():
    v1 = {
        "timestamp": "2026-09-14T12:00:00Z",
        "deployment_name": "legacy-deployment",
        "model_name": "gpt-4.1",
        "messages": [{"role": "user", "content": "hello"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }
    v2 = {
        "schema_version": 2,
        "timestamp": "2026-09-14T12:00:00Z",
        "deployment_name": "legacy-deployment",
        "model_name": "gpt-4.1",
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }
    assert normalize_record(v1).usage.input_tokens == 10
    assert normalize_record(v2).usage.output_tokens == 4


def test_config_rejects_content_capture_opt_in():
    with pytest.raises(ValueError):
        TelemetryConfig.from_mapping({"content_capture": True})


def test_config_reads_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKENLENS_TELEMETRY_DIR", str(tmp_path / "env-traces"))
    monkeypatch.setenv("TOKENLENS_TELEMETRY_MAX_MB", "5")
    monkeypatch.setenv("TOKENLENS_TELEMETRY_RETENTION_DAYS", "3")
    config = TelemetryConfig.from_env()
    assert config.output_dir == tmp_path / "env-traces"
    assert config.max_mb == 5
    assert config.retention_days == 3
    monkeypatch.setenv("TOKENLENS_TELEMETRY_SAMPLE_RATE", "2")
    with pytest.raises(ValueError):
        TelemetryConfig.from_env()
