"""Azure Monitor dimension normalization.

``QueryMetrics`` returns ``TimeSeriesElement.metadata_values`` as a *dictionary*
in the currently shipping SDK, while older ``azure-monitor-query`` builds return
a sequence of metadata objects and raw REST payloads return a sequence of
dictionaries. All three shapes are normalized here so identity is never lost and
never guessed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: Canonical dimension names TokenLens depends on, keyed by their case-folded
#: Azure spelling. Anything not listed is preserved verbatim for diagnostics.
DIMENSION_ALIASES: dict[str, str] = {
    "modeldeploymentname": "ModelDeploymentName",
    "deploymentname": "ModelDeploymentName",
    "modelname": "ModelName",
    "model": "ModelName",
    "modelversion": "ModelVersion",
    "statuscode": "StatusCode",
    "servicetierrequest": "ServiceTierRequest",
    "servicetierresponse": "ServiceTierResponse",
    "region": "Region",
    "apiname": "ApiName",
    "operationname": "OperationName",
    "contextlength": "ContextLength",
    "streamtype": "StreamType",
}

#: Dimensions TokenLens reads. Everything else is namespaced under
#: ``azure.<name>`` so a future Azure dimension is visible to diagnostics
#: without being mistaken for identity.
CANONICAL_DIMENSIONS = frozenset(DIMENSION_ALIASES.values())


def canonical_dimension(name: str) -> str:
    return DIMENSION_ALIASES.get(name.strip().casefold(), name.strip())


def _name_of(raw: Any) -> str | None:
    """Read a dimension name from a dict, a LocalizableString, or a plain value."""
    if isinstance(raw, Mapping):
        name = raw.get("name")
    else:
        name = getattr(raw, "name", None)
    if isinstance(name, Mapping):
        name = name.get("value")
    elif name is not None and not isinstance(name, str):
        name = getattr(name, "value", name)
    return str(name) if name is not None else None


def _value_of(raw: Any) -> str | None:
    value = raw.get("value") if isinstance(raw, Mapping) else getattr(raw, "value", None)
    if isinstance(value, Mapping):
        value = value.get("value")
    return None if value is None else str(value)


def normalize_dimensions(raw: object) -> dict[str, str]:
    """Normalize every supported ``metadata_values`` shape into a flat mapping.

    Supported inputs:

    * ``Mapping[str, object]`` — the installed ``azure-monitor-querymetrics``
      shape, for example ``{"modeldeploymentname": "chat-prod"}``;
    * ``Sequence[MetadataValue]`` — legacy SDK objects with ``name``/``value``;
    * ``Sequence[dict]`` — raw REST payloads.

    Names are matched case-insensitively and mapped to their canonical Azure
    spelling. Unknown dimensions are kept under an ``azure.`` namespace so they
    can be reported without ever being treated as identity.
    """
    if raw is None:
        return {}
    items: list[tuple[str | None, str | None]] = []
    if isinstance(raw, Mapping):
        items = [(str(key), None if value is None else str(value)) for key, value in raw.items()]
    elif isinstance(raw, (str, bytes)):
        return {}
    elif isinstance(raw, Sequence):
        items = [(_name_of(item), _value_of(item)) for item in raw]
    else:
        try:
            iterator = iter(raw)  # type: ignore[call-overload]
        except TypeError:
            return {}
        items = [(_name_of(item), _value_of(item)) for item in iterator]

    normalized: dict[str, str] = {}
    for name, value in items:
        if not name or value is None:
            continue
        canonical = canonical_dimension(name)
        key = canonical if canonical in CANONICAL_DIMENSIONS else f"azure.{canonical}"
        normalized[key] = value
    return normalized


def identity_dimensions(dimensions: Mapping[str, str]) -> dict[str, str]:
    """Return only the canonical identity dimensions from a normalized mapping."""
    return {key: value for key, value in dimensions.items() if key in CANONICAL_DIMENSIONS}


def extra_dimensions(dimensions: Mapping[str, str]) -> dict[str, str]:
    """Return the namespaced dimensions Azure reported that TokenLens does not model."""
    return {key: value for key, value in dimensions.items() if key.startswith("azure.")}


__all__ = [
    "CANONICAL_DIMENSIONS",
    "DIMENSION_ALIASES",
    "canonical_dimension",
    "extra_dimensions",
    "identity_dimensions",
    "normalize_dimensions",
]
