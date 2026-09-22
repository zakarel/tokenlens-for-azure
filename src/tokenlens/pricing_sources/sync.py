"""Orchestrate public pricing synchronization and expose the cached result.

Analysis only ever reads :func:`public_pricing_catalog`, which touches the
local cache and nothing else. Network access happens exclusively inside
:func:`sync_public_pricing`, and every failure is returned as data so the
caller can continue without cost rather than abort.

Two invariants hold throughout: a truncated feed is never cached, and catalogs
denominated in different currencies are never merged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from ..pricing import PricingCatalog
from .assumptions import (
    DEFAULT_PRICING_ASSUMPTIONS,
    PricingAssumptions,
    PricingDimensionError,
    canonical_context,
    canonical_deployment,
)
from .azure_retail import retail_snapshot
from .cache import (
    AZURE_RETAIL_SNAPSHOT,
    CLAUDE_SNAPSHOT,
    DEFAULT_MAX_AGE_DAYS,
    SOURCE_NAMES,
    PricingSnapshot,
    load_snapshot,
    load_source_selection,
    save_source_selection,
    snapshot_is_stale,
    snapshot_path,
    write_snapshot,
)
from .claude_docs import (
    CLAUDE_CURRENCY,
    ClaudeCurrencyError,
    PricingSourceDrift,
    claude_snapshot,
)
from .fetch import BudgetExceededError, FetchBudget, FetchError, Transport

__all__ = [
    "CLAUDE_SOURCE",
    "PUBLIC_CATALOG_NAME",
    "RETAIL_SOURCE",
    "SyncOutcome",
    "SyncReport",
    "cached_snapshots",
    "public_pricing_catalog",
    "public_pricing_provenance",
    "public_sync_needed",
    "requested_sources",
    "sync_public_pricing",
]

PUBLIC_CATALOG_NAME = "public-pricing-sync"
RETAIL_SOURCE = "azure_retail_prices"
CLAUDE_SOURCE = "claude_pricing_docs"


@dataclass
class SyncOutcome:
    """Result for one source. ``ok`` false never raises to the caller."""

    source: str
    ok: bool
    entries: int = 0
    quarantined: int = 0
    path: Path | None = None
    retrieved_at: datetime | None = None
    content_hash: str | None = None
    error: str | None = None
    used_cache: bool = False
    #: The source was deliberately not attempted, with a stated reason.
    skipped: bool = False
    reason: str | None = None
    #: ``False`` when a ceiling truncated the feed, so nothing was cached.
    feed_complete: bool = True

    @property
    def status(self) -> str:
        if self.skipped:
            return "skipped"
        return "ok" if self.ok else "failed"


@dataclass
class SyncReport:
    outcomes: list[SyncOutcome] = field(default_factory=list)
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and all(outcome.ok for outcome in self.outcomes)

    @property
    def entries(self) -> int:
        return sum(outcome.entries for outcome in self.outcomes)

    def errors(self) -> list[str]:
        return [f"{o.source}: {o.error}" for o in self.outcomes if o.error]


def cached_snapshots() -> list[PricingSnapshot]:
    """Every readable cached snapshot, in resolution order."""
    return [
        snapshot
        for snapshot in (load_snapshot(AZURE_RETAIL_SNAPSHOT), load_snapshot(CLAUDE_SNAPSHOT))
        if snapshot is not None
    ]


def requested_sources(*, include_claude: bool | None = None) -> list[str]:
    """The sources freshness and status should be judged against.

    An explicit ``include_claude`` wins. Otherwise the last recorded selection
    is used, so a user who synchronizes without Claude is not told forever that
    a synchronization is overdue. With neither, both sources are assumed.
    """
    if include_claude is not None:
        return [RETAIL_SOURCE, *([CLAUDE_SOURCE] if include_claude else [])]
    recorded = load_source_selection()
    if recorded:
        return recorded
    return [RETAIL_SOURCE, CLAUDE_SOURCE]


def public_sync_needed(
    *,
    include_claude: bool | None = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: datetime | None = None,
) -> bool:
    """True when a *requested* snapshot is absent or past the freshness budget."""
    return any(
        snapshot_is_stale(load_snapshot(SOURCE_NAMES[source]), max_age_days=max_age_days, now=now)
        for source in requested_sources(include_claude=include_claude)
    )


def _usable_snapshots(currency: str) -> tuple[list[PricingSnapshot], list[PricingSnapshot]]:
    """Split cached snapshots into those matching ``currency`` and those not."""
    wanted = currency.upper()
    snapshots = cached_snapshots()
    included = [item for item in snapshots if item.currency.upper() == wanted]
    excluded = [item for item in snapshots if item.currency.upper() != wanted]
    return included, excluded


def public_pricing_catalog(*, currency: str = "USD") -> PricingCatalog | None:
    """Merge the cached snapshots for one currency into an offline catalog.

    A snapshot in another currency is excluded rather than merged: TokenLens
    never converts, and a single catalog carries a single currency.
    """
    included, _ = _usable_snapshots(currency)
    if not included:
        return None
    prices = [price for snapshot in included for price in snapshot.catalog.prices]
    if not prices:
        return None
    newest = max(included, key=lambda item: item.retrieved_at)
    assumptions: dict[str, str] = {}
    for snapshot in included:
        if snapshot.catalog.assumptions:
            assumptions.update(snapshot.catalog.assumptions)
    return PricingCatalog(
        currency=currency.upper(),
        catalog_name=PUBLIC_CATALOG_NAME,
        source_url=newest.catalog.source_url,
        retrieved_at=newest.retrieved_at.date(),
        # Published pricing is a snapshot of current rates; it is applied as an
        # analysis-date estimate, which the report states explicitly.
        pricing_basis="analysis_date_snapshot",
        assumptions=assumptions or None,
        prices=prices,
    )


def public_pricing_provenance(*, currency: str = "USD") -> list[dict[str, object]]:
    """Per-source provenance, marking which snapshots were actually usable."""
    included, excluded = _usable_snapshots(currency)
    records: list[dict[str, object]] = [
        {**snapshot.provenance(), "included": True} for snapshot in included
    ]
    for snapshot in excluded:
        records.append(
            {
                **snapshot.provenance(),
                "included": False,
                "excluded_reason": (
                    f"currency {snapshot.currency.upper()} differs from the reporting currency "
                    f"{currency.upper()}; TokenLens never converts currencies"
                ),
            }
        )
    return records


def sync_public_pricing(
    *,
    currency: str = "USD",
    region: str | None = None,
    account_region: str | None = None,
    deployments: Sequence[str] | None = None,
    contexts: Sequence[str] | None = None,
    include_claude: bool = True,
    claude_models: Sequence[str] | None = None,
    claude_deployment_modes: Sequence[str] | None = None,
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS,
    budget: FetchBudget | None = None,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Synchronize the official sources. Each failure is reported, never raised."""
    report = SyncReport(assumptions=assumptions)
    moment = now or datetime.now(UTC)
    budget = budget or FetchBudget()
    target_currency = currency.upper()

    # Requested dimensions are canonicalized once, here, so both sources see
    # the same values and an unsupported one fails before any request.
    canonical_deployments = (
        [canonical_deployment(item) for item in deployments] if deployments else None
    )
    canonical_contexts = [canonical_context(item) for item in contexts] if contexts else None
    canonical_claude_modes = (
        [canonical_deployment(item) for item in claude_deployment_modes]
        if claude_deployment_modes
        else None
    )

    report.outcomes.append(
        _run(
            RETAIL_SOURCE,
            AZURE_RETAIL_SNAPSHOT,
            lambda: retail_snapshot(
                currency=target_currency,
                region=region,
                assumptions=assumptions,
                deployments=canonical_deployments,
                contexts=canonical_contexts,
                account_region=account_region,
                budget=budget,
                transport=transport,
                now=moment,
            ),
        )
    )
    if include_claude and target_currency != CLAUDE_CURRENCY:
        # Excluding Claude with a stated reason is honest; converting its
        # published USD rates into another currency would not be.
        report.outcomes.append(
            SyncOutcome(
                source=CLAUDE_SOURCE,
                ok=True,
                skipped=True,
                reason=(
                    f"Anthropic publishes Claude pricing in {CLAUDE_CURRENCY} only, and TokenLens "
                    f"never converts currencies, so it is excluded from this {target_currency} "
                    "synchronization. Claude cost stays unresolved unless you record a "
                    f"{target_currency} contracted rate."
                ),
            )
        )
    elif include_claude:
        report.outcomes.append(
            _run(
                CLAUDE_SOURCE,
                CLAUDE_SNAPSHOT,
                lambda: claude_snapshot(
                    models=claude_models,
                    deployment_modes=canonical_claude_modes,
                    currency=target_currency,
                    assumptions=assumptions,
                    budget=budget,
                    transport=transport,
                    now=moment,
                ),
            )
        )
    # Freshness is judged against what was actually attempted.
    save_source_selection([outcome.source for outcome in report.outcomes if not outcome.skipped])
    return report


