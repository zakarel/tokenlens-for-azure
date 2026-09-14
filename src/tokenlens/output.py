from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


def timestamp_slug(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.strftime("%Y%m%d-%H%M%SZ")


def timestamped_path(
    *,
    report_format: str,
    output_dir: str | Path = ".",
    now: datetime | None = None,
    comparison: bool = False,
) -> Path:
    extension = {"html": "html", "json": "json", "sarif": "sarif", "text": "txt"}[report_format]
    prefix = "tokenlens-comparison" if comparison else "tokenlens-report"
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{prefix}-{timestamp_slug(now)}.{extension}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{prefix}-{timestamp_slug(now)}-{counter}.{extension}"
        counter += 1
    return candidate


def choose_output(
    output: str | None,
    *,
    report_format: str,
    output_dir: str | Path = ".",
    comparison: bool = False,
) -> Path | None:
    if output:
        return Path(output)
    if report_format == "text":
        return None
    return timestamped_path(report_format=report_format, output_dir=output_dir, comparison=comparison)
