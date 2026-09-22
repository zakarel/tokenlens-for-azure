"""Official public pricing sources, parsers, and the user-local snapshot cache.

Nothing in this package is imported by the analysis path for network use:
analysis reads only the cached snapshot written by an explicit synchronization.
"""

from __future__ import annotations

from .assumptions import (
    ASSUMPTIONS_BANNER,
    ASSUMPTIONS_WARNING,
    DEFAULT_PRICING_ASSUMPTIONS,
    PricingAssumptions,
)
from .cache import (
    AZURE_RETAIL_SNAPSHOT,
    CLAUDE_SNAPSHOT,
    PricingSnapshot,
    load_snapshot,
    snapshot_is_stale,
    snapshot_path,
    write_snapshot,
)
from .sync import (
    CLAUDE_SOURCE,
    PUBLIC_CATALOG_NAME,
    RETAIL_SOURCE,
    SyncOutcome,
    SyncReport,
    public_pricing_catalog,
    public_pricing_provenance,
    public_sync_needed,
    requested_sources,
    sync_public_pricing,
)

__all__ = [
    "ASSUMPTIONS_BANNER",
    "ASSUMPTIONS_WARNING",
    "AZURE_RETAIL_SNAPSHOT",
    "CLAUDE_SNAPSHOT",
    "CLAUDE_SOURCE",
    "RETAIL_SOURCE",
    "DEFAULT_PRICING_ASSUMPTIONS",
    "PUBLIC_CATALOG_NAME",
    "PricingAssumptions",
    "PricingSnapshot",
    "SyncOutcome",
    "SyncReport",
    "load_snapshot",
    "public_pricing_catalog",
    "public_pricing_provenance",
    "public_sync_needed",
    "requested_sources",
    "snapshot_is_stale",
    "snapshot_path",
    "sync_public_pricing",
    "write_snapshot",
]