def _run(source: str, cache_name: str, factory) -> SyncOutcome:  # noqa: ANN001 - internal
    try:
        snapshot = factory()
    except (
        FetchError,
        PricingSourceDrift,
        ClaudeCurrencyError,
        PricingDimensionError,
        ValueError,
    ) as exc:
        cached = load_snapshot(cache_name)
        truncated = isinstance(exc, BudgetExceededError)
        return SyncOutcome(
            source=source,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            used_cache=cached is not None,
            entries=len(cached.catalog.prices) if cached else 0,
            retrieved_at=cached.retrieved_at if cached else None,
            content_hash=cached.content_hash if cached else None,
            path=snapshot_path(cache_name) if cached else None,
            feed_complete=not truncated,
            reason=(
                "The feed was truncated by a page, item, or size ceiling. Nothing was written, so "
                "the previous complete snapshot is unchanged and no meter is reported as missing."
                if truncated
                else None
            ),
        )
    path = write_snapshot(snapshot, name=cache_name)
    return SyncOutcome(
        source=source,
        ok=True,
        entries=len(snapshot.catalog.prices),
        quarantined=len(snapshot.quarantined),
        path=path,
        retrieved_at=snapshot.retrieved_at,
        content_hash=snapshot.content_hash,
        feed_complete=snapshot.feed_complete,
        # A complete read that priced nothing is a valid outcome, and the
        # reason travels with it rather than leaving a silent empty snapshot.
        reason=snapshot.empty_reason,
    )
