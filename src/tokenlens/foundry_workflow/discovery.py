"""Discovery and deterministic metadata detection.

Nothing in this module invents identity. A deployment mode comes from its exact
SKU, a provider family from the model's publisher, and an inference API from the
provider family. Anything that cannot be resolved stays ``unknown``.

Azure access is confined to injected clients and one Azure CLI subprocess, so
tests exercise every mapping offline.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Iterable, Sequence

from ..pricing import canonical_model_name, infer_publisher
from .models import (
    AccountOption,
    DeploymentRecord,
    InferenceApi,
    ProviderFamily,
    SubscriptionOption,
    WorkflowError,
)

__all__ = [
    "SUPPORTED_ACCOUNT_KINDS",
    "anthropic_base_url",
    "deployment_mode_for_sku",
    "discover_accounts",
    "discover_deployments",
    "inference_api_for_family",
    "list_subscriptions",
    "metrics_endpoint_for_region",
    "normalize_endpoint",
    "provider_family_for_model",
]

#: Cognitive Services kinds that host Foundry/Azure OpenAI deployments. Other
#: kinds (Speech, Vision, Language) are filtered out before the user sees them.
SUPPORTED_ACCOUNT_KINDS = ("aiservices", "openai")

#: Exact SKU name -> canonical deployment mode. The original SKU string is kept
#: separately for provenance; nothing here falls back to Global.
_SKU_MODES: tuple[tuple[str, str], ...] = (
    ("globalprovisionedmanaged", "global_provisioned"),
    ("datazoneprovisionedmanaged", "data_zone_provisioned"),
    ("provisionedmanaged", "regional_provisioned"),
    ("globalbatch", "batch"),
    ("datazonebatch", "batch"),
    ("globalstandard", "global"),
    ("datazonestandard", "data_zone"),
    ("standard", "regional"),
    ("batch", "batch"),
)

_FAMILY_BY_PUBLISHER: dict[str, ProviderFamily] = {
    "microsoft": "azure_openai",
    "openai": "azure_openai",
    "anthropic": "claude_foundry",
    "mistral": "partner_model",
    "meta": "partner_model",
    "cohere": "partner_model",
    "deepseek": "partner_model",
    "google": "partner_model",
    "ai21": "partner_model",
}


def deployment_mode_for_sku(sku: str | None) -> str:
    """Map an exact deployment SKU to a canonical mode.

    An unrecognised or missing SKU returns ``unknown``. That is a deliberate,
    visible state: PTU sizing and pricing differ per mode.
    """
    normalized = re.sub(r"[\s_-]+", "", str(sku or "").casefold())
    if not normalized:
        return "unknown"
    for candidate, mode in _SKU_MODES:
        if normalized == candidate:
            return mode
    for candidate, mode in _SKU_MODES:
        if normalized.startswith(candidate) or normalized.endswith(candidate):
            return mode
    return "unknown"


def provider_family_for_model(model: str | None, publisher: str | None = None) -> ProviderFamily:
    """Resolve the provider family from the model's publisher."""
    resolved = (publisher or infer_publisher(str(model or "")) or "").casefold()
    return _FAMILY_BY_PUBLISHER.get(resolved, "unknown")


def inference_api_for_family(family: ProviderFamily) -> InferenceApi:
    """Resolve the documented inference API for a provider family.

    Claude on Foundry speaks the Anthropic Messages API. Azure OpenAI and
    partner models routed through Foundry speak the OpenAI-compatible API.
    A failed billable request is never retried through a different API.
    """
    if family == "claude_foundry":
        return "anthropic"
    if family in {"azure_openai", "partner_model"}:
        return "openai"
    return "unknown"


