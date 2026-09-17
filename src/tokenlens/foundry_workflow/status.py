"""``foundry status`` and ``workloads status`` rendering.

Nothing here prints a credential, an access token, a tenant secret, a full
endpoint, a request ID, or a complete local source path.
"""

from __future__ import annotations

import os
from typing import Any

from ..workloads import canonical_workload_id
from .configuration import (
    config_path,
    customer_catalog_path,
    load_config,
    load_run_state,
    run_state_path,
    safe_display_path,
)
from .models import FoundryWorkflowConfig
from .orchestration import install_command, missing_collector_packages
from .pricing import load_customer_catalog, pricing_readiness

__all__ = ["status_payload", "status_lines", "workloads_payload", "workloads_lines"]


def _azure_ready() -> str:
    missing = missing_collector_packages()
    if missing:
        return "extras missing"
    try:  # pragma: no cover - requires the optional Azure SDK
        from azure.identity import DefaultAzureCredential

        DefaultAzureCredential(exclude_interactive_browser_credential=True)
        return "credential chain available"
    except Exception:  # noqa: BLE001 - reported, never fatal
        return "not available; run az login and verify your role"


def status_payload(*, path: str | None = None) -> dict[str, Any]:
    """Machine-readable status for scripting and JSON output."""
    config, _raw = load_config(path)
    state = load_run_state()
    missing = missing_collector_packages()
    customer = load_customer_catalog()
    readiness = pricing_readiness(config.foundry.all_deployments, customer_catalog=customer)
    resolved = [item for item in readiness if item.resolved]
    return {
        "configuration": safe_display_path(config_path(path)),
        "configured": config.configured,
        "azure-authentication": _azure_ready(),
        # The subscription ID is an Azure identifier: only its shortened form is
        # printed unless the user asks for the full value elsewhere.
        "subscription": _short(config.foundry.subscription_id),
        "scope": config.foundry.scope,
        "accounts": [item.account for item in config.foundry.targets],
        "resource-group": config.foundry.resource_group or "not configured",
        "account": config.foundry.account or "not configured",
        "region": config.foundry.region or "not configured",
        "deployments": config.foundry.deployment_names,
        "lookback-days": config.collection.lookback_days,
        "analysis-goal": config.collection.analysis_goal,
        "last-collection": state.last_collection or "never",
        "collection-status": state.collection_status,
        "last-run-directory": state.run_directory or "none",
        "last-report": state.report or "none",
        "model-identity-coverage-percent": state.identity_coverage_percent,
        "pricing-coverage-percent": state.pricing_coverage_percent,
        "business-identity-coverage-percent": state.business_identity_coverage_percent,
        "pricing-readiness": {item.deployment: item.label for item in readiness},
        "pricing-resolved": f"{len(resolved)}/{len(readiness)}",
        "ptu-evidence": (
            "collected buckets required; run a collection first"
            if state.last_collection is None
            else f"{state.lookback_days or config.collection.lookback_days}-day window collected"
        ),
        "missing-optional-dependencies": missing,
        "install-command": install_command() if missing else "",
        "customer-pricing-catalog": (
            safe_display_path(customer_catalog_path()) if customer is not None else "not configured"
        ),
        "run-state": safe_display_path(run_state_path()),
    }


def _short(value: str | None) -> str:
    if not value:
        return "not configured"
    return f"{value[:8]}…{value[-4:]}" if len(value) > 14 else value


def status_lines(*, path: str | None = None) -> list[str]:
    payload = status_payload(path=path)
    lines: list[str] = []
    for key, value in payload.items():
        if value == "" or value == []:
            continue
        if isinstance(value, list):
            lines.append(f"{key}={', '.join(str(item) for item in value)}")
        elif isinstance(value, dict):
            for inner_key, inner_value in value.items():
                lines.append(f"{key}.{inner_key}={inner_value}")
        else:
            lines.append(f"{key}={value}")
    return lines


def workloads_payload(config: FoundryWorkflowConfig | None = None, *, path: str | None = None) -> dict[str, Any]:
    """Technical and business workload configuration status."""
    if config is None:
        config, _raw = load_config(path)
    mappings = config.workloads.mappings
    dedicated = {
        canonical_workload_id(deployment)
        for mapping in mappings
        if mapping.allocation == "dedicated"
        for deployment in mapping.deployments
    }
    shared = {
        canonical_workload_id(deployment)
        for mapping in mappings
        if mapping.allocation == "shared"
        for deployment in mapping.deployments
    }
    known = {canonical_workload_id(item.name) for item in config.foundry.all_deployments}
    stale = sorted(
        deployment
        for mapping in mappings
        for deployment in mapping.deployments
        if canonical_workload_id(deployment) not in known
    )
    technical = [item for item in config.workloads.identities if item.workload_scope == "technical"]
    business = [item for item in config.workloads.identities if item.workload_scope == "business"]
    return {
        "technical-workloads": len(technical),
        "technical-coverage": (
            f"{len([item for item in technical if not item.stale])}/{len(config.foundry.all_deployments)} deployments"
        ),
        "business-workloads": len(business),
        "business-identity-configured": f"{len(dedicated | shared)}/{len(known)} deployments",
        "shared-deployments-needing-request-tags": sorted(shared),
        "deployments-needing-configuration": sorted(
            item.name
            for item in config.foundry.all_deployments
            if canonical_workload_id(item.name) not in (dedicated | shared)
        ),
        "stale-mappings": stale,
    }


def workloads_lines(config: FoundryWorkflowConfig | None = None, *, path: str | None = None) -> list[str]:
    payload = workloads_payload(config, path=path)
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, list):
            lines.append(f"{key}={', '.join(value) if value else 'none'}")
        else:
            lines.append(f"{key}={value}")
    return lines
