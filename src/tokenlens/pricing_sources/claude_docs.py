"""Official Claude pricing (``platform.claude.com``) parsed deterministically.

Foundry bills Claude through Anthropic consumption units (CCU). Anthropic
publishes token rates in US dollars, and Azure's Claude meters are denominated
in CCU at a fixed ``$0.01`` per CCU (100 CCU = $1). TokenLens therefore reads
the official dollar rates, converts them to CCU, and labels the result
``claude_ccu_equivalent`` so nobody mistakes it for an Azure token meter.

The parser is strict on purpose. If the published table changes shape, it
raises :class:`PricingSourceDrift`; the caller then keeps the previous cached
snapshot or a customer override. A rate is never invented from a similar model,
and customer-specific private discounts are never modelled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Literal, Sequence

from ..pricing import PriceEntry, PricingCatalog, canonical_model_name
from .assumptions import (
    USD_PER_CCU,
    DEFAULT_PRICING_ASSUMPTIONS,
    PricingAssumptions,
    canonical_service_tier,
    optional_deployment,
)
from .cache import CLAUDE_SNAPSHOT, PricingSnapshot, QuarantinedMeter
from .fetch import FetchBudget, Transport, UrlAllowList, content_sha256, fetch_text

__all__ = [
    "CLAUDE_ALLOW_LIST",
    "CLAUDE_CURRENCY",
    "CLAUDE_PRICING_URL",
    "ClaudeCurrencyError",
    "USD_PER_CCU",
    "ClaudeModelPricing",
    "ClaudePricingTable",
    "PricingSourceDrift",
    "claude_snapshot",
    "deployment_multiplier",
    "parse_claude_pricing",
    "sync_claude_pricing",
    "usd_to_ccu",
]

CLAUDE_PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing"

#: Exactly one host and one path. No prefix matching, no redirect following.
CLAUDE_ALLOW_LIST = UrlAllowList(
    hosts=frozenset({"platform.claude.com"}),
    paths=frozenset({"/docs/en/about-claude/pricing"}),
)

#: Anthropic publishes this page in US dollars only. TokenLens never converts
#: currencies, so a synchronization in any other currency must exclude Claude.
CLAUDE_CURRENCY = "USD"

#: Global standard is the baseline. The US data-zone premium applies only when
#: the deployment mode is *known* to be data-zone — never under the global
#: default assumption.
_DEPLOYMENT_MULTIPLIERS: dict[str, float] = {
    "global": 1.0,
    "data_zone": 1.1,
}

_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "base_input": ("base input tokens", "input tokens", "base input"),
    "cache_write_5m": ("5m cache writes", "5 m cache writes", "5-minute cache writes"),
    "cache_write_1h": ("1h cache writes", "1 h cache writes", "1-hour cache writes"),
    "cache_hit": ("cache hits & refreshes", "cache hits and refreshes", "cache hits"),
    "output": ("output tokens", "output"),
}

_PRICE_PATTERN = re.compile(r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*MTok", re.IGNORECASE)


class PricingSourceDrift(RuntimeError):
    """The official page no longer matches the committed parser contract."""


class ClaudeCurrencyError(ValueError):
    """Claude pricing is published in USD and is never converted."""


class _TableCollector(HTMLParser):
    """Collect every HTML table as a list of rows of plain-text cells."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001 - stdlib signature
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


@dataclass(frozen=True)
class ClaudeModelPricing:
    """One published Claude model row, in US dollars per million tokens."""

    model: str
    base_input_per_mtok: float
    cache_write_5m_per_mtok: float
    cache_write_1h_per_mtok: float
    cache_hit_per_mtok: float
    output_per_mtok: float

    def ccu(self) -> dict[str, float]:
        return {
            "base_input": usd_to_ccu(self.base_input_per_mtok),
            "cache_write_5m": usd_to_ccu(self.cache_write_5m_per_mtok),
            "cache_write_1h": usd_to_ccu(self.cache_write_1h_per_mtok),
            "cache_hit": usd_to_ccu(self.cache_hit_per_mtok),
            "output": usd_to_ccu(self.output_per_mtok),
        }


