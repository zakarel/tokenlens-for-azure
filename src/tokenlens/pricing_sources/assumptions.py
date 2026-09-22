"""The pricing dimensions TokenLens assumes when telemetry does not state them.

Azure Foundry publishes a separate meter for every purchasing dimension: retail
versus a negotiated agreement, global versus data-zone versus regional,
standard versus batch/priority/flex/provisioned, short versus long context, and
normal inference versus fine-tuned or hosted serving.

Telemetry and deployment metadata frequently resolve only some of those. The
remaining dimensions are resolved by an explicit, documented default rather
than by a silent choice, and every default that was actually applied is carried
through to the report, the JSON metadata, and the pricing provenance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ASSUMPTION_DIMENSIONS",
    "CONTEXT_VALUES",
    "DEPLOYMENT_VALUES",
    "INFERENCE_VALUES",
    "SERVICE_TIER_VALUES",
    "USD_PER_CCU",
    "ASSUMPTIONS_BANNER",
    "ASSUMPTIONS_WARNING",
    "DEFAULT_PRICING_ASSUMPTIONS",
    "PricingAssumptions",
    "PricingDimensionError",
    "assumption_label",
    "canonical_context",
    "canonical_deployment",
    "canonical_inference",
    "canonical_service_tier",
    "optional_deployment",
]

#: Azure bills Anthropic consumption units at a fixed 100 CCU per US dollar.
USD_PER_CCU = 0.01

PurchaseModel = Literal["retail"]
DeploymentDimension = Literal["global", "data_zone", "regional"]
ServiceTierDimension = Literal["standard"]
ContextDimension = Literal["short", "long"]
InferenceDimension = Literal["normal"]

#: Ordered (key, human label) pairs used for the banner and the JSON metadata.
ASSUMPTION_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("purchase_model", "Retail"),
    ("deployment", "Global"),
    ("service_tier", "Standard"),
    ("context", "Short context"),
    ("inference", "Normal inference"),
)

_LABELS: dict[str, dict[str, str]] = {
    "purchase_model": {"retail": "Retail"},
    "deployment": {"global": "Global", "data_zone": "Data zone", "regional": "Regional"},
    "service_tier": {"standard": "Standard"},
    "context": {"short": "Short context", "long": "Long context"},
    "inference": {"normal": "Normal inference"},
}


def assumption_label(dimension: str, value: str) -> str:
    """Human label for one assumed dimension value."""
    return _LABELS.get(dimension, {}).get(value, value.replace("_", " ").capitalize())


class PricingAssumptions(BaseModel):
    """The defaults applied when a more specific dimension is not available."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    purchase_model: PurchaseModel = "retail"
    deployment: DeploymentDimension = "global"
    service_tier: ServiceTierDimension = "standard"
    context: ContextDimension = "short"
    inference: InferenceDimension = "normal"

    def labels(self) -> list[str]:
        return [assumption_label(key, getattr(self, key)) for key, _ in ASSUMPTION_DIMENSIONS]

    def banner_text(self) -> str:
        """The exact line rendered in bold at the top of Cost analysis."""
        return "Pricing assumptions: " + " · ".join(self.labels())

    def to_metadata(self) -> dict[str, object]:
        """Machine-readable form embedded in report metadata and snapshots."""
        return {
            "applies_when": "telemetry_or_deployment_metadata_did_not_state_the_dimension",
            "overridden_by": "exact_observed_or_configured_dimensions",
            "banner": self.banner_text(),
            "warning": ASSUMPTIONS_WARNING,
            "dimensions": {key: getattr(self, key) for key, _ in ASSUMPTION_DIMENSIONS},
            "labels": {key: assumption_label(key, getattr(self, key)) for key, _ in ASSUMPTION_DIMENSIONS},
            "excluded_modes": [
                "batch",
                "fine_tuning",
                "priority",
                "provisioned",
                "flex",
                "media",
                "tool",
                "session",
            ],
        }


