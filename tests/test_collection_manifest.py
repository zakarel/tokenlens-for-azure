from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tokenlens.telemetry.manifest import build_deployment_manifest
from tokenlens.telemetry.schema import BucketMetrics, MetricBucketRecord


def test_collection_manifest_separates_requested_returned_and_active_coverage():
    start = datetime(2026, 9, 1, tzinfo=UTC)
    records = [
        MetricBucketRecord(
            event_id="synthetic-manifest-0",
            timestamp=start,
            deployment_name="synthetic-deployment-label",
            model_name="tts-1",
            modality="text_to_speech",
            usage_bases=("characters", "audio_seconds"),
            capability_status="verified",
            capability_catalog_version="2026-10-06.1",
            metrics=BucketMetrics(input_tokens=None, output_tokens=None, requests=1),
            missing_metrics=["input_tokens", "output_tokens"],
            metric_provenance={"requests": "ModelRequests"},
        ),
        MetricBucketRecord(
            event_id="synthetic-manifest-2",
            timestamp=start + timedelta(minutes=10),
            deployment_name="synthetic-deployment-label",
            model_name="tts-1",
            modality="text_to_speech",
            usage_bases=("characters", "audio_seconds"),
            capability_status="verified",
            capability_catalog_version="2026-10-06.1",
            metrics=BucketMetrics(input_tokens=None, output_tokens=None, requests=2),
            missing_metrics=["input_tokens", "output_tokens"],
            metric_provenance={"requests": "ModelRequests"},
        ),
    ]

    manifest = build_deployment_manifest(
        run_id="synthetic-run",
        identity_scope="synthetic-scope",
        deployment_name="synthetic-deployment-label",
        model_name="tts-1",
        model_version="1",
        modality="text_to_speech",
        usage_bases=("characters", "audio_seconds"),
        capability_catalog_version="2026-10-06.1",
        collection_state="succeeded",
        requested_start=start,
        requested_end=start + timedelta(minutes=20),
        bucket_minutes=5,
        records=records,
        requested_metrics=("ModelRequests", "InputTokens", "GeneratedTokens"),
        pages_read=2,
        retries=1,
    )

    assert manifest.expected_buckets == 4
    assert manifest.observed_buckets == 2
    assert manifest.active_buckets == 2
    assert manifest.requested_days == 0.01
    assert manifest.observed_days == 1
    assert manifest.metric_coverage["requests"].points_reported == 2
    assert manifest.metric_coverage["input_tokens"].state == "unavailable"
    assert manifest.metric_coverage["output_tokens"].state == "unavailable"
    assert manifest.pages_read == 2
    assert manifest.retries == 1
    assert len(manifest.gap_intervals) == 2
    serialized = manifest.model_dump_json()
    assert "synthetic-deployment-label" not in serialized
    assert manifest.deployment_ref != "synthetic-deployment-label"