@dataclass(frozen=True)
class ClaudePricingTable:
    models: tuple[ClaudeModelPricing, ...]
    source_url: str = CLAUDE_PRICING_URL

    def find(self, model: str) -> ClaudeModelPricing | None:
        wanted = canonical_model_name(model)
        for item in self.models:
            if canonical_model_name(item.model) == wanted:
                return item
        return None


def usd_to_ccu(usd: float) -> float:
    """Convert a US-dollar rate into Anthropic consumption units."""
    return usd / USD_PER_CCU


def _ccu_text(usd: float) -> str:
    """Render a CCU figure exactly, never rounded away from the stored rate."""
    return f"{round(usd_to_ccu(usd), 2):g}"


def deployment_multiplier(deployment_mode: str | None) -> tuple[float, bool]:
    """Return ``(multiplier, is_exact)`` for a deployment mode.

    Only an exact ``data_zone`` mode attracts the US data-zone premium. An
    *unstated* mode falls back to the global-standard baseline and is reported
    as an assumption rather than silently priced at the higher rate. An
    *unrecognized* mode is a caller error and is rejected by the shared
    canonicalizer.
    """
    canonical = optional_deployment(deployment_mode)
    if canonical is None:
        return _DEPLOYMENT_MULTIPLIERS["global"], False
    if canonical not in _DEPLOYMENT_MULTIPLIERS:
        raise PricingSourceDrift(
            f"Claude pricing is not published for the {canonical} deployment mode."
        )
    return _DEPLOYMENT_MULTIPLIERS[canonical], True


def _price(cell: str, *, model: str, column: str) -> float:
    match = _PRICE_PATTERN.search(cell)
    if not match:
        raise PricingSourceDrift(
            f"The published Claude pricing cell for {model!r} / {column} is not a "
            f"'$N / MTok' value: {cell!r}."
        )
    return float(match.group(1))


def _header_index(header: Sequence[str]) -> dict[str, int] | None:
    normalized = [cell.casefold().strip() for cell in header]
    mapping: dict[str, int] = {}
    for key, aliases in _COLUMN_ALIASES.items():
        for index, cell in enumerate(normalized):
            if cell in aliases:
                mapping[key] = index
                break
        else:
            return None
    return mapping


def parse_claude_pricing(html: str) -> ClaudePricingTable:
    """Parse the official pricing table. Raises on any structural drift."""
    collector = _TableCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception as exc:  # noqa: BLE001 - malformed markup is drift
        raise PricingSourceDrift(f"The Claude pricing page could not be parsed ({type(exc).__name__}).") from exc

    for table in collector.tables:
        if not table:
            continue
        columns = _header_index(table[0])
        if columns is None:
            continue
        models: list[ClaudeModelPricing] = []
        width = max(columns.values()) + 1
        for row in table[1:]:
            if len(row) < width or not row[0].strip():
                raise PricingSourceDrift(
                    f"A Claude pricing row does not match the published column layout: {row!r}."
                )
            model = row[0].strip()
            models.append(
                ClaudeModelPricing(
                    model=model,
                    base_input_per_mtok=_price(row[columns["base_input"]], model=model, column="base input"),
                    cache_write_5m_per_mtok=_price(row[columns["cache_write_5m"]], model=model, column="5m cache write"),
                    cache_write_1h_per_mtok=_price(row[columns["cache_write_1h"]], model=model, column="1h cache write"),
                    cache_hit_per_mtok=_price(row[columns["cache_hit"]], model=model, column="cache hit"),
                    output_per_mtok=_price(row[columns["output"]], model=model, column="output"),
                )
            )
        if not models:
            raise PricingSourceDrift("The Claude pricing table contains no model rows.")
        return ClaudePricingTable(models=tuple(models))
    raise PricingSourceDrift(
        "No table on the Claude pricing page matches the expected columns "
        "(model, base input, 5m cache writes, 1h cache writes, cache hits, output)."
    )


CacheTier = Literal["5m", "1h"]


