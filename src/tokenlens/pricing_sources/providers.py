"""Layered pricing providers with explicit provenance and unresolved states."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class DeploymentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    model_version: str | None = None
    deployment: str | None = None
    deployment_type: str = "unknown"
    service_tier: str = "standard"
    processing_scope: str = "unknown"
    region: str | None = None
    sku: str | None = None
    commerce_type: str = "unknown"
    router: bool = False


class BillingComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    quantity: float | None = Field(default=None, ge=0)
    unit: str
    currency: str = "USD"


class CustomerPricingScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agreement: str | None = None
    billing_account: str | None = None
    subscription: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None


class PriceResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str
    resolved: bool
    provider: str
    source_record_id: str | None = None
    product_id: str | None = None
    meter_id: str | None = None
    currency: str | None = None
    unit: str | None = None
    unit_price: float | None = Field(default=None, ge=0)
    quantity: float | None = Field(default=None, ge=0)
    amount: float | None = Field(default=None, ge=0)
    effective_from: date | None = None
    effective_to: date | None = None
    retrieved_at: datetime | None = None
    source_scope: str | None = None
    raw_record_hash: str | None = None
    mapping_rule_version: str | None = None
    reason: str | None = None


class PricingProvider(Protocol):
    name: str

    def resolve(
        self,
        identity: DeploymentIdentity,
        component: BillingComponent,
        at: datetime,
        customer_scope: CustomerPricingScope | None = None,
    ) -> PriceResolution:
        ...


@dataclass(frozen=True)
class StaticPricingProvider:
    """A deterministic provider used by imported catalogs and offline tests."""

    name: str
    state: str
    rates: dict[tuple[str, str, str], tuple[float, str, str]]
    source_scope: str = "offline"
    currency: str = "USD"
    mapping_rule_version: str = "1"

    def resolve(
        self,
        identity: DeploymentIdentity,
        component: BillingComponent,
        at: datetime,
        customer_scope: CustomerPricingScope | None = None,
    ) -> PriceResolution:
        key = (
            identity.model.casefold(),
            identity.processing_scope.casefold(),
            component.name.casefold(),
        )
        row = self.rates.get(key)
        if row is None:
            return PriceResolution(
                state="NO_VERIFIED_PRICE",
                resolved=False,
                provider=self.name,
                currency=self.currency,
                unit=component.unit,
                reason="exact model, scope, and billing component were not found",
            )
        unit_price, source_record_id, meter_id = row
        amount = unit_price * component.quantity if component.quantity is not None else None
        return PriceResolution(
            state=self.state,
            resolved=amount is not None,
            provider=self.name,
            source_record_id=source_record_id,
            meter_id=meter_id,
            currency=self.currency,
            unit=component.unit,
            unit_price=unit_price,
            quantity=component.quantity,
            amount=amount,
            effective_from=at.date(),
            source_scope=self.source_scope,
            mapping_rule_version=self.mapping_rule_version,
            reason=None if amount is not None else "verified rate but usage quantity is unavailable",
        )


class NoPriceProvider:
    name = "none"

    def resolve(
        self,
        identity: DeploymentIdentity,
        component: BillingComponent,
        at: datetime,
        customer_scope: CustomerPricingScope | None = None,
    ) -> PriceResolution:
        return PriceResolution(
            state="NO_VERIFIED_PRICE",
            resolved=False,
            provider=self.name,
            currency=component.currency,
            unit=component.unit,
            reason="no verified pricing provider matched",
        )


class LayeredPricingResolver:
    """Resolve one track without merging actual and forward-looking prices."""

    def __init__(self, providers: list[PricingProvider]) -> None:
        self.providers = providers

    def resolve(
        self,
        identity: DeploymentIdentity,
        component: BillingComponent,
        at: datetime,
        customer_scope: CustomerPricingScope | None = None,
    ) -> PriceResolution:
        expected_currency = component.currency.casefold()
        for provider in self.providers:
            resolution = provider.resolve(identity, component, at, customer_scope)
            if resolution.resolved:
                if resolution.currency and resolution.currency.casefold() != expected_currency:
                    continue
                return resolution
        return NoPriceProvider().resolve(identity, component, at, customer_scope)


__all__ = [
    "BillingComponent",
    "CustomerPricingScope",
    "DeploymentIdentity",
    "LayeredPricingResolver",
    "NoPriceProvider",
    "PriceResolution",
    "PricingProvider",
    "StaticPricingProvider",
]