DEFAULT_PRICING_ASSUMPTIONS = PricingAssumptions()

ASSUMPTIONS_BANNER = DEFAULT_PRICING_ASSUMPTIONS.banner_text()

ASSUMPTIONS_WARNING = (
    "These defaults are used only where telemetry or deployment metadata did not provide a more "
    "specific pricing dimension. Exact observed or configured dimensions — deployment mode, "
    "service tier, context window, inference mode, and any contracted rate — always override them."
)


# --------------------------------------------------------------------------
# One canonicalizer, used by every source
# --------------------------------------------------------------------------


class PricingDimensionError(ValueError):
    """A pricing dimension value is not one TokenLens recognizes."""


DEPLOYMENT_VALUES: tuple[str, ...] = ("global", "data_zone", "regional")
CONTEXT_VALUES: tuple[str, ...] = ("short", "long")
SERVICE_TIER_VALUES: tuple[str, ...] = ("standard",)
INFERENCE_VALUES: tuple[str, ...] = ("normal",)

#: Values that mean "the dimension was not stated", not "an invalid value".
UNSTATED_VALUES: frozenset[str] = frozenset({"", "unknown", "none", "unspecified", "null"})

_DEPLOYMENT_ALIASES: dict[str, str] = {
    "global": "global",
    "globalstandard": "global",
    "global_standard": "global",
    "gl": "global",
    "glbl": "global",
    "data_zone": "data_zone",
    "datazone": "data_zone",
    "data_zone_standard": "data_zone",
    "datazonestandard": "data_zone",
    "dz": "data_zone",
    "dzone": "data_zone",
    "regional": "regional",
    "regionalstandard": "regional",
    "regional_standard": "regional",
    "regnl": "regional",
    "reg": "regional",
}

_CONTEXT_ALIASES: dict[str, str] = {
    "short": "short",
    "short_context": "short",
    "shortcontext": "short",
    "shortco": "short",
    "shco": "short",
    "long": "long",
    "long_context": "long",
    "longcontext": "long",
    "longco": "long",
    "loco": "long",
}

_SERVICE_TIER_ALIASES: dict[str, str] = {"standard": "standard", "std": "standard"}

_INFERENCE_ALIASES: dict[str, str] = {
    "normal": "normal",
    "standard": "normal",
    "normal_inference": "normal",
}


def _normalize(value: object) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _canonical(value: object, aliases: dict[str, str], dimension: str, allowed: tuple[str, ...]) -> str:
    normalized = _normalize(value)
    resolved = aliases.get(normalized)
    if resolved is None:
        raise PricingDimensionError(
            f"{value!r} is not a supported {dimension}. Supported values: {', '.join(allowed)}."
        )
    return resolved


def canonical_deployment(value: object) -> str:
    """Canonical deployment mode, or :class:`PricingDimensionError`."""
    return _canonical(value, _DEPLOYMENT_ALIASES, "deployment mode", DEPLOYMENT_VALUES)


def optional_deployment(value: object) -> str | None:
    """Canonical deployment mode, or ``None`` when the dimension is unstated.

    An unstated mode is a fact about the telemetry. An unrecognized mode is a
    caller error, and is still rejected.
    """
    if _normalize(value) in UNSTATED_VALUES:
        return None
    return canonical_deployment(value)


def canonical_context(value: object) -> str:
    """Canonical context window, or :class:`PricingDimensionError`."""
    return _canonical(value, _CONTEXT_ALIASES, "context window", CONTEXT_VALUES)


def canonical_service_tier(value: object) -> str:
    """Canonical service tier, or :class:`PricingDimensionError`."""
    return _canonical(value, _SERVICE_TIER_ALIASES, "service tier", SERVICE_TIER_VALUES)


def canonical_inference(value: object) -> str:
    """Canonical inference mode, or :class:`PricingDimensionError`."""
    return _canonical(value, _INFERENCE_ALIASES, "inference mode", INFERENCE_VALUES)