def build_price_entry(
    pricing: ClaudeModelPricing,
    *,
    deployment_mode: str | None = None,
    cache_tier: CacheTier = "5m",
    effective_from: date | None = None,
    retrieved_at: datetime | None = None,
    source_hash: str | None = None,
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS,
) -> PriceEntry:
    """One Claude catalog entry, billed on a CCU-equivalent basis."""
    multiplier, exact = deployment_multiplier(deployment_mode)
    mode = "data_zone" if multiplier > 1.0 and exact else "global"
    assumed = [f"purchase_model={assumptions.purchase_model}", f"service_tier={assumptions.service_tier}"]
    if not exact:
        assumed.insert(0, f"deployment={assumptions.deployment}")
    tier = canonical_service_tier(assumptions.service_tier)
    write_rate = (
        pricing.cache_write_5m_per_mtok if cache_tier == "5m" else pricing.cache_write_1h_per_mtok
    )
    # Every figure quoted in the note is derived from the rate actually stored
    # on the entry, so the provenance can never disagree with the arithmetic.
    stored_input = pricing.base_input_per_mtok * multiplier
    stored_cache_hit = pricing.cache_hit_per_mtok * multiplier
    stored_write = write_rate * multiplier
    stored_output = pricing.output_per_mtok * multiplier
    return PriceEntry(
        provider="azure_foundry",
        publisher="anthropic",
        model=canonical_model_name(pricing.model),
        aliases=[pricing.model],
        service_tier=tier,
        region=mode,
        effective_from=effective_from or (retrieved_at or datetime.now(UTC)).date(),
        billing_basis="claude_ccu_equivalent",
        confidence="verified",
        source_url=CLAUDE_PRICING_URL,
        source_rank=10,
        context_window=assumptions.context,
        assumed_dimensions=sorted(assumed),
        content_hash=source_hash,
        retrieved_at=retrieved_at,
        note=(
            f"Anthropic published USD token rates converted to CCU at {USD_PER_CCU:g} USD/CCU "
            f"(100 CCU = $1), after the {multiplier:g}x {mode} deployment multiplier: "
            f"input {_ccu_text(stored_input)} CCU/MTok, "
            f"{cache_tier} cache write {_ccu_text(stored_write)} CCU/MTok, "
            f"cache hit {_ccu_text(stored_cache_hit)} CCU/MTok, "
            f"output {_ccu_text(stored_output)} CCU/MTok. "
            f"Published base rates before the multiplier: input {pricing.base_input_per_mtok:g} USD, "
            f"{cache_tier} cache write {write_rate:g} USD, "
            f"cache hit {pricing.cache_hit_per_mtok:g} USD, "
            f"output {pricing.output_per_mtok:g} USD per MTok. "
            "Customer-specific private discounts are not included."
        ),
        input_per_million=stored_input,
        cached_input_per_million=stored_cache_hit,
        cache_write_per_million=stored_write,
        output_per_million=stored_output,
    )


