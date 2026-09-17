"""Configuration and run-state persistence for the guided Foundry workflow.

Writes are atomic, schema validated, and credential free. Unrelated keys in an
existing ``.tokenlens.yml`` are preserved: the workflow only owns the
``foundry``, ``collection``, ``pricing``, ``report``, and ``workloads`` blocks
it writes, and it merges rather than replaces the file.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ..workloads import WorkloadIdentity, WorkloadMapping, merge_identities
from .models import (
    CONFIG_VERSION,
    CollectionSettings,
    DeploymentRecord,
    FoundryTarget,
    FoundryWorkflowConfig,
    PricingSettings,
    ReportSettings,
    RunState,
    WorkflowError,
    WorkloadSettings,
)

__all__ = [
    "CONFIG_PATH",
    "config_path",
    "customer_catalog_path",
    "load_config",
    "load_run_state",
    "migrate_mapping",
    "pricing_cache_dir",
    "refresh_identities",
    "load_subscription",
    "run_state_path",
    "atomic_write",
    "safe_display_path",
    "target_path",
    "save_config",
    "save_run_state",
    "save_subscription",
    "user_config_dir",
]

CONFIG_PATH = ".tokenlens.yml"
RUN_STATE_NAME = "foundry-run-state.json"


def config_path(explicit: str | None = None) -> Path:
    return Path(explicit) if explicit else Path(CONFIG_PATH)


def user_config_dir() -> Path:
    """Platform-appropriate, user-only application data directory.

    ``TOKENLENS_CONFIG_DIR`` overrides it so tests and sandboxes never touch a
    real user profile.
    """
    override = os.getenv("TOKENLENS_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "tokenlens"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "tokenlens"
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "tokenlens"


def pricing_cache_dir() -> Path:
    return user_config_dir() / "pricing"


def customer_catalog_path() -> Path:
    return pricing_cache_dir() / "customer.yml"


def run_state_path() -> Path:
    return user_config_dir() / RUN_STATE_NAME


def safe_display_path(path: Path | str) -> str:
    """Render a path without leaking a home directory or username."""
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(Path.cwd().resolve()))
    except (ValueError, OSError):
        pass
    try:
        return str(Path("~") / candidate.resolve().relative_to(Path.home()))
    except (ValueError, OSError, RuntimeError):
        return candidate.name


def _atomic_write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise



TARGET_NAME = "foundry-target.json"


def target_path() -> Path:
    """User-local file holding the remembered subscription.

    The subscription ID is an Azure tenant identifier, so it is deliberately
    kept out of the repository-adjacent ``.tokenlens.yml`` — that file can be
    tracked by git, and the plan forbids committing a real subscription. The
    workflow still remembers the selection, just outside the working tree with
    user-only permissions.
    """
    return user_config_dir() / TARGET_NAME


def load_subscription() -> str | None:
    path = target_path()
    if not path.is_file():
        return None
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("subscription_id") if isinstance(payload, dict) else None
    return str(value) if value else None


def save_subscription(subscription_id: str | None) -> Path | None:
    if not subscription_id:
        return None
    import json

    path = target_path()
    _atomic_write(path, json.dumps({"subscription_id": subscription_id}, indent=2) + "\n", mode=0o600)
    return path


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise WorkflowError(f"{path.name} is not valid YAML ({type(exc).__name__}).") from exc
    if not isinstance(payload, dict):
        raise WorkflowError(f"{path.name} must contain a YAML mapping.")
    return payload


def migrate_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    """Migrate a v1 configuration block onto the v2 workflow shape.

    Version 1 stored deployments as bare names and the lookback under
    ``monitor``. Migration keeps the names, marks their identity as
    undiscovered, and leaves every unrelated key untouched.
    """
    foundry = payload.get("foundry") if isinstance(payload.get("foundry"), dict) else {}
    monitor = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else {}
    collection = payload.get("collection") if isinstance(payload.get("collection"), dict) else {}
    deployments = foundry.get("deployments") or []
    migrated: list[dict[str, Any]] = []
    for entry in deployments:
        if isinstance(entry, str):
            migrated.append({"name": entry})
        elif isinstance(entry, dict) and entry.get("name"):
            migrated.append(dict(entry))
    result = dict(payload)
    result["foundry"] = {**foundry, "deployments": migrated}
    merged_collection = {
        "lookback_days": int(collection.get("lookback_days", monitor.get("lookback_days", 14)) or 14),
        "granularity_minutes": int(
            collection.get("granularity_minutes", monitor.get("granularity_minutes", 5)) or 5
        ),
        **{key: value for key, value in collection.items() if key not in {"lookback_days", "granularity_minutes"}},
    }
    result["collection"] = merged_collection
    return result


def load_config(path: str | None = None) -> tuple[FoundryWorkflowConfig, dict[str, Any]]:
    """Load the workflow configuration and the untouched raw document.

    The raw document is returned so unrelated keys survive the next write.
    """
    target = config_path(path)
    raw = _read_mapping(target)
    migrated = migrate_mapping(raw) if raw else {}
    foundry_raw = dict(migrated.get("foundry") or {})
    deployments = [
        DeploymentRecord.model_validate(entry)
        for entry in foundry_raw.get("deployments", [])
        if isinstance(entry, dict) and entry.get("name")
    ]
    workloads_raw = migrated.get("workloads") if isinstance(migrated.get("workloads"), dict) else {}
    report_raw = migrated.get("report") if isinstance(migrated.get("report"), dict) else {}
    pricing_raw = migrated.get("pricing") if isinstance(migrated.get("pricing"), dict) else {}
    try:
        config = FoundryWorkflowConfig(
            version=int(migrated.get("version", CONFIG_VERSION) or CONFIG_VERSION),
            foundry=FoundryTarget(
                # A legacy in-file value is still honoured so an existing
                # configuration keeps working; new writes never add one.
                subscription_id=foundry_raw.get("subscription_id") or load_subscription(),
                subscription_id_env=str(foundry_raw.get("subscription_id_env") or "AZURE_SUBSCRIPTION_ID"),
                resource_group=foundry_raw.get("resource_group"),
                account=foundry_raw.get("account"),
                region=foundry_raw.get("region"),
                metrics_endpoint=foundry_raw.get("metrics_endpoint"),
                inference_endpoint=foundry_raw.get("inference_endpoint"),
                deployments=deployments,
            ),
            collection=CollectionSettings.model_validate(migrated.get("collection") or {}),
            pricing=PricingSettings(
                public_cache=bool(pricing_raw.get("public_cache", True)),
                customer_catalog=(
                    str(pricing_raw["customer_catalog"]) if pricing_raw.get("customer_catalog") else None
                ),
                use_reference_catalog=bool(pricing_raw.get("use_reference_catalog", True)),
            ),
            report=ReportSettings(
                format=str(report_raw.get("format") or "html"),  # type: ignore[arg-type]
                output_dir=str(report_raw.get("output_dir") or "reports"),
                open=bool(report_raw.get("open", True)),
            ),
            workloads=WorkloadSettings(
                mappings=[
                    WorkloadMapping.model_validate(entry)
                    for entry in (workloads_raw.get("mappings") or [])
                    if isinstance(entry, dict)
                ],
                identities=[
                    WorkloadIdentity.model_validate(entry)
                    for entry in (workloads_raw.get("identities") or [])
                    if isinstance(entry, dict)
                ],
            ),
        )
    except ValueError as exc:
        raise WorkflowError(f"The saved Foundry configuration is invalid: {exc}") from exc
    return config, raw


def save_config(config: FoundryWorkflowConfig, raw: dict[str, Any] | None = None, *, path: str | None = None) -> Path:
    """Write the workflow blocks atomically without clobbering other keys."""
    target = config_path(path)
    document = dict(raw or {})
    document["version"] = max(int(document.get("version", 1) or 1), CONFIG_VERSION)
    existing_foundry = document.get("foundry") if isinstance(document.get("foundry"), dict) else {}
    foundry = {
        key: value
        for key, value in existing_foundry.items()
        if key not in {
            "subscription_id",
            "subscription_id_env",
            "resource_group",
            "account",
            "region",
            "metrics_endpoint",
            "inference_endpoint",
            "deployments",
        }
    }
    foundry["subscription_id_env"] = config.foundry.subscription_id_env
    # The subscription is a tenant identifier and is stored user-locally only.
    for key in ("resource_group", "account", "region", "metrics_endpoint", "inference_endpoint"):
        value = getattr(config.foundry, key)
        if value:
            foundry[key] = value
    save_subscription(config.foundry.subscription_id)
    foundry["deployments"] = [
        item.model_dump(mode="json", exclude_none=True) for item in config.foundry.deployments
    ]
    document["foundry"] = foundry
    document["collection"] = config.collection.model_dump(mode="json")
    existing_pricing = document.get("pricing") if isinstance(document.get("pricing"), dict) else {}
    document["pricing"] = {**existing_pricing, **config.pricing.model_dump(mode="json", exclude_none=True)}
    existing_report = document.get("report") if isinstance(document.get("report"), dict) else {}
    document["report"] = {**existing_report, **config.report.model_dump(mode="json")}
    document["workloads"] = {
        "defaults": config.workloads.defaults.model_dump(mode="json"),
        "mappings": [item.model_dump(mode="json", exclude_none=True) for item in config.workloads.mappings],
        "identities": [
            item.model_dump(mode="json", exclude_none=True) for item in config.workloads.identities
        ],
    }
    _atomic_write(target, yaml.safe_dump(document, sort_keys=False), mode=0o600)
    return target


def refresh_identities(config: FoundryWorkflowConfig, *, account_scope: str) -> FoundryWorkflowConfig:
    """Regenerate technical workloads from discovery, preserving enrichment."""
    identities = merge_identities(
        config.foundry.deployment_names,
        mappings=config.workloads.mappings,
        existing=config.workloads.identities,
        account_scope=account_scope,
    )
    config.workloads.identities = identities
    return config


#: Public alias: other workflow modules write user-local files atomically too.
atomic_write = _atomic_write


def load_run_state() -> RunState:
    path = run_state_path()
    if not path.is_file():
        return RunState()
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        return RunState.model_validate(payload)
    except (OSError, ValueError):
        return RunState()


def save_run_state(state: RunState) -> Path:
    import json

    path = run_state_path()
    _atomic_write(path, json.dumps(state.model_dump(mode="json"), indent=2) + "\n", mode=0o600)
    return path
