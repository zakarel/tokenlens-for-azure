"""Canonical privacy-first telemetry collection for TokenLens.

Collection adapters live in :mod:`tokenlens.integrations` (SDK wrappers),
:mod:`tokenlens.telemetry.otel` (OpenTelemetry), and
:mod:`tokenlens.foundry.monitor` (Azure Monitor). They all emit the canonical
records defined in :mod:`tokenlens.telemetry.schema` through the shared
:class:`~tokenlens.telemetry.writer.TelemetryWriter`.
"""

from __future__ import annotations

from .config import TelemetryConfig
from .privacy import ContentLeakError, FingerprintPolicy, assert_contentless
from .schema import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    BucketMetrics,
    ContentFeatures,
    Fingerprints,
    MetricBucketRecord,
    ModelRequestRecord,
    ResourceScope,
    TelemetryUsage,
    deterministic_event_id,
    parse_record,
    record_json,
)
from .writer import TelemetryWriteError, TelemetryWriter

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "BucketMetrics",
    "ContentFeatures",
    "ContentLeakError",
    "Fingerprints",
    "FingerprintPolicy",
    "MetricBucketRecord",
    "ModelRequestRecord",
    "ResourceScope",
    "TelemetryConfig",
    "TelemetryUsage",
    "TelemetryWriteError",
    "TelemetryWriter",
    "assert_contentless",
    "deterministic_event_id",
    "parse_record",
    "record_json",
]
