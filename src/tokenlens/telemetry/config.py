"""Telemetry configuration resolved from explicit arguments, config file, or env."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .privacy import FINGERPRINT_KEY_ENV, FingerprintPolicy

DEFAULT_OUTPUT_DIR = "tokenlens-traces"
ENV_OUTPUT_DIR = "TOKENLENS_TELEMETRY_DIR"
ENV_MAX_MB = "TOKENLENS_TELEMETRY_MAX_MB"
ENV_RETENTION_DAYS = "TOKENLENS_TELEMETRY_RETENTION_DAYS"
ENV_SAMPLE_RATE = "TOKENLENS_TELEMETRY_SAMPLE_RATE"
ENV_STRICT = "TOKENLENS_TELEMETRY_STRICT"
ENV_ENABLED = "TOKENLENS_TELEMETRY_ENABLED"


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TelemetryConfig:
    """Local-only telemetry settings. No credentials are ever stored here."""

    output_dir: Path = Path(DEFAULT_OUTPUT_DIR)
    file_prefix: str = "tokenlens"
    max_mb: float = 50.0
    retention_days: int = 30
    sample_rate: float = 1.0
    strict: bool = False
    enabled: bool = True
    content_capture: bool = False
    fingerprints: FingerprintPolicy = FingerprintPolicy(enabled=False, key=None)

    @classmethod
    def from_env(cls, **overrides: Any) -> "TelemetryConfig":
        config = cls(
            output_dir=Path(os.getenv(ENV_OUTPUT_DIR) or DEFAULT_OUTPUT_DIR),
            max_mb=_float(ENV_MAX_MB, 50.0, minimum=0.1, maximum=10_240.0),
            retention_days=_int(ENV_RETENTION_DAYS, 30, minimum=0),
            sample_rate=_float(ENV_SAMPLE_RATE, 1.0, minimum=0.0001, maximum=1.0),
            strict=_bool(ENV_STRICT, False),
            enabled=_bool(ENV_ENABLED, True),
            fingerprints=FingerprintPolicy.from_env(),
        )
        known = {field: value for field, value in overrides.items() if value is not None}
        return replace(config, **known) if known else config

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None, **overrides: Any) -> "TelemetryConfig":
        """Build a config from the ``telemetry:`` block of ``.tokenlens.yml``."""
        settings = dict(value or {})
        rotation = settings.get("rotation") if isinstance(settings.get("rotation"), dict) else {}
        fingerprints = settings.get("fingerprints") if isinstance(settings.get("fingerprints"), dict) else {}
        if settings.get("content_capture"):
            raise ValueError(
                "telemetry.content_capture is not supported in this release; "
                "TokenLens collects contentless telemetry only"
            )
        config = cls.from_env(
            output_dir=Path(settings["output_dir"]) if settings.get("output_dir") else None,
            max_mb=float(rotation["max_mb"]) if rotation.get("max_mb") is not None else None,
            retention_days=int(rotation["retention_days"]) if rotation.get("retention_days") is not None else None,
            sample_rate=float(settings["sample_rate"]) if settings.get("sample_rate") is not None else None,
        )
        if fingerprints:
            policy = FingerprintPolicy.from_env(
                enabled=bool(fingerprints.get("enabled", False)),
                key_env=str(fingerprints.get("key_env") or FINGERPRINT_KEY_ENV),
            )
            config = replace(config, fingerprints=policy)
        known = {field: item for field, item in overrides.items() if item is not None}
        return replace(config, **known) if known else config
