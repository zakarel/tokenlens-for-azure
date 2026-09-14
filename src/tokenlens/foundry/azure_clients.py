"""Live Azure client construction, isolated from all collection logic.

Nothing in this module is imported until a user runs an explicit collection
command, so the offline analyzer never depends on an Azure SDK and tests never
construct a credential.
"""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from typing import Any, Iterable, Sequence

from .monitor import CollectorError, resource_uri

MONITOR_EXTRA_HINT = "Install the collector extras: pip install 'tokenlens-azure[foundry-monitor]'"


def has_package(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def credential() -> Any:
    """Build ``DefaultAzureCredential``; ``az login`` is a prerequisite, not a step TokenLens runs."""
    if not has_package("azure.identity"):
        raise CollectorError(f"azure-identity is not installed. {MONITOR_EXTRA_HINT}")
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


class AzureMonitorMetricsClient:
    """Thin adapter over the supported Azure Monitor metrics query client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def create(cls, credential_object: Any | None = None) -> "AzureMonitorMetricsClient":
        if has_package("azure.monitor.querymetrics"):
            from azure.monitor.querymetrics import MetricsClient as _Client  # type: ignore[import-not-found]
        elif has_package("azure.monitor.query"):
            from azure.monitor.query import MetricsQueryClient as _Client  # type: ignore[import-not-found]
        else:
            raise CollectorError(f"No supported Azure Monitor metrics package is installed. {MONITOR_EXTRA_HINT}")
        return cls(_Client(credential_object or credential()))

    def query_resource(
        self,
        resource: str,
        metric_names: Sequence[str],
        *,
        timespan: Any,
        granularity: Any,
        aggregations: Sequence[str],
        filter: str | None = None,
        page_token: str | None = None,
    ) -> Any:
        # ``page_token`` is accepted for interface parity; the Azure SDK pages
        # internally and returns a complete response object.
        return self._client.query_resource(
            resource,
            metric_names=list(metric_names),
            timespan=timespan,
            granularity=granularity if isinstance(granularity, timedelta) else timedelta(minutes=5),
            aggregations=list(aggregations),
            filter=filter,
        )


class AzureResourceClient:
    """Discovery limited to one explicitly selected subscription."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def create(cls, subscription_id: str, credential_object: Any | None = None) -> "AzureResourceClient":
        if not has_package("azure.mgmt.cognitiveservices"):
            raise CollectorError(
                "azure-mgmt-cognitiveservices is not installed. " + MONITOR_EXTRA_HINT
            )
        from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient  # type: ignore[import-not-found]

        return cls(CognitiveServicesManagementClient(credential_object or credential(), subscription_id))

    def list_accounts(self, subscription_id: str, resource_group: str | None = None) -> Iterable[dict[str, Any]]:
        source = (
            self._client.accounts.list_by_resource_group(resource_group)
            if resource_group
            else self._client.accounts.list()
        )
        for account in source:
            yield {
                "name": getattr(account, "name", ""),
                "resource_group": _resource_group_of(getattr(account, "id", "")),
                "kind": getattr(account, "kind", ""),
                "location": getattr(account, "location", ""),
            }

    def list_deployments(self, subscription_id: str, resource_group: str, account: str) -> Iterable[dict[str, Any]]:
        for deployment in self._client.deployments.list(resource_group, account):
            properties = getattr(deployment, "properties", None)
            model = getattr(properties, "model", None)
            sku = getattr(deployment, "sku", None)
            yield {
                "name": getattr(deployment, "name", ""),
                "model": getattr(model, "name", "") if model else "",
                "model_version": getattr(model, "version", "") if model else "",
                "sku": getattr(sku, "name", "") if sku else "",
                "capacity": getattr(sku, "capacity", None) if sku else None,
            }

    def list_metric_definitions(self, resource: str) -> Iterable[str]:
        if not has_package("azure.mgmt.monitor"):
            raise CollectorError("azure-mgmt-monitor is not installed. " + MONITOR_EXTRA_HINT)
        from azure.mgmt.monitor import MonitorManagementClient  # type: ignore[import-not-found]

        subscription = resource.split("/subscriptions/", 1)[1].split("/", 1)[0]
        client = MonitorManagementClient(credential(), subscription)
        for definition in client.metric_definitions.list(resource):
            name = getattr(definition, "name", None)
            value = getattr(name, "value", None) if name is not None else None
            if value:
                yield str(value)


def _resource_group_of(resource_id: str) -> str:
    marker = "/resourceGroups/"
    if marker not in resource_id:
        return ""
    return resource_id.split(marker, 1)[1].split("/", 1)[0]


__all__ = [
    "AzureMonitorMetricsClient",
    "AzureResourceClient",
    "credential",
    "has_package",
    "resource_uri",
]
