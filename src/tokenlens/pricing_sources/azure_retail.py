"""Azure Retail Prices synchronization for Foundry token meters.

Source: ``https://prices.azure.com/api/retail/prices`` filtered to
``serviceName eq 'Foundry Models'``. The API is official, documented, and
machine-readable, which is what lets TokenLens label a synchronized rate
``verified`` instead of guessed.

The crosswalk is deterministic and closed:

* a row is priced only when an explicit model entry in :data:`MODEL_CROSSWALK`
  matches its meter label, **and** every remaining token in that label is a
  token TokenLens recognizes;
* every purchasing dimension (input/output/cached, deployment, context window,
  service tier, inference mode) must resolve unambiguously;
* anything else is quarantined with a reason — never approximated.

Excluded by construction: fine-tuning (``-FT``), batch, priority (``PP``),
flex/provisioned, hosted deployment units, image/audio/media, tool and session
meters, tiered meters, and reservations.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Iterable, Literal, Mapping, Sequence
from urllib.parse import quote

from ..pricing import PriceEntry, PricingCatalog
from .assumptions import (
    DEFAULT_PRICING_ASSUMPTIONS,
    PricingAssumptions,
    canonical_context,
    canonical_deployment,
)
from .cache import AZURE_RETAIL_SNAPSHOT, PricingSnapshot, QuarantinedMeter, content_hash
from .fetch import (
    FetchBudget,
    FetchedPage,
    PageParse,
    Transport,
    UrlAllowList,
    content_sha256,
    fetch_pages,
)

__all__ = [
    "MODEL_CROSSWALK",
    "RETAIL_ALLOW_LIST",
    "RETAIL_API_VERSION",
    "RETAIL_PRICES_URL",
    "RETAIL_SERVICE_NAME",
    "CrosswalkResult",
    "ModelCrosswalk",
    "ParsedMeter",
    "RetailRow",
    "build_initial_url",
    "crosswalk_rows",
    "parse_meter",
    "retail_snapshot",
    "rows_from_payload",
    "sync_azure_retail",
]

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
RETAIL_API_VERSION = "2023-01-01-preview"
RETAIL_SERVICE_NAME = "Foundry Models"

#: Only this exact host and path are ever requested, including continuations.
RETAIL_ALLOW_LIST = UrlAllowList(
    hosts=frozenset({"prices.azure.com"}),
    paths=frozenset({"/api/retail/prices"}),
)

Dimension = Literal["input", "output", "cached_input", "cache_write"]
Deployment = Literal["global", "data_zone", "regional"]
ContextWindow = Literal["short", "long"]


# --------------------------------------------------------------------------
# Meter label vocabulary
# --------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r"[^0-9a-z.]+")


def normalize_tokens(value: str) -> tuple[str, ...]:
    """Lowercase a meter label into comparable tokens.

    ``"5.6 luna ShortCo Cd Inp Std Gl 1M Tokens"`` becomes
    ``("5.6", "luna", "shortco", "cd", "inp", "std", "gl", "1m", "tokens")``.
    Hyphenated compounds such as ``In-FT`` split, so a fine-tuning marker can
    never hide inside another word.
    """
    lowered = value.casefold()
    return tuple(token for token in _TOKEN_SPLIT.split(lowered) if token)


#: Any of these anywhere in meterName, skuName, or armSkuName disqualifies the
#: row. The value is the reason recorded against the excluded meter.
EXCLUDED_TOKENS: dict[str, str] = {
    "ft": "fine-tuning",
    "finetune": "fine-tuning",
    "finetuned": "fine-tuning",
    "finetuning": "fine-tuning",
    "ftune": "fine-tuning",
    "batch": "batch",
    "bat": "batch",
    "btch": "batch",
    "pp": "priority-processing",
    "priority": "priority-processing",
    "prov": "provisioned",
    "provisioned": "provisioned",
    "ptu": "provisioned",
    "reserved": "reservation",
    "reservation": "reservation",
    "fl": "flex",
    "flex": "flex",
    "image": "media",
    "images": "media",
    "audio": "media",
    "speech": "media",
    "video": "media",
    "vision": "media",
    "media": "media",
    "tool": "tool",
    "tools": "tool",
    "session": "session",
    "sessions": "session",
    "realtime": "session",
    "hosting": "hosted-deployment-unit",
    "unit": "hosted-deployment-unit",
    "units": "hosted-deployment-unit",
    "hour": "hosted-deployment-unit",
    "page": "non-token-meter",
    "pages": "non-token-meter",
    "character": "non-token-meter",
    "characters": "non-token-meter",
    "minute": "non-token-meter",
    "minutes": "non-token-meter",
}

_INPUT_TOKENS = frozenset({"inp", "in", "input", "inputs"})
_OUTPUT_TOKENS = frozenset({"opt", "out", "outp", "output", "outputs"})
_CACHE_TOKENS = frozenset({"cd", "cache", "cached", "caching"})
_WRITE_TOKENS = frozenset({"wr", "write", "writes"})
_GLOBAL_TOKENS = frozenset({"gl", "gbl", "glbl", "global"})
_DATA_ZONE_TOKENS = frozenset({"dz", "dzone", "datazone", "dzn"})
_REGIONAL_TOKENS = frozenset({"reg", "regnl", "regional"})
_SHORT_TOKENS = frozenset({"shco", "shortco", "short", "shortcontext"})
_LONG_TOKENS = frozenset({"loco", "longco", "long", "longcontext"})
_STANDARD_TOKENS = frozenset({"std", "standard"})
#: Tokens that carry no pricing dimension and may appear in a priced label.
_NOISE_TOKENS = frozenset({"tokens", "token", "model", "models", "1m", "1k", "10k", "100k", "1"})

_KNOWN_TOKENS = (
    _INPUT_TOKENS
    | _OUTPUT_TOKENS
    | _CACHE_TOKENS
    | _WRITE_TOKENS
    | _GLOBAL_TOKENS
    | _DATA_ZONE_TOKENS
    | _REGIONAL_TOKENS
    | _SHORT_TOKENS
    | _LONG_TOKENS
    | _STANDARD_TOKENS
    | _NOISE_TOKENS
    | frozenset(EXCLUDED_TOKENS)
)


# --------------------------------------------------------------------------
# Model crosswalk
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelCrosswalk:
    """One explicit deployment-model ↔ retail-meter-label mapping.

    ``meter_labels`` are the exact label prefixes Azure publishes for the model.
    They are enumerated rather than derived, because deriving them would be a
    guess the moment Microsoft abbreviates a name differently.
    """

    model: str
    publisher: str
    aliases: tuple[str, ...] = ()
    product_names: frozenset[str] = frozenset()
    meter_labels: tuple[str, ...] = ()
    #: The documented prompt-token boundary at which the published long-context
    #: meter takes over from the short-context meter. ``None`` means the model
    #: publishes no context split, or the boundary is not documented — in which
    #: case a long-context meter is quarantined rather than guessed at.
    long_context_threshold_tokens: int | None = None

    def label_tokens(self) -> tuple[tuple[str, ...], ...]:
        return tuple(normalize_tokens(label) for label in self.meter_labels)

    def match(self, tokens: Sequence[str]) -> tuple[str, ...] | None:
        """Return the remaining tokens when a meter label matches this model."""
        best: tuple[str, ...] | None = None
        best_len = -1
        for label in self.label_tokens():
            size = len(label)
            if size and tuple(tokens[:size]) == label and size > best_len:
                best, best_len = tuple(tokens[size:]), size
        return best


MODEL_CROSSWALK: tuple[ModelCrosswalk, ...] = (
    ModelCrosswalk(
        model="gpt-5.6-luna",
        publisher="microsoft",
        aliases=("gpt-5.6 luna", "gpt5.6-luna", "gpt-56-luna"),
        product_names=frozenset({"Azure OpenAI GPT5"}),
        meter_labels=("5.6 luna", "56 luna", "56luna", "gpt 5.6 luna"),
        # Azure OpenAI GPT-5 series bills the long-context meter from 128K
        # prompt tokens upward; below that the short-context meter applies.
        long_context_threshold_tokens=128_000,
    ),
    ModelCrosswalk(
        model="ministral-3b",
        publisher="mistral",
        aliases=("ministral 3b", "ministral-3b-2410", "mistral-ministral-3b"),
        product_names=frozenset({"Azure Mistral Models"}),
        meter_labels=("ministral 3b", "ministral3b", "mnstrl 3b", "mnstrl3b"),
        # Mistral publishes no short/long context split for this model.
        long_context_threshold_tokens=None,
    ),
)


def crosswalk_for(model: str) -> ModelCrosswalk | None:
    from ..pricing import canonical_model_name

    wanted = canonical_model_name(model)
    for entry in MODEL_CROSSWALK:
        if canonical_model_name(entry.model) == wanted:
            return entry
        if wanted in {canonical_model_name(alias) for alias in entry.aliases}:
            return entry
    return None


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetailRow:
    """One Azure Retail Prices item, reduced to the fields the parser uses."""

    meter_id: str
    meter_name: str
    sku_name: str
    arm_sku_name: str
    product_name: str
    unit_of_measure: str
    retail_price: float
    arm_region_name: str
    currency_code: str
    effective_start_date: str
    type: str = "Consumption"
    tier_minimum_units: float = 0.0
    reservation_term: str | None = None
    service_name: str = RETAIL_SERVICE_NAME

    @classmethod
    def from_item(cls, item: Mapping[str, object]) -> "RetailRow | None":
        try:
            return cls(
                meter_id=str(item.get("meterId") or ""),
                meter_name=str(item.get("meterName") or ""),
                sku_name=str(item.get("skuName") or ""),
                arm_sku_name=str(item.get("armSkuName") or ""),
                product_name=str(item.get("productName") or ""),
                unit_of_measure=str(item.get("unitOfMeasure") or ""),
                retail_price=float(item.get("retailPrice")),  # type: ignore[arg-type]
                arm_region_name=str(item.get("armRegionName") or ""),
                currency_code=str(item.get("currencyCode") or "USD").upper(),
                effective_start_date=str(item.get("effectiveStartDate") or ""),
                type=str(item.get("type") or "Consumption"),
                tier_minimum_units=float(item.get("tierMinimumUnits") or 0.0),
                reservation_term=(
                    str(item["reservationTerm"]) if item.get("reservationTerm") else None
                ),
                service_name=str(item.get("serviceName") or RETAIL_SERVICE_NAME),
            )
        except (TypeError, ValueError):
            return None

    def effective_date(self) -> date | None:
        raw = self.effective_start_date.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(raw).date()
        except ValueError:
            return None


def rows_from_payload(payload: Mapping[str, object]) -> list[RetailRow]:
    items = payload.get("Items")
    if not isinstance(items, list):
        return []
    parsed = [RetailRow.from_item(item) for item in items if isinstance(item, Mapping)]
    return [row for row in parsed if row is not None]


# --------------------------------------------------------------------------
# Deterministic meter parsing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedMeter:
    """A fully resolved token meter with every dimension made explicit."""

    row: RetailRow
    model: str
    publisher: str
    dimension: Dimension
    deployment: Deployment
    context: ContextWindow
    service_tier: str
    inference: str
    per_million: float
    assumed: tuple[str, ...] = ()


@dataclass(frozen=True)
class MeterRejection:
    """A row that was not priced, with a machine-readable reason."""

    row: RetailRow
    reason: str
    detail: str | None = None
    quarantine: bool = False
    #: Set when the row *did* match a crosswalk model but was still rejected,
    #: so the caller can explain why a known model ended up unpriced.
    model: str | None = None

    def to_record(self) -> QuarantinedMeter:
        return QuarantinedMeter(
            reason=self.reason,
            meter_id=self.row.meter_id or None,
            meter_name=self.row.meter_name or None,
            sku_name=self.row.sku_name or None,
            product_name=self.row.product_name or None,
            detail=self.detail,
        )


_UNIT_PATTERN = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([km]?)\s*(?:tokens?)?\s*$", re.IGNORECASE)
_UNIT_SCALE = {"": 1.0, "k": 1_000.0, "m": 1_000_000.0}


def units_per_measure(unit_of_measure: str) -> float | None:
    """``"1K"`` → 1000, ``"1M"`` → 1000000. Anything else is unsupported.

    A composite unit such as ``1/Hour`` is rejected outright: it measures
    hosted capacity, not tokens, and must never be read as "one token".
    """
    match = _UNIT_PATTERN.match(unit_of_measure or "")
    if not match:
        return None
    magnitude = float(match.group(1))
    scale = _UNIT_SCALE.get(match.group(2).casefold())
    if scale is None or magnitude <= 0:
        return None
    return magnitude * scale


def to_per_million(retail_price: float, unit_of_measure: str) -> float | None:
    units = units_per_measure(unit_of_measure)
    if units is None:
        return None
    return retail_price * 1_000_000.0 / units


def _single(tokens: Iterable[str], vocabulary: frozenset[str]) -> bool:
    return any(token in vocabulary for token in tokens)


def parse_meter(
    row: RetailRow,
    *,
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS,
) -> ParsedMeter | MeterRejection:
    """Resolve one retail row into an exact priced dimension, or reject it."""
    if row.service_name and row.service_name.casefold() != RETAIL_SERVICE_NAME.casefold():
        return MeterRejection(row, "not-foundry-service")
    if row.reservation_term:
        return MeterRejection(row, "reservation-meter")
    if row.type.casefold() != "consumption":
        return MeterRejection(row, "non-consumption-meter")
    if row.tier_minimum_units:
        return MeterRejection(row, "tiered-meter", detail=f"tierMinimumUnits={row.tier_minimum_units}")

    meter_tokens = normalize_tokens(row.meter_name)
    if not meter_tokens or meter_tokens[-1] not in {"tokens", "token"}:
        return MeterRejection(row, "non-token-meter")

    crosswalk: ModelCrosswalk | None = None
    remainder: tuple[str, ...] | None = None
    for candidate in MODEL_CROSSWALK:
        if candidate.product_names and row.product_name not in candidate.product_names:
            continue
        matched = candidate.match(meter_tokens)
        if matched is not None:
            crosswalk, remainder = candidate, matched
            break
    if crosswalk is None or remainder is None:
        return MeterRejection(row, "model-not-in-crosswalk")

    # Exclusion markers are scanned across every published label, so a marker
    # that only appears in armSkuName (for example ``Mistral model 1-IN-FT``)
    # still disqualifies the row.
    for label in (row.meter_name, row.sku_name, row.arm_sku_name):
        for token in normalize_tokens(label):
            reason = EXCLUDED_TOKENS.get(token)
            if reason is not None:
                return MeterRejection(
                    row, f"excluded-{reason}", detail=f"token={token}", model=crosswalk.model
                )

    unknown = [token for token in remainder if token not in _KNOWN_TOKENS]
    if unknown:
        return MeterRejection(
            row,
            "unrecognized-meter-token",
            detail="tokens=" + ",".join(sorted(set(unknown))),
            quarantine=True,
            model=crosswalk.model,
        )

    has_cache = _single(remainder, _CACHE_TOKENS)
    has_write = _single(remainder, _WRITE_TOKENS)
    has_input = _single(remainder, _INPUT_TOKENS)
    has_output = _single(remainder, _OUTPUT_TOKENS)
    if has_cache and has_write:
        dimension: Dimension = "cache_write"
    elif has_cache and has_input:
        dimension = "cached_input"
    elif has_output and not has_input:
        dimension = "output"
    elif has_input and not has_output:
        dimension = "input"
    else:
        return MeterRejection(row, "dimension-ambiguous", quarantine=True)

    assumed: list[str] = []
    deployments = {
        "global": _single(remainder, _GLOBAL_TOKENS),
        "data_zone": _single(remainder, _DATA_ZONE_TOKENS),
        "regional": _single(remainder, _REGIONAL_TOKENS),
    }
    stated_deployments = [key for key, present in deployments.items() if present]
    if len(stated_deployments) > 1:
        return MeterRejection(
            row, "deployment-ambiguous", detail="+".join(stated_deployments), quarantine=True
        )
    if stated_deployments:
        deployment: Deployment = stated_deployments[0]  # type: ignore[assignment]
    else:
        deployment = assumptions.deployment
        assumed.append(f"deployment={deployment}")

    short = _single(remainder, _SHORT_TOKENS)
    long_context = _single(remainder, _LONG_TOKENS)
    if short and long_context:
        return MeterRejection(row, "context-ambiguous", quarantine=True)
    if short:
        context: ContextWindow = "short"
    elif long_context:
        context = "long"
    else:
        # The user-selected default is applied explicitly and labelled, rather
        # than the row being silently treated as short context.
        context = assumptions.context
        assumed.append(f"context={context}")

    if not _single(remainder, _STANDARD_TOKENS):
        assumed.append(f"service_tier={assumptions.service_tier}")
    assumed.append(f"purchase_model={assumptions.purchase_model}")

    per_million = to_per_million(row.retail_price, row.unit_of_measure)
    if per_million is None:
        return MeterRejection(
            row, "unit-of-measure-unsupported", detail=row.unit_of_measure, quarantine=True
        )

    return ParsedMeter(
        row=row,
        model=crosswalk.model,
        publisher=crosswalk.publisher,
        dimension=dimension,
        deployment=deployment,
        context=context,
        service_tier=assumptions.service_tier,
        inference=assumptions.inference,
        per_million=per_million,
        assumed=tuple(assumed),
    )


# --------------------------------------------------------------------------
# Crosswalk into catalog entries
# --------------------------------------------------------------------------


@dataclass
class CrosswalkResult:
    entries: list[PriceEntry] = field(default_factory=list)
    quarantined: list[QuarantinedMeter] = field(default_factory=list)
    assumed_dimensions: list[str] = field(default_factory=list)
    rows_read: int = 0
    feed_complete: bool = True
    notes: list[str] = field(default_factory=list)


_DIMENSION_ORDER: tuple[Dimension, ...] = ("input", "output", "cached_input", "cache_write")


def crosswalk_rows(
    rows: Sequence[RetailRow],
    *,
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS,
    deployments: Sequence[str] | None = None,
    contexts: Sequence[str] | None = None,
    account_region: str | None = None,
    currency: str = "USD",
    as_of: date | None = None,
    retrieved_at: datetime | None = None,
    source_hash: str | None = None,
    feed_complete: bool = True,
) -> CrosswalkResult:
    """Turn retail rows into exact catalog entries, quarantining the ambiguous.

    ``deployments`` defaults to the assumed deployment dimension, so
    data-zone and regional meters are excluded under the global default. A
    caller that *knows* a deployment's exact mode passes it explicitly, and the
    observed mode then wins over the default. Every requested value goes
    through the shared canonicalizer, so an unsupported one is rejected rather
    than silently matching nothing.

    ``feed_complete`` must be ``False`` when the source was truncated. A
    truncated feed can never support the claim that a meter is *absent*.
    """
    wanted_deployments = {
        canonical_deployment(item) for item in (deployments or [assumptions.deployment])
    }
    wanted_contexts = {canonical_context(item) for item in (contexts or [assumptions.context])}
    today = as_of or datetime.now(UTC).date()
    result = CrosswalkResult(rows_read=len(rows), feed_complete=feed_complete)

    selected: dict[tuple[str, str, str, Dimension], list[ParsedMeter]] = {}
    excluded_modes: dict[str, set[str]] = {}
    other_dimensions: dict[str, set[str]] = {}
    for row in rows:
        if row.currency_code.upper() != currency.upper():
            continue
        parsed = parse_meter(row, assumptions=assumptions)
        if isinstance(parsed, MeterRejection):
            if parsed.quarantine:
                result.quarantined.append(parsed.to_record())
            if parsed.model and parsed.reason.startswith("excluded-"):
                excluded_modes.setdefault(parsed.model, set()).add(
                    parsed.reason.removeprefix("excluded-")
                )
            continue
        if parsed.deployment not in wanted_deployments or parsed.context not in wanted_contexts:
            other_dimensions.setdefault(parsed.model, set()).add(
                f"{parsed.deployment}/{parsed.context}-context"
            )
            continue
        effective = row.effective_date()
        if effective is None:
            result.quarantined.append(
                MeterRejection(row, "effective-date-unparsable", detail=row.effective_start_date).to_record()
            )
            continue
        if effective > today:
            continue
        key = (parsed.model, parsed.deployment, parsed.context, parsed.dimension)
        selected.setdefault(key, []).append(parsed)

    resolved: dict[tuple[str, str, str], dict[Dimension, ParsedMeter]] = {}
    for (model, deployment, context, dimension), candidates in selected.items():
        winner = _resolve_candidates(candidates, account_region=account_region)
        if isinstance(winner, MeterRejection):
            result.quarantined.append(winner.to_record())
            continue
        resolved.setdefault((model, deployment, context), {})[dimension] = winner

    assumed_all: set[str] = set()
    for (model, deployment, context), dimensions in sorted(resolved.items()):
        if "input" not in dimensions or "output" not in dimensions:
            missing = [item for item in ("input", "output") if item not in dimensions]
            result.quarantined.append(
                QuarantinedMeter(
                    reason="incomplete-meter-set",
                    meter_name=f"{model} · {deployment} · {context} context",
                    detail="missing=" + ",".join(missing),
                )
            )
            continue
        crosswalk = crosswalk_for(model)
        threshold = crosswalk.long_context_threshold_tokens if crosswalk else None
        if context == "long" and threshold is None:
            # Publishing a long-context rate with no documented boundary would
            # make it indistinguishable from the short-context rate at
            # resolution time, so it is quarantined instead of guessed.
            result.quarantined.append(
                QuarantinedMeter(
                    reason="context-boundary-undocumented",
                    meter_name=f"{model} · {deployment} · long context",
                    detail="no documented short/long prompt-token boundary for this model",
                )
            )
            result.notes.append(
                f"{model}: the long-context meter is not published because TokenLens has no "
                "documented prompt-token boundary to separate it from the short-context meter."
            )
            continue
        context_min = 0 if context == "short" and threshold is not None else None
        context_max = threshold - 1 if context == "short" and threshold is not None else None
        if context == "long":
            context_min, context_max = threshold, None
        assumed = sorted({item for meter in dimensions.values() for item in meter.assumed})
        assumed_all.update(assumed)
        effective_dates = [
            meter.row.effective_date() for meter in dimensions.values() if meter.row.effective_date()
        ]
        regions = sorted({meter.row.arm_region_name for meter in dimensions.values() if meter.row.arm_region_name})
        result.entries.append(
            PriceEntry(
                provider="azure_foundry",
                publisher=dimensions["input"].publisher,
                model=model,
                aliases=list(crosswalk.aliases) if crosswalk else [],
                service_tier=assumptions.service_tier,
                region=deployment,
                effective_from=max(effective_dates) if effective_dates else today,
                billing_basis="token_rate",
                confidence="verified",
                source_url=RETAIL_PRICES_URL,
                source_rank=10,
                note=(
                    f"Azure Retail Prices · {deployment} · {context} context"
                    + (
                        f" ({context_min:,}–{context_max:,} prompt tokens)"
                        if context_min is not None and context_max is not None
                        else f" (from {context_min:,} prompt tokens)"
                        if context_min is not None
                        else ""
                    )
                    + f" · {assumptions.service_tier} · {assumptions.inference} inference"
                    + (f" · regions={','.join(regions)}" if regions else "")
                ),
                context_window=context,  # type: ignore[arg-type]
                context_length_min=context_min,
                context_length_max=context_max,
                assumed_dimensions=assumed,
                meter_ids={
                    dimension: meter.row.meter_id
                    for dimension, meter in sorted(dimensions.items(), key=lambda item: _DIMENSION_ORDER.index(item[0]))
                    if meter.row.meter_id
                },
                content_hash=source_hash,
                retrieved_at=retrieved_at,
                input_per_million=dimensions["input"].per_million,
                cached_input_per_million=(
                    dimensions["cached_input"].per_million if "cached_input" in dimensions else None
                ),
                cache_write_per_million=(
                    dimensions["cache_write"].per_million if "cache_write" in dimensions else 0.0
                ),
                output_per_million=dimensions["output"].per_million,
            )
        )
    result.assumed_dimensions = sorted(assumed_all)
    priced_models = {entry.model for entry in result.entries}
    # A model TokenLens knows about but could not price is explained, not
    # silently dropped: "the retail feed only publishes fine-tuning meters for
    # this model" is actionable, an empty result is not.
    for model in sorted(set(excluded_modes) | set(other_dimensions)):
        if model in priced_models:
            continue
        modes = sorted(excluded_modes.get(model, set()))
        others = sorted(other_dimensions.get(model, set()))
        requested = (
            f"requested={','.join(sorted(wanted_deployments))}/"
            f"{','.join(sorted(wanted_contexts))}-context"
        )
        if not feed_complete:
            # "We stopped reading" is not evidence that a meter does not exist.
            result.quarantined.append(
                QuarantinedMeter(
                    reason="feed-truncated-model-unresolved",
                    meter_name=model,
                    detail="; ".join(part for part in ("source-feed=truncated", requested) if part),
                )
            )
            result.notes.append(
                f"{model}: the retail feed was truncated before it could be read to completion, "
                "so TokenLens cannot tell a missing meter from an unread one. Nothing is published "
                "for this model."
            )
            continue
        result.quarantined.append(
            QuarantinedMeter(
                reason=(
                    "no-normal-inference-meter-published"
                    if modes and not others
                    else "no-meter-for-requested-dimensions"
                ),
                meter_name=model,
                detail="; ".join(
                    part
                    for part in (
                        f"published-only-as={','.join(modes)}" if modes else "",
                        f"published-dimensions={','.join(others)}" if others else "",
                        requested,
                    )
                    if part
                ),
            )
        )
        result.notes.append(
            f"{model}: no retail meter matches normal inference at "
            f"{','.join(sorted(wanted_deployments))} / {','.join(sorted(wanted_contexts))} context. "
            "Cost stays unresolved; record a contracted rate with `tokenlens-azure pricing set-rate` "
            "if you have one."
        )
    return result


def _resolve_candidates(
    candidates: Sequence[ParsedMeter], *, account_region: str | None
) -> ParsedMeter | MeterRejection:
    """Pick one meter for a dimension, or reject when regions disagree.

    A global meter is normally published identically in every region. Those
    duplicates are collapsed only after verifying the rates are byte-identical;
    if they differ, the account's own region decides, and without one the
    dimension is quarantined rather than averaged or arbitrarily chosen.
    """
    if account_region:
        regional = [
            item for item in candidates if item.row.arm_region_name.casefold() == account_region.casefold()
        ]
        if regional:
            candidates = regional
    latest = max(
        (item.row.effective_date() for item in candidates if item.row.effective_date()),
        default=None,
    )
    if latest is not None:
        candidates = [item for item in candidates if item.row.effective_date() == latest]
    rates = {round(item.per_million, 10) for item in candidates}
    if len(rates) > 1:
        return MeterRejection(
            candidates[0].row,
            "region-rate-conflict",
            detail="rates=" + ",".join(f"{rate:g}" for rate in sorted(rates)),
            quarantine=True,
        )
    return candidates[0]


# --------------------------------------------------------------------------
# Synchronization
# --------------------------------------------------------------------------


def crosswalk_product_names() -> tuple[str, ...]:
    """Every retail product family the crosswalk can actually price."""
    return tuple(sorted({name for entry in MODEL_CROSSWALK for name in entry.product_names}))


def build_initial_url(
    *,
    currency: str = "USD",
    region: str | None = None,
    product_names: Sequence[str] | None = None,
    api_version: str = RETAIL_API_VERSION,
) -> str:
    """The one URL the sync starts from. Continuations come from the API.

    The query is bounded server-side by the product families the crosswalk can
    price, and optionally by one ARM region, so an ordinary synchronization
    finishes well inside the page and item budget instead of walking the entire
    Foundry Models price list.
    """
    names = tuple(product_names) if product_names is not None else crosswalk_product_names()
    filter_expression = f"serviceName eq '{RETAIL_SERVICE_NAME}'"
    if region:
        filter_expression += f" and armRegionName eq '{region}'"
    if names:
        clause = " or ".join(f"productName eq '{name}'" for name in names)
        filter_expression += f" and ({clause})"
    query = (
        f"api-version={quote(api_version, safe='')}"
        f"&currencyCode={quote(currency.upper(), safe='')}"
        f"&$filter={quote(filter_expression, safe='')}"
    )
    return f"{RETAIL_PRICES_URL}?{query}"


def parse_retail_page(page: FetchedPage) -> PageParse:
    """Decode one Retail Prices page exactly once."""
    try:
        payload = json.loads(page.text())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("The Azure Retail Prices response was not valid JSON.") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("The Azure Retail Prices response was not a JSON object.")
    link = payload.get("NextPageLink")
    items = payload.get("Items")
    return PageParse(
        payload=payload,
        next_url=str(link) if isinstance(link, str) and link else None,
        item_count=len(items) if isinstance(items, list) else 0,
    )


def retail_snapshot(
    *,
    currency: str = "USD",
    region: str | None = None,
    product_names: Sequence[str] | None = None,
    assumptions: PricingAssumptions = DEFAULT_PRICING_ASSUMPTIONS,
    deployments: Sequence[str] | None = None,
    contexts: Sequence[str] | None = None,
    account_region: str | None = None,
    budget: FetchBudget | None = None,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> PricingSnapshot:
    """Fetch, parse, and package one Azure Retail Prices snapshot.

    A truncated feed is never packaged: :func:`fetch_pages` raises
    ``BudgetExceededError`` while a continuation is still pending, the caller
    reports the failure, and the previous complete cache stays untouched.
    """
    budget = budget or FetchBudget()
    retrieved_at = now or datetime.now(UTC)
    first_url = build_initial_url(currency=currency, region=region, product_names=product_names)

    rows: list[RetailRow] = []
    digest_parts: list[str] = []
    pages_read = 0
    resolved_url = first_url
    for envelope in fetch_pages(
        first_url,
        allow_list=RETAIL_ALLOW_LIST,
        parse=parse_retail_page,
        budget=budget,
        transport=transport,
    ):
        if envelope.index == 0:
            resolved_url = envelope.page.resolved_url
        pages_read += 1
        digest_parts.append(envelope.content_hash)
        rows.extend(rows_from_payload(envelope.payload))
    source_hash = content_sha256("|".join(digest_parts))

    crosswalked = crosswalk_rows(
        rows,
        assumptions=assumptions,
        deployments=deployments,
        contexts=contexts,
        account_region=account_region,
        currency=currency,
        as_of=retrieved_at.date(),
        retrieved_at=retrieved_at,
        source_hash=source_hash,
        feed_complete=True,
    )
    catalog = PricingCatalog(
        currency=currency.upper(),
        catalog_name="azure-retail-prices-foundry",
        source_url=RETAIL_PRICES_URL,
        retrieved_at=retrieved_at.date(),
        pricing_basis="effective_period",
        assumptions=assumptions.to_metadata()["dimensions"],  # type: ignore[arg-type]
        prices=crosswalked.entries,
    )
    return PricingSnapshot(
        source="azure_retail_prices",
        source_url=resolved_url,
        api_version=RETAIL_API_VERSION,
        retrieved_at=retrieved_at,
        content_hash=source_hash,
        pages_read=pages_read,
        rows_read=crosswalked.rows_read,
        currency=currency.upper(),
        feed_complete=True,
        assumptions=assumptions,
        assumed_dimensions=crosswalked.assumed_dimensions,
        catalog=catalog,
        quarantined=crosswalked.quarantined,
        notes=[
            f"Filtered to serviceName eq '{RETAIL_SERVICE_NAME}'"
            + (
                f" and productName in ({', '.join(product_names or crosswalk_product_names())})"
                if (product_names or crosswalk_product_names())
                else ""
            )
            + (f" and armRegionName eq '{region}'" if region else "")
            + ".",
            "Fine-tuning, batch, priority, flex, provisioned, hosted-unit, media, tool, and "
            "session meters are excluded by construction.",
            "The feed was read to completion; every page budget was respected.",
            *crosswalked.notes,
        ],
    )


def sync_azure_retail(**kwargs: object) -> tuple[PricingSnapshot, str]:
    """Fetch a snapshot and report the cache file name it belongs in."""
    snapshot = retail_snapshot(**kwargs)  # type: ignore[arg-type]
    return snapshot, AZURE_RETAIL_SNAPSHOT
