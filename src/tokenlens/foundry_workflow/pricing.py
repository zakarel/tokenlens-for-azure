"""Pricing readiness, customer overrides, and the explicitly deferred sync.

Pricing is never guessed from a related model or family. Resolution order is:

1. observed per-call cost;
2. customer catalog;
3. synchronized verified public catalog;
4. packaged verified catalog;
5. unresolved.

``tokenlens-azure pricing sync`` is **deferred**. Publishing a rate that was
parsed from an undocumented, unstable source would be indistinguishable from a
guess, so the command reports the deferral and points at the customer-rate
workflow instead of inventing numbers. See ``docs/pricing.md``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Iterable, Literal, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..pricing import PriceEntry, PricingCatalog, canonical_model_name, infer_publisher
from ..reference import load_bundled_reference_catalog
from .configuration import customer_catalog_path, pricing_cache_dir, safe_display_path, _atomic_write
from .models import DeploymentRecord, WorkflowError

__all__ = [
    "PRICING_SYNC_DEFERRED_REASON",
    "CustomerRate",
    "PricingReadiness",
    "catalog_status",
    "load_customer_catalog",
    "pricing_readiness",
    "synchronized_catalog_path",
    "write_customer_rate",
]

PRICING_SYNC_DEFERRED_REASON = (
    "Public pricing synchronization is deferred. TokenLens will only publish a rate it can "
    "attribute to a documented, machine-readable source with a deterministic parser, an "
    "effective date, a retrieval timestamp, and a content hash. Until that source is wired in, "
    "synchronizing would be indistinguishable from guessing a rate."
)

PricingState = Literal[
    "exact_public_rate",
    "customer_override",
    "rate_unavailable",
    "identity_unresolved",
    "currency_mismatch",
]

_STATE_LABELS: dict[str, str] = {
    "exact_public_rate": "Exact public rate",
    "customer_override": "Customer override",
    "rate_unavailable": "Model identified, rate unavailable",
    "identity_unresolved": "Identity unresolved",
    "currency_mismatch": "Currency mismatch",
}


class PricingReadiness(BaseModel):
    """Identity and price coverage for one deployment, as separate states."""

    model_config = ConfigDict(extra="forbid")

    deployment: str
    model: str
    model_version: str | None = None
    deployment_mode: str = "unknown"
    state: PricingState
    catalog_name: str | None = None
    currency: str = "USD"
    billing_basis: str | None = None
    suggested_override_key: str | None = None
    catalogs_consulted: list[str] = Field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.state in {"exact_public_rate", "customer_override"}

    @property
    def symbol(self) -> str:
        return "OK" if self.resolved else "!"

    @property
    def label(self) -> str:
        return _STATE_LABELS[self.state]


class CustomerRate(BaseModel):
    """One explicitly supplied contracted rate. Never inferred, never scaled."""

    model_config = ConfigDict(extra="forbid")

    model: str
    provider: str = "azure_foundry"
    currency: str = "USD"
    input_per_million: float = Field(ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float = Field(ge=0)
    deployment_mode: str = "unknown"
    effective_from: date
    billing_basis: Literal[
        "token_rate", "claude_ccu_equivalent", "marketplace_partner_token_rate"
    ] = "token_rate"
    service_tier: str = "standard"
    note: str | None = None

    def summary_lines(self) -> list[str]:
        lines = [
            f"model={self.model}",
            f"currency={self.currency}",
            f"input-per-million={self.input_per_million}",
            f"cached-input-per-million={self.cached_input_per_million if self.cached_input_per_million is not None else 'not supplied'}",
            f"output-per-million={self.output_per_million}",
            f"deployment-mode={self.deployment_mode}",
            f"effective-from={self.effective_from.isoformat()}",
            f"billing-basis={self.billing_basis}",
        ]
        if self.note:
            lines.append(f"note={self.note}")
        return lines

    def to_entry(self) -> PriceEntry:
        return PriceEntry(
            provider=self.provider,
            publisher=infer_publisher(self.model) or "",
            model=canonical_model_name(self.model),
            service_tier=self.service_tier,
            effective_from=self.effective_from,
            billing_basis=self.billing_basis,
            confidence="customer_override",
            note=self.note or f"Customer-provided rate ({self.deployment_mode} deployment mode)",
            input_per_million=self.input_per_million,
            cached_input_per_million=self.cached_input_per_million,
            output_per_million=self.output_per_million,
        )


def synchronized_catalog_path() -> Path:
    """Where a verified public snapshot would be cached once sync ships."""
    return pricing_cache_dir() / "public-snapshot.yml"


def load_customer_catalog(path: Path | str | None = None) -> PricingCatalog | None:
    """Load the user-local customer catalog if one exists."""
    target = Path(path) if path else customer_catalog_path()
    if not target.is_file():
        return None
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise WorkflowError(f"{safe_display_path(target)} must contain a YAML mapping.")
    try:
        return PricingCatalog.model_validate(payload)
    except ValueError as exc:
        raise WorkflowError(f"The customer pricing catalog is invalid: {exc}") from exc


def write_customer_rate(rate: CustomerRate, *, path: Path | str | None = None) -> Path:
    """Append or replace one customer rate in the user-local catalog.

    The catalog lives outside the repository, is written with user-only
    permissions, and contains no credential.
    """
    target = Path(path) if path else customer_catalog_path()
    existing = load_customer_catalog(target)
    catalog = existing or PricingCatalog(
        currency=rate.currency.upper(),
        catalog_name="customer-overrides",
        pricing_basis="effective_period",
    )
    if catalog.currency.upper() != rate.currency.upper():
        raise WorkflowError(
            f"The customer catalog is denominated in {catalog.currency.upper()}. TokenLens never "
            f"converts currencies, so a {rate.currency.upper()} rate cannot be added to it."
        )
    entry = rate.to_entry()
    catalog.prices = [
        price
        for price in catalog.prices
        if not (
            canonical_model_name(price.model) == entry.model
            and price.service_tier == entry.service_tier
            and price.provider == entry.provider
        )
    ]
    catalog.prices.append(entry)
    catalog.retrieved_at = datetime.now(UTC).date()
    payload = catalog.model_dump(mode="json", exclude_none=True)
    _atomic_write(target, yaml.safe_dump(payload, sort_keys=False), mode=0o600)
    return target


def _match(catalog: PricingCatalog | None, model: str, *, service_tier: str = "standard") -> PriceEntry | None:
    if catalog is None or not model:
        return None
    canonical = canonical_model_name(model)
    for entry in catalog.prices:
        if entry.model_match_rank(canonical) is None:
            continue
        if entry.service_tier.casefold() != service_tier.casefold():
            continue
        return entry
    return None


def pricing_readiness(
    deployments: Sequence[DeploymentRecord],
    *,
    customer_catalog: PricingCatalog | None = None,
    reference_catalog: PricingCatalog | None = None,
    reporting_currency: str = "USD",
) -> list[PricingReadiness]:
    """Report exact pricing state per deployment before any collection runs.

    Identity and price coverage are separate states, so a resolved model with no
    published rate is never reported as an identity failure and vice versa.
    """
    if reference_catalog is None:
        reference_catalog = load_bundled_reference_catalog()
    consulted = [
        catalog.catalog_name for catalog in (customer_catalog, reference_catalog) if catalog is not None
    ]
    results: list[PricingReadiness] = []
    for deployment in deployments:
        model = deployment.model or ""
        if not model or model.strip().casefold() in {"unknown", "none"}:
            results.append(
                PricingReadiness(
                    deployment=deployment.name,
                    model=model or "unknown",
                    model_version=deployment.model_version,
                    deployment_mode=deployment.deployment_mode,
                    state="identity_unresolved",
                    catalogs_consulted=consulted,
                    currency=reporting_currency,
                )
            )
            continue
        customer_entry = _match(customer_catalog, model)
        reference_entry = _match(reference_catalog, model)
        entry = customer_entry or reference_entry
        catalog = customer_catalog if customer_entry is not None else reference_catalog
        if entry is None:
            results.append(
                PricingReadiness(
                    deployment=deployment.name,
                    model=model,
                    model_version=deployment.model_version,
                    deployment_mode=deployment.deployment_mode,
                    state="rate_unavailable",
                    catalogs_consulted=consulted,
                    currency=reporting_currency,
                    suggested_override_key=canonical_model_name(model),
                )
            )
            continue
        currency = (catalog.currency if catalog else reporting_currency).upper()
        if currency != reporting_currency.upper():
            results.append(
                PricingReadiness(
                    deployment=deployment.name,
                    model=model,
                    model_version=deployment.model_version,
                    deployment_mode=deployment.deployment_mode,
                    state="currency_mismatch",
                    catalog_name=catalog.catalog_name if catalog else None,
                    currency=currency,
                    catalogs_consulted=consulted,
                )
            )
            continue
        results.append(
            PricingReadiness(
                deployment=deployment.name,
                model=model,
                model_version=deployment.model_version,
                deployment_mode=deployment.deployment_mode,
                state="customer_override" if customer_entry is not None else "exact_public_rate",
                catalog_name=catalog.catalog_name if catalog else None,
                currency=currency,
                billing_basis=entry.billing_basis,
                catalogs_consulted=consulted,
            )
        )
    return results


def catalog_status(*, customer_catalog: PricingCatalog | None = None) -> dict[str, object]:
    """Summarize which catalogs are available, without contacting the network."""
    reference = load_bundled_reference_catalog()
    snapshot = synchronized_catalog_path()
    customer = customer_catalog if customer_catalog is not None else load_customer_catalog()
    return {
        "packaged-catalog": reference.catalog_name,
        "packaged-entries": len(reference.prices),
        "packaged-currency": reference.currency,
        "packaged-retrieved": reference.retrieved_at.isoformat() if reference.retrieved_at else "unknown",
        "customer-catalog": (
            safe_display_path(customer_catalog_path()) if customer is not None else "not configured"
        ),
        "customer-entries": len(customer.prices) if customer is not None else 0,
        "synchronized-snapshot": (
            safe_display_path(snapshot) if snapshot.is_file() else "not present (synchronization deferred)"
        ),
        "public-sync": "deferred",
    }


def verify_catalogs(*, customer_catalog: PricingCatalog | None = None) -> list[str]:
    """Validate local catalogs offline; returns a list of problems."""
    problems: list[str] = []
    try:
        reference = load_bundled_reference_catalog()
    except ValueError as exc:
        return [f"packaged catalog invalid: {exc}"]
    today = datetime.now(UTC).date()
    for entry in reference.prices:
        if entry.effective_to and entry.effective_to < today:
            problems.append(f"packaged entry expired and will not be applied: {entry.model}")
    customer = customer_catalog if customer_catalog is not None else None
    if customer is None:
        try:
            customer = load_customer_catalog()
        except WorkflowError as exc:
            return problems + [str(exc)]
    if customer is not None:
        if customer.currency.upper() != reference.currency.upper():
            problems.append(
                f"customer catalog currency {customer.currency.upper()} differs from the packaged "
                f"catalog currency {reference.currency.upper()}; TokenLens never converts currencies"
            )
        for entry in customer.prices:
            if entry.effective_to and entry.effective_to < today:
                problems.append(f"customer entry expired and will not be applied: {entry.model}")
            if entry.confidence != "customer_override":
                problems.append(f"customer entry must be labelled customer_override: {entry.model}")
    return problems


def unresolved_models(readiness: Iterable[PricingReadiness]) -> list[PricingReadiness]:
    return [item for item in readiness if not item.resolved]
