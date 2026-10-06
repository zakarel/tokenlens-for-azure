"""Offline adapters for customer-specific pricing and actual-cost imports."""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .providers import (
    BillingComponent,
    CustomerPricingScope,
    DeploymentIdentity,
    PriceResolution,
)


class ImportedPriceRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    processing_scope: str = "unknown"
    component: str
    unit: str
    unit_multiplier: float = Field(default=1, gt=0)
    unit_price: float = Field(ge=0)
    quantity: float | None = Field(default=None, ge=0)
    currency: str = "USD"
    source_record_id: str
    meter_id: str | None = None
    product_id: str | None = None
    effective_from: date
    effective_to: date | None = None
    source_scope: str
    raw_record_hash: str | None = None


class ActualCostSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = "actual-cost"
    currency: str = "USD"
    rows: int = 0
    matched_rows: int = 0
    unattributed_rows: int = 0
    total_actual_cost: float | None = None
    join_basis: str = "exact model + component"
    note: str = ""


def summarize_actual_cost(rows: Iterable[ImportedPriceRow], *, known_models: set[str] | None = None) -> ActualCostSummary:
    items = list(rows)
    matched = [row for row in items if known_models is None or row.model.casefold() in known_models]
    total = sum(
        row.unit_price * float(row.quantity or 0) / row.unit_multiplier
        for row in matched
    )
    return ActualCostSummary(
        rows=len(items),
        matched_rows=len(matched),
        unattributed_rows=len(items) - len(matched),
        total_actual_cost=total if matched else None,
        note="Actual billed cost is shown separately from forward estimates; unjoinable rows remain unattributed.",
    )


class ImportedPricingProvider:
    """Resolve normalized rows without collapsing duplicates or currencies."""

    def __init__(self, name: str, state: str, rows: Iterable[ImportedPriceRow]) -> None:
        self.name = name
        self.state = state
        self._rows = list(rows)

    def resolve(
        self,
        identity: DeploymentIdentity,
        component: BillingComponent,
        at: datetime,
        customer_scope: CustomerPricingScope | None = None,
    ) -> PriceResolution:
        matches = [
            row
            for row in self._rows
            if row.model.casefold() == identity.model.casefold()
            and row.processing_scope.casefold() == identity.processing_scope.casefold()
            and row.component.casefold() == component.name.casefold()
            and row.currency.casefold() == component.currency.casefold()
            and row.effective_from <= at.date()
            and (row.effective_to is None or at.date() <= row.effective_to)
        ]
        if not matches:
            return PriceResolution(
                state="NO_VERIFIED_PRICE",
                resolved=False,
                provider=self.name,
                currency=component.currency,
                unit=component.unit,
                reason="no exact imported row matched",
            )
        if len(matches) > 1:
            return PriceResolution(
                state="AMBIGUOUS",
                resolved=False,
                provider=self.name,
                currency=component.currency,
                unit=component.unit,
                reason="multiple imported rows matched the same identity and period",
            )
        row = matches[0]
        amount = (
            row.unit_price * component.quantity / row.unit_multiplier
            if component.quantity is not None
            else None
        )
        return PriceResolution(
            state=self.state if amount is not None else "RATE_VERIFIED_USAGE_MISSING",
            resolved=amount is not None,
            provider=self.name,
            source_record_id=row.source_record_id,
            product_id=row.product_id,
            meter_id=row.meter_id,
            currency=row.currency,
            unit=row.unit,
            unit_price=row.unit_price,
            quantity=component.quantity,
            amount=amount,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            source_scope=row.source_scope,
            raw_record_hash=row.raw_record_hash,
            reason=None if amount is not None else "verified rate but usage quantity is unavailable",
        )


def actual_cost_provider(rows: Iterable[ImportedPriceRow]) -> ImportedPricingProvider:
    return ImportedPricingProvider("actual_cost", "EXACT_ACTUAL", rows)


def contract_price_provider(rows: Iterable[ImportedPriceRow]) -> ImportedPricingProvider:
    return ImportedPricingProvider("contract", "EXACT_CONTRACT", rows)


def marketplace_price_provider(rows: Iterable[ImportedPriceRow]) -> ImportedPricingProvider:
    return ImportedPricingProvider("marketplace", "EXACT_MARKETPLACE", rows)


def override_price_provider(rows: Iterable[ImportedPriceRow]) -> ImportedPricingProvider:
    return ImportedPricingProvider("override", "EXACT_OVERRIDE", rows)


__all__ = [
    "ImportedPriceRow",
    "ImportedPricingProvider",
    "ActualCostSummary",
    "summarize_actual_cost",
    "actual_cost_provider",
    "contract_price_provider",
    "marketplace_price_provider",
    "override_price_provider",
]