def claude_snapshot(
    *,
    models: Sequence[str] | None = None,
    deployment_modes: Sequence[str] | None = None,
    currency: str = CLAUDE_CURRENCY,
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS,
    budget: FetchBudget | None = None,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> PricingSnapshot:
    """Fetch and parse the official Claude pricing page into a snapshot."""
    if currency.upper() != CLAUDE_CURRENCY:
        raise ClaudeCurrencyError(
            f"Anthropic publishes Claude pricing in {CLAUDE_CURRENCY} only. TokenLens never "
            f"converts currencies, so it cannot produce a {currency.upper()} Claude snapshot."
        )
    retrieved_at = now or datetime.now(UTC)
    page = fetch_text(
        CLAUDE_PRICING_URL,
        allow_list=CLAUDE_ALLOW_LIST,
        budget=budget or FetchBudget(),
        transport=transport,
    )
    source_hash = content_sha256(page.body)
    table = parse_claude_pricing(page.text())
    return snapshot_from_table(
        table,
        source_hash=source_hash,
        models=models,
        deployment_modes=deployment_modes,
        assumptions=assumptions,
        retrieved_at=retrieved_at,
    )


def snapshot_from_table(
    table: ClaudePricingTable,
    *,
    source_hash: str,
    models: Sequence[str] | None = None,
    deployment_modes: Sequence[str] | None = None,
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS,
    retrieved_at: datetime | None = None,
) -> PricingSnapshot:
    retrieved = retrieved_at or datetime.now(UTC)
    wanted = [canonical_model_name(item) for item in models] if models else None
    # ``None`` means the deployment mode was never stated. It is preserved as
    # such so the global default is applied *and labelled*, instead of being
    # coerced into an exact "global" that hides the assumption.
    modes: list[str | None] = (
        [optional_deployment(item) for item in deployment_modes] if deployment_modes else [None]
    )
    supported = [mode for mode in modes if mode is None or mode in _DEPLOYMENT_MULTIPLIERS]
    unsupported = [mode for mode in modes if mode is not None and mode not in _DEPLOYMENT_MULTIPLIERS]
    entries: list[PriceEntry] = []
    quarantined: list[QuarantinedMeter] = []
    priced_models = [
        pricing
        for pricing in table.models
        if wanted is None or canonical_model_name(pricing.model) in wanted
    ]
    for pricing in priced_models:
        for mode in supported:
            # One unsupported mode never costs the caller the supported ones.
            entries.append(
                build_price_entry(
                    pricing,
                    deployment_mode=mode,
                    retrieved_at=retrieved,
                    source_hash=source_hash,
                    assumptions=assumptions,
                )
            )
        for mode in unsupported:
            quarantined.append(
                QuarantinedMeter(
                    reason="deployment-mode-not-published",
                    meter_name=pricing.model,
                    detail=(
                        f"requested={mode}; Anthropic publishes Claude on Foundry for "
                        f"{', '.join(sorted(_DEPLOYMENT_MULTIPLIERS))} only"
                    ),
                )
            )
    if wanted is not None:
        missing = [item for item in wanted if table.find(item) is None]
        quarantined.extend(
            QuarantinedMeter(reason="model-not-published", meter_name=item) for item in missing
        )
    empty_reason: str | None = None
    if not entries:
        if unsupported and not supported:
            empty_reason = (
                f"No Claude rate was published: the requested deployment mode(s) "
                f"{', '.join(sorted(str(mode) for mode in unsupported))} are not offered for Claude "
                f"on Foundry. Supported modes: {', '.join(sorted(_DEPLOYMENT_MULTIPLIERS))}."
            )
        elif not priced_models:
            empty_reason = (
                "No Claude rate was published: none of the requested models appears on the "
                "official pricing page."
            )
    catalog = PricingCatalog(
        currency=CLAUDE_CURRENCY,
        catalog_name="claude-official-pricing",
        source_url=CLAUDE_PRICING_URL,
        retrieved_at=retrieved.date(),
        pricing_basis="effective_period",
        assumptions=assumptions.to_metadata()["dimensions"],  # type: ignore[arg-type]
        prices=entries,
    )
    return PricingSnapshot(
        source="claude_pricing_docs",
        source_url=CLAUDE_PRICING_URL,
        retrieved_at=retrieved,
        content_hash=source_hash,
        pages_read=1,
        rows_read=len(table.models),
        currency=CLAUDE_CURRENCY,
        feed_complete=True,
        empty_reason=empty_reason,
        assumptions=assumptions,
        assumed_dimensions=sorted({item for entry in entries for item in entry.assumed_dimensions}),
        catalog=catalog,
        quarantined=quarantined,
        notes=[
            *([empty_reason] if empty_reason else []),
            f"Foundry bills Claude in Anthropic consumption units at {USD_PER_CCU:g} USD/CCU "
            "(100 CCU = $1); the reported figure is an estimated USD token cost with an "
            "equivalent CCU estimate.",
            "Global standard is the 1.0x baseline; the 1.1x US data-zone premium is applied only "
            "when the deployment mode is exactly data-zone.",
            "Customer-specific private discounts are not included.",
        ],
    )


def sync_claude_pricing(**kwargs: object) -> tuple[PricingSnapshot, str]:
    snapshot = claude_snapshot(**kwargs)  # type: ignore[arg-type]
    return snapshot, CLAUDE_SNAPSHOT