def normalize_endpoint(endpoint: str | None) -> str | None:
    """Normalize a discovered account endpoint to its base services form."""
    if not endpoint:
        return None
    value = str(endpoint).strip().rstrip("/")
    if not value.startswith("https://"):
        raise WorkflowError("Only https endpoints are accepted for Foundry discovery.")
    for suffix in ("/anthropic", "/openai/v1", "/openai", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    return value or None


def anthropic_base_url(endpoint: str | None) -> str | None:
    """Append ``/anthropic`` for Claude at client-construction time only."""
    base = normalize_endpoint(endpoint)
    return f"{base}/anthropic" if base else None


def metrics_endpoint_for_region(region: str | None, *, override: str | None = None) -> str:
    """Derive the regional Azure Monitor QueryMetrics endpoint from the account region."""
    if override:
        return str(override).rstrip("/")
    from ..foundry.azure_clients import metrics_endpoint_for_location

    if not region:
        raise WorkflowError(
            "The Foundry account region is unknown, so the Azure Monitor regional endpoint "
            "cannot be derived. Set an explicit metrics endpoint override."
        )
    return metrics_endpoint_for_location(str(region))


def _az_available() -> bool:
    return shutil.which("az") is not None


def list_subscriptions(*, runner=None) -> list[SubscriptionOption]:
    """List accessible subscriptions using the Azure CLI's own account list.

    TokenLens never scans subscriptions silently: the caller shows this list and
    requires an explicit choice.
    """
    if runner is None:
        if not _az_available():
            return []

        def runner() -> str:  # pragma: no cover - exercised through injection
            result = subprocess.run(
                ["az", "account", "list", "--output", "json"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return result.stdout if result.returncode == 0 else "[]"

    try:
        payload = json.loads(runner() or "[]")
    except (json.JSONDecodeError, OSError, subprocess.TimeoutExpired):
        return []
    options: list[SubscriptionOption] = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        subscription_id = str(entry.get("id") or "").strip()
        if not subscription_id:
            continue
        if str(entry.get("state") or "Enabled").casefold() != "enabled":
            continue
        options.append(
            SubscriptionOption(
                subscription_id=subscription_id,
                name=str(entry.get("name") or subscription_id),
                tenant_name=(
                    str(entry["tenantDisplayName"])
                    if isinstance(entry.get("tenantDisplayName"), str) and entry["tenantDisplayName"]
                    else None
                ),
                is_default=bool(entry.get("isDefault")),
            )
        )
    return sorted(options, key=lambda item: (not item.is_default, item.name.casefold()))


def discover_accounts(
    resources: Any,
    subscription_id: str,
    *,
    resource_group: str | None = None,
) -> list[AccountOption]:
    """List only Foundry/Azure OpenAI accounts in the selected subscription."""
    options: list[AccountOption] = []
    for account in resources.list_accounts(subscription_id, resource_group):
        kind = str(account.get("kind") or "")
        if kind and kind.casefold() not in SUPPORTED_ACCOUNT_KINDS:
            continue
        options.append(
            AccountOption(
                name=str(account.get("name") or ""),
                resource_group=str(account.get("resource_group") or resource_group or ""),
                location=str(account.get("location") or ""),
                kind=kind,
            )
        )
    return sorted(options, key=lambda item: (item.name.casefold(), item.resource_group.casefold()))


def enrich_deployment(raw: dict[str, Any]) -> DeploymentRecord:
    """Attach mode, provider family, and inference API to one inventory entry."""
    model = str(raw.get("model") or "")
    publisher = infer_publisher(model) if model else None
    family = provider_family_for_model(model, publisher)
    capacity = raw.get("capacity")
    return DeploymentRecord(
        name=str(raw.get("name") or ""),
        model=model,
        model_version=str(raw["model_version"]) if raw.get("model_version") else None,
        sku=str(raw.get("sku") or ""),
        capacity=int(capacity) if isinstance(capacity, (int, float)) else None,
        deployment_mode=deployment_mode_for_sku(raw.get("sku")),  # type: ignore[arg-type]
        provider_family=family,
        inference_api=inference_api_for_family(family),
        publisher=publisher,
    )


def discover_deployments(
    resources: Any,
    subscription_id: str,
    resource_group: str,
    account: str,
) -> list[DeploymentRecord]:
    """Return the account's deployment inventory with deterministic metadata."""
    inventory = list(resources.list_deployments(subscription_id, resource_group, account))
    records = [enrich_deployment(entry) for entry in inventory if entry.get("name")]
    return sorted(records, key=lambda item: item.name.casefold())


def account_endpoint(metadata: dict[str, Any]) -> str | None:
    """Read the account's base services endpoint from ARM metadata."""
    candidate = metadata.get("endpoint")
    if not candidate:
        endpoints = metadata.get("endpoints")
        if isinstance(endpoints, dict):
            for key in ("Azure AI Model Inference API", "OpenAI Language Model Instance API", "Token Service Endpoint"):
                if isinstance(endpoints.get(key), str):
                    candidate = endpoints[key]
                    break
            else:
                candidate = next((value for value in endpoints.values() if isinstance(value, str)), None)
    try:
        return normalize_endpoint(candidate if isinstance(candidate, str) else None)
    except WorkflowError:
        return None


def environment_endpoint() -> str | None:
    """Last-resort endpoint from the documented environment variables."""
    for name in ("FOUNDRY_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT", "AZURE_OPENAI_ENDPOINT"):
        value = os.getenv(name)
        if value:
            try:
                return normalize_endpoint(value)
            except WorkflowError:
                continue
    return None


def canonical_model_keys(deployments: Iterable[DeploymentRecord]) -> list[str]:
    """Canonical model keys for the selected deployments, without duplicates."""
    seen: dict[str, None] = {}
    for deployment in deployments:
        if deployment.model:
            seen.setdefault(canonical_model_name(deployment.model), None)
    return list(seen)


def selected_deployments(
    inventory: Sequence[DeploymentRecord],
    names: Sequence[str],
) -> tuple[list[DeploymentRecord], list[str]]:
    """Resolve requested deployment names against the discovered inventory."""
    by_name = {item.name.casefold(): item for item in inventory}
    resolved: list[DeploymentRecord] = []
    missing: list[str] = []
    for name in names:
        match = by_name.get(str(name).casefold())
        if match is None:
            missing.append(str(name))
        else:
            resolved.append(match)
    return resolved, missing
