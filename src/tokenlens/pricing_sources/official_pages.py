"""Fail-closed parser for official Foundry Models pricing-page tables."""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin

from .fetch import FetchBudget, Transport, UrlAllowList, content_sha256, fetch_text
from ..pricing import PriceEntry, PricingCatalog
from .cache import PricingSnapshot, write_snapshot
from datetime import UTC, datetime

OFFICIAL_PRICING_ROOT = "https://azure.microsoft.com/en-us/pricing/details/ai-foundry-models/"
OFFICIAL_ROUTES = {
    "aoai": "aoai/",
    "mai": "mai/",
    "fireworks": "fireworks/",
    "mistral": "mistral-ai/",
    "deepseek": "deepseek/",
    "cohere": "cohere/",
    "grok": "grok/",
    "llama": "llama/",
    "black_forest_labs": "black-forest-labs/",
    "kimi": "kimi/",
    "fine_tuning": "fine-tuning-models/",
    "model_router": "model-router/",
}
OFFICIAL_ALLOW_LIST = UrlAllowList(
    hosts=frozenset({"azure.microsoft.com"}),
    paths=frozenset({
        "/en-us/pricing/details/ai-foundry-models/" + route
        for route in OFFICIAL_ROUTES.values()
    }),
)


@dataclass(frozen=True)
class OfficialPageRate:
    provider_route: str
    table_heading: str
    model_label: str
    price_text: str
    components: dict[str, float]
    unit: str
    currency: str
    source_url: str
    content_hash: str
    parser_version: str = "2026-10-06.1"


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[list[str], list[list[str]]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._heading = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.casefold()
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []
            self._heading = tag == "th"
        elif tag == "span" and self._cell is not None:
            data = dict(attrs).get("data-amount")
            if data:
                self._cell.append(f"__DATA_AMOUNT__{data}")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if len(self._table) >= 2:
                self.tables.append((self._table[0], self._table[1:]))
            self._table = None


def official_page_url(provider_route: str) -> str:
    route = OFFICIAL_ROUTES.get(provider_route, provider_route)
    url = urljoin(OFFICIAL_PRICING_ROOT, route)
    return OFFICIAL_ALLOW_LIST.validate(url)


def _money(value: str) -> float | None:
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", value.replace(",", ""))
    return float(match.group(1)) if match else None


def _published_amount(value: str) -> float | None:
    amount = _money(value)
    if amount is not None:
        return amount
    marker = "__DATA_AMOUNT__"
    if marker not in value:
        return None
    try:
        encoded = value.split(marker, 1)[1]
        encoded = encoded[: encoded.rfind("}") + 1]
        payload = json.loads(encoded)
        regional = payload.get("regional", {})
        return float(next(iter(regional.values()))) if regional else None
    except (ValueError, TypeError, StopIteration):
        return None


def _components(price_text: str) -> tuple[dict[str, float], str] | None:
    text = " ".join(price_text.split())
    unit_match = re.search(r"/\s*(1K|1M|1,000|1,000,000)\s*(Tokens?|Pages?|Characters?|Images?|Hours?|Seconds?)", text, re.I)
    unit = unit_match.group(0).strip() if unit_match else ""
    values: dict[str, float] = {}
    for label, key in (("input", "input"), ("output", "output"), ("cached", "cached_input")):
        match = re.search(label + r"\s*:\s*\$?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
        if match:
            values[key] = float(match.group(1))
    if not values:
        amount = _money(text)
        if amount is None:
            return None
        values["unit"] = amount
    return values, unit or "published unit"


def parse_official_page(body: str, *, provider_route: str, source_url: str | None = None) -> list[OfficialPageRate]:
    parser = _TableParser()
    parser.feed(body)
    if not parser.tables:
        raise ValueError("official pricing page contains no semantic pricing tables")
    rates: list[OfficialPageRate] = []
    for headers, rows in parser.tables:
        if len(headers) < 2:
            continue
        header_text = " ".join(headers).casefold()
        if provider_route == "fine_tuning" and not (
            "training" in header_text and "input usage" in header_text
        ):
            continue
        heading = headers[0]
        for row in rows:
            if len(row) < 2:
                continue
            if any("training" in header.casefold() or "hosting" in header.casefold() for header in headers):
                values = {}
                for header, value in zip(headers[1:], row[1:]):
                    amount = _published_amount(value)
                    if amount is None:
                        continue
                    lowered = header.casefold()
                    if "training" in lowered:
                        values["training"] = amount
                    elif "hosting" in lowered:
                        values["hosting"] = amount
                    elif "input" in lowered:
                        values["input"] = amount
                    elif "output" in lowered:
                        values["output"] = amount
                if "input" not in values or "output" not in values:
                    continue
                unit = "published column units"
                price_text = " | ".join(row[1:])
            else:
                parsed = _components(row[1])
                if parsed is None:
                    raise ValueError(f"unrecognized published price syntax for {row[0]!r}")
                values, unit = parsed
                price_text = row[1]
            rates.append(OfficialPageRate(
                provider_route=provider_route,
                table_heading=heading,
                model_label=row[0],
                price_text=price_text,
                components=values,
                unit=unit,
                currency="USD",
                source_url=source_url or official_page_url(provider_route),
                content_hash=content_sha256(body),
            ))
    if not rates:
        raise ValueError("official pricing tables contained no parseable rate rows")
    return rates


def page_catalog(rates: Iterable[OfficialPageRate], *, retrieved_at: datetime | None = None) -> PricingCatalog:
    """Convert page rows to a public catalog without inventing missing components."""
    rows = list(rates)
    if not rows:
        raise ValueError("cannot build an official-page catalog from no rows")
    prices: list[PriceEntry] = []
    for row in rows:
        if "input" not in row.components or "output" not in row.components:
            continue
        label = row.model_label.casefold()
        model = "ministral-3b" if "ministral 3b" in label or "ministral-3b" in label else row.model_label
        region = "data_zone" if "data zone" in label else "regional" if "regional" in label else "global" if "global" in label else None
        prices.append(PriceEntry(
            provider="azure_foundry",
            model=model,
            aliases=[row.model_label],
            region=region,
            effective_from=(retrieved_at or datetime.now(UTC)).date(),
            billing_basis="token_rate",
            confidence="verified",
            source_url=row.source_url,
            content_hash=row.content_hash,
            retrieved_at=retrieved_at or datetime.now(UTC),
            input_per_million=row.components["input"],
            output_per_million=row.components["output"],
            note=f"Official Foundry pricing page; published unit {row.unit}",
        ))
    if not prices:
        raise ValueError("official-page rows contain no complete input/output token rates")
    return PricingCatalog(
        catalog_name="official-foundry-pages",
        source_url="https://azure.microsoft.com/en-us/pricing/details/ai-foundry-models/",
        retrieved_at=(retrieved_at or datetime.now(UTC)).date(),
        pricing_basis="analysis_date_snapshot",
        prices=prices,
    )


def fetch_official_page(
    provider_route: str,
    *,
    budget: FetchBudget | None = None,
    transport: Transport | None = None,
) -> list[OfficialPageRate]:
    """Fetch and parse one provider page under the bounded official allow-list."""
    url = official_page_url(provider_route)
    page = fetch_text(url, allow_list=OFFICIAL_ALLOW_LIST, budget=budget, transport=transport)
    return parse_official_page(
        page.text(),
        provider_route=provider_route,
        source_url=page.resolved_url,
    )


def sync_official_pages(
    provider_routes: Iterable[str],
    *,
    budget: FetchBudget | None = None,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> tuple[PricingSnapshot, list[OfficialPageRate]]:
    """Fetch selected provider pages and atomically cache one complete snapshot."""
    retrieved_at = now or datetime.now(UTC)
    routes = list(provider_routes)
    if not routes:
        raise ValueError("at least one official provider route is required")
    rates: list[OfficialPageRate] = []
    for route in routes:
        rates.extend(fetch_official_page(route, budget=budget, transport=transport))
    catalog = page_catalog(rates, retrieved_at=retrieved_at)
    digest = content_sha256("\n".join(sorted(rate.content_hash for rate in rates)))
    snapshot = PricingSnapshot(
        source="official_foundry_pages",
        source_url=OFFICIAL_PRICING_ROOT,
        retrieved_at=retrieved_at,
        content_hash=digest,
        pages_read=len(routes),
        rows_read=len(rates),
        catalog=catalog,
        notes=[f"provider-routes={','.join(routes)}"],
    )
    write_snapshot(snapshot, name="official-foundry-pages.json")
    return snapshot, rates


__all__ = [
    "OFFICIAL_ALLOW_LIST",
    "OFFICIAL_PRICING_ROOT",
    "OFFICIAL_ROUTES",
    "OfficialPageRate",
    "official_page_url",
    "fetch_official_page",
    "page_catalog",
    "parse_official_page",
    "sync_official_pages",
]
