"""User-local pricing snapshots: atomic writes, ``0600``, offline reads.

A snapshot is the only thing synchronization produces. Analysis reads it from
disk and never contacts a network, so a report is reproducible from the cached
snapshot alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..foundry_workflow.configuration import atomic_write, pricing_cache_dir
from ..pricing import PricingCatalog
from .assumptions import DEFAULT_PRICING_ASSUMPTIONS, PricingAssumptions
from .fetch import content_sha256

__all__ = [
    "AZURE_RETAIL_SNAPSHOT",
    "CLAUDE_SNAPSHOT",
    "DEFAULT_MAX_AGE_DAYS",
    "SOURCE_NAMES",
    "SOURCE_SELECTION",
    "PricingSnapshot",
    "QuarantinedMeter",
    "content_hash",
    "load_snapshot",
    "load_source_selection",
    "save_source_selection",
    "snapshot_is_stale",
    "snapshot_path",
    "write_snapshot",
]

AZURE_RETAIL_SNAPSHOT = "azure-retail-foundry.json"
CLAUDE_SNAPSHOT = "claude-pricing-docs.json"
SOURCE_SELECTION = "pricing-sources.json"
DEFAULT_MAX_AGE_DAYS = 7
SNAPSHOT_SCHEMA_VERSION = 2

#: source identifier -> cache file name.
SOURCE_NAMES: dict[str, str] = {
    "azure_retail_prices": AZURE_RETAIL_SNAPSHOT,
    "claude_pricing_docs": CLAUDE_SNAPSHOT,
}

#: Re-exported so callers hash bytes the same way the fetcher does.
content_hash = content_sha256



class QuarantinedMeter(BaseModel):
    """One source row TokenLens refused to price, with the exact reason."""

    model_config = ConfigDict(extra="forbid")

    reason: str
    meter_id: str | None = None
    meter_name: str | None = None
    sku_name: str | None = None
    product_name: str | None = None
    detail: str | None = None


class PricingSnapshot(BaseModel):
    """A dated, hashed, provenance-carrying public pricing snapshot."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    source: Literal["azure_retail_prices", "claude_pricing_docs"]
    source_url: str
    api_version: str | None = None
    retrieved_at: datetime
    content_hash: str
    pages_read: int = 0
    rows_read: int = 0
    #: The billing currency the source was requested and published in.
    currency: str = "USD"
    #: ``False`` when a ceiling truncated the source. A truncated snapshot is
    #: never written, so a loaded snapshot is always complete.
    feed_complete: bool = True
    #: Why a complete, successful read produced no priced entry. A snapshot can
    #: legitimately be empty; the reason is always stated rather than implied.
    empty_reason: str | None = None
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS
    assumed_dimensions: list[str] = Field(default_factory=list)
    catalog: PricingCatalog
    quarantined: list[QuarantinedMeter] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def age(self, *, now: datetime | None = None) -> timedelta:
        current = now or datetime.now(UTC)
        retrieved = self.retrieved_at
        if retrieved.tzinfo is None:
            retrieved = retrieved.replace(tzinfo=UTC)
        return current - retrieved

    def provenance(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_url": self.source_url,
            "api_version": self.api_version,
            "retrieved_at": self.retrieved_at.isoformat(),
            "content_hash": self.content_hash,
            "currency": self.currency,
            "feed_complete": self.feed_complete,
            "empty_reason": self.empty_reason,
            "entries": len(self.catalog.prices),
            "quarantined": len(self.quarantined),
            "catalog_assumed_dimensions": list(self.assumed_dimensions),
        }


def snapshot_path(name: str) -> Path:
    return pricing_cache_dir() / name


def write_snapshot(snapshot: PricingSnapshot, *, name: str) -> Path:
    """Persist a snapshot atomically with user-only permissions.

    An incomplete snapshot is refused outright: overwriting a complete cache
    with a truncated read would silently degrade every later analysis.
    """
    if not snapshot.feed_complete:
        raise ValueError(
            "A truncated pricing feed is never cached. The previous complete snapshot, the "
            "packaged catalog, and any customer rates remain in effect."
        )
    target = snapshot_path(name)
    payload = json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    atomic_write(target, payload, mode=0o600)
    return target


def load_snapshot(name: str) -> PricingSnapshot | None:
    """Read a cached snapshot. A corrupt or foreign file is ignored, not trusted."""
    target = snapshot_path(name)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        return None
    try:
        return PricingSnapshot.model_validate(payload)
    except ValueError:
        return None


def snapshot_is_stale(
    snapshot: PricingSnapshot | None,
    *,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> bool:
    if snapshot is None:
        return True
    return snapshot.age(now=now) > timedelta(days=max_age_days)


def load_source_selection() -> list[str] | None:
    """Which sources the last synchronization was asked for, if recorded.

    Freshness is judged against the *requested* sources. A user who never asks
    for Claude must not be told forever that a synchronization is overdue.
    """
    target = snapshot_path(SOURCE_SELECTION)
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    sources = payload.get("sources")
    if not isinstance(sources, list):
        return None
    resolved = [str(item) for item in sources if str(item) in SOURCE_NAMES]
    return resolved or None


def save_source_selection(sources: Sequence[str]) -> Path:
    """Remember the requested source set, atomically and user-only."""
    resolved = [item for item in dict.fromkeys(sources) if item in SOURCE_NAMES]
    payload = json.dumps(
        {"schema_version": SNAPSHOT_SCHEMA_VERSION, "sources": resolved}, indent=2, sort_keys=True
    )
    target = snapshot_path(SOURCE_SELECTION)
    atomic_write(target, payload + "\n", mode=0o600)
    return target
