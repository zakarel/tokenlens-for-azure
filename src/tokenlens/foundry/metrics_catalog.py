"""Azure Monitor metric capability matrix.

Metric availability, dimension support, aggregation, and unit differ between
Azure OpenAI, Claude, and partner deployments. This module is an explicit,
reviewable table plus a resolver that builds queries *from the resource's own
metric definitions* rather than from an assumption. A metric whose definition
does not support ``ModelDeploymentName`` is excluded before the request instead
of producing an Azure 400 that is later swallowed as a generic missing metric.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Iterable

from .dimensions import canonical_dimension

#: Canonical analysis fields, in report order.
CANONICAL_FIELDS = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "requests",
    "successful_requests",
    "throttled_requests",
    "failed_requests",
    "average_latency_ms",
)


@dataclass(frozen=True)
class MetricCapability:
    """One documented Azure Monitor metric TokenLens knows how to read."""

    name: str
    field: str
    aggregation: str
    unit: str
    priority: int
    #: Dimensions the metric is documented to expose.
    dimensions: frozenset[str] = frozenset()
    #: Whether the metric can be filtered by ``ModelDeploymentName``.
    deployment_filterable: bool = True
    #: Whether a reported zero is a meaningful measurement (true for counters)
    #: rather than an artefact of a missing series.
    zero_is_meaningful: bool = True
    #: Deployment families the metric applies to.
    families: frozenset[str] = frozenset({"azure_openai", "claude_foundry", "partner_model"})

    @property
    def status_dimensioned(self) -> bool:
        return "StatusCode" in self.dimensions


_DEPLOYMENT_DIMS = frozenset({"ModelDeploymentName", "ModelName", "ModelVersion"})
_STATUS_DIMS = _DEPLOYMENT_DIMS | {"StatusCode"}

#: The capability matrix. Lower ``priority`` wins when a resource exposes more
#: than one candidate for the same canonical field.
METRIC_CAPABILITIES: tuple[MetricCapability, ...] = (
    # Tokens -----------------------------------------------------------------
    MetricCapability("InputTokens", "input_tokens", "Total", "tokens", 0, _DEPLOYMENT_DIMS),
    MetricCapability("ProcessedPromptTokens", "input_tokens", "Total", "tokens", 1, _DEPLOYMENT_DIMS),
    MetricCapability("PromptTokenCount", "input_tokens", "Total", "tokens", 2, _DEPLOYMENT_DIMS),
    MetricCapability("OutputTokens", "output_tokens", "Total", "tokens", 0, _DEPLOYMENT_DIMS),
    MetricCapability("GeneratedTokens", "output_tokens", "Total", "tokens", 1, _DEPLOYMENT_DIMS),
    MetricCapability("CompletionTokenCount", "output_tokens", "Total", "tokens", 2, _DEPLOYMENT_DIMS),
    MetricCapability(
        "cacheReadInputTokens",
        "cached_tokens",
        "Total",
        "tokens",
        0,
        _DEPLOYMENT_DIMS | {"ContextLength"},
    ),
    MetricCapability("ProcessedCachedPromptTokens", "cached_tokens", "Total", "tokens", 1, _DEPLOYMENT_DIMS),
    MetricCapability("CachedPromptTokens", "cached_tokens", "Total", "tokens", 2, _DEPLOYMENT_DIMS),
    # Requests and outcomes ---------------------------------------------------
    MetricCapability("ModelRequests", "requests", "Total", "requests", 0, _STATUS_DIMS),
    MetricCapability("AzureOpenAIRequests", "requests", "Total", "requests", 1, _STATUS_DIMS),
    MetricCapability(
        "TotalCalls",
        "requests",
        "Total",
        "requests",
        2,
        frozenset({"ApiName", "OperationName", "StatusCode"}),
        deployment_filterable=False,
    ),
    MetricCapability(
        "SuccessfulCalls",
        "successful_requests",
        "Total",
        "requests",
        0,
        frozenset({"ApiName", "OperationName", "StatusCode"}),
        deployment_filterable=False,
    ),
    MetricCapability("ThrottledCalls", "throttled_requests", "Total", "requests", 0, _DEPLOYMENT_DIMS),
    MetricCapability("AzureOpenAIThrottledRequests", "throttled_requests", "Total", "requests", 1, _DEPLOYMENT_DIMS),
    MetricCapability(
        "TotalErrors",
        "failed_requests",
        "Total",
        "requests",
        0,
        frozenset({"ApiName", "OperationName", "StatusCode"}),
        deployment_filterable=False,
    ),
    MetricCapability(
        "ClientErrors",
        "failed_requests",
        "Total",
        "requests",
        1,
        frozenset({"ApiName", "OperationName", "StatusCode"}),
        deployment_filterable=False,
    ),
    MetricCapability(
        "ServerErrors",
        "failed_requests",
        "Total",
        "requests",
        2,
        frozenset({"ApiName", "OperationName", "StatusCode"}),
        deployment_filterable=False,
    ),
    # Latency -----------------------------------------------------------------
    MetricCapability("AzureOpenAITimeToResponse", "average_latency_ms", "Average", "ms", 0, _STATUS_DIMS),
    MetricCapability("TimeToResponse", "average_latency_ms", "Average", "ms", 1, _STATUS_DIMS),
    MetricCapability("AzureOpenAINormalizedTTFTInMS", "average_latency_ms", "Average", "ms", 2, _DEPLOYMENT_DIMS),
    MetricCapability("NormalizedTimeToFirstByte", "average_latency_ms", "Average", "ms", 3, _DEPLOYMENT_DIMS),
    MetricCapability(
        "Latency",
        "average_latency_ms",
        "Average",
        "ms",
        4,
        frozenset({"ApiName", "OperationName", "StatusCode"}),
        deployment_filterable=False,
    ),
)

CAPABILITY_BY_NAME: dict[str, MetricCapability] = {
    capability.name.casefold(): capability for capability in METRIC_CAPABILITIES
}

#: Canonical field -> candidate metric names in resolution order. Kept as a
#: module-level table so the documented priority is reviewable at a glance.
METRIC_MAP: dict[str, tuple[str, ...]] = {
    field_name: tuple(
        capability.name
        for capability in sorted(
            (item for item in METRIC_CAPABILITIES if item.field == field_name),
            key=lambda item: item.priority,
        )
    )
    for field_name in CANONICAL_FIELDS
}

AGGREGATIONS: dict[str, str] = {
    field_name: next(item.aggregation for item in METRIC_CAPABILITIES if item.field == field_name)
    for field_name in CANONICAL_FIELDS
}


@dataclass(frozen=True)
class MetricDefinition:
    """One metric definition as reported by the resource itself."""

    name: str
    dimensions: frozenset[str] = frozenset()
    #: ``True`` when the resource reported its dimensions, ``False`` when only a
    #: metric name was available and the documented defaults must be used.
    dimensions_known: bool = False
    unit: str | None = None


def _definition_dimensions(raw: Any) -> tuple[frozenset[str], bool]:
    dimensions = raw.get("dimensions") if isinstance(raw, Mapping) else getattr(raw, "dimensions", None)
    if dimensions is None:
        return frozenset(), False
    names: set[str] = set()
    for item in dimensions:
        if isinstance(item, str):
            names.add(canonical_dimension(item))
            continue
        value = item.get("value") if isinstance(item, Mapping) else getattr(item, "value", None)
        if value is None:
            value = item.get("name") if isinstance(item, Mapping) else getattr(item, "name", None)
        if value is not None:
            names.add(canonical_dimension(str(value)))
    return frozenset(names), True


def parse_metric_definitions(raw: Iterable[Any]) -> list[MetricDefinition]:
    """Accept metric names, dicts, or SDK definition objects without guessing.

    A definition that carries its dimensions is authoritative. A bare metric
    name keeps ``dimensions_known=False`` so the documented capability defaults
    are used instead of pretending the resource confirmed them.
    """
    definitions: list[MetricDefinition] = []
    for item in raw:
        if isinstance(item, MetricDefinition):
            definitions.append(item)
            continue
        if isinstance(item, str):
            definitions.append(MetricDefinition(name=item))
            continue
        name = item.get("name") if isinstance(item, Mapping) else getattr(item, "name", None)
        if isinstance(name, Mapping):
            name = name.get("value")
        elif name is not None and not isinstance(name, str):
            name = getattr(name, "value", name)
        if not name:
            continue
        dimensions, known = _definition_dimensions(item)
        unit = item.get("unit") if isinstance(item, Mapping) else getattr(item, "unit", None)
        definitions.append(
            MetricDefinition(
                name=str(name),
                dimensions=dimensions,
                dimensions_known=known,
                unit=str(unit) if unit is not None else None,
            )
        )
    return definitions


@dataclass(frozen=True)
class MetricPlan:
    """One metric TokenLens will actually query, with its exact query shape."""

    field: str
    metric_name: str
    aggregation: str
    unit: str
    deployment_filterable: bool
    status_dimensioned: bool
    zero_is_meaningful: bool
    dimensions: frozenset[str] = frozenset()
    #: ``True`` when the resource itself reported the metric's dimensions.
    dimensions_confirmed: bool = False


@dataclass
class MetricSelection:
    """The resolved query plan plus every documented exclusion reason."""

    plans: dict[str, MetricPlan] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    #: ``metric name -> reason`` for candidates excluded before any request.
    excluded: dict[str, str] = field(default_factory=dict)

    @property
    def resolved(self) -> dict[str, str]:
        return {name: plan.metric_name for name, plan in self.plans.items()}

    @property
    def status_field(self) -> str | None:
        plan = self.plans.get("requests")
        return "requests" if plan is not None and plan.status_dimensioned else None


#: Which canonical fields each supported deployment family is expected to
#: provide. Anything absent is surfaced in ``missing_fields``.
CAPABILITY_MATRIX: dict[str, frozenset[str]] = {
    "azure_openai": frozenset(
        {
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "requests",
            "successful_requests",
            "throttled_requests",
            "failed_requests",
            "average_latency_ms",
        }
    ),
    "claude_foundry": frozenset({"input_tokens", "output_tokens", "requests", "throttled_requests"}),
    "partner_model": frozenset({"requests"}),
    "unknown": frozenset(),
}


def select_metrics(
    definitions: Iterable[Any],
    *,
    family: str = "azure_openai",
    deployment_filtered: bool = True,
) -> MetricSelection:
    """Resolve canonical fields to concrete metrics this resource can answer.

    Nothing is guessed. A field is resolved only when the resource's own metric
    definitions contain one of its documented candidates *and* that candidate
    supports the dimensions the query needs.
    """
    parsed = parse_metric_definitions(definitions)
    by_name = {item.name.casefold(): item for item in parsed}
    expected = CAPABILITY_MATRIX.get(family, CAPABILITY_MATRIX["unknown"])
    selection = MetricSelection()
    for field_name in CANONICAL_FIELDS:
        candidates = sorted(
            (item for item in METRIC_CAPABILITIES if item.field == field_name),
            key=lambda item: item.priority,
        )
        chosen: MetricPlan | None = None
        for capability in candidates:
            definition = by_name.get(capability.name.casefold())
            if definition is None:
                continue
            if family not in capability.families:
                selection.excluded[capability.name] = f"not applicable to the {family} family"
                continue
            dimensions = definition.dimensions if definition.dimensions_known else capability.dimensions
            filterable = (
                "ModelDeploymentName" in dimensions
                if definition.dimensions_known
                else capability.deployment_filterable
            )
            if deployment_filtered and not filterable:
                selection.excluded[capability.name] = (
                    "metric definition does not support a ModelDeploymentName filter"
                )
                continue
            chosen = MetricPlan(
                field=field_name,
                metric_name=capability.name,
                aggregation=capability.aggregation,
                unit=capability.unit,
                deployment_filterable=filterable,
                # A status filter is only sent when the resource's own
                # definition confirms the dimension. An assumed filter is how a
                # collection earns an Azure 400.
                status_dimensioned=definition.dimensions_known and "StatusCode" in dimensions,
                zero_is_meaningful=capability.zero_is_meaningful,
                dimensions=frozenset(dimensions),
                dimensions_confirmed=definition.dimensions_known,
            )
            break
        if chosen is not None:
            selection.plans[field_name] = chosen
        elif field_name in expected:
            selection.missing_fields.append(field_name)
    return selection


__all__ = [
    "AGGREGATIONS",
    "CANONICAL_FIELDS",
    "CAPABILITY_BY_NAME",
    "CAPABILITY_MATRIX",
    "METRIC_CAPABILITIES",
    "METRIC_MAP",
    "MetricCapability",
    "MetricDefinition",
    "MetricPlan",
    "MetricSelection",
    "parse_metric_definitions",
    "select_metrics",
]
