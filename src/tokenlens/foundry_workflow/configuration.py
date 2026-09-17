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
from typing import Any, Sequence

import yaml

from ..workloads import WorkloadIdentity, WorkloadMapping, merge_identities
from .models import (
    CONFIG_VERSION,
    CollectionSettings,
    DeploymentRecord,
    FoundryAccountTarget,
    FoundryTarget,
    FoundryWorkflowConfig,
    PricingSettings,
    ReportSettings,
    RunState,
    WorkflowError,
    WorkloadSettings,
    normalize_analysis_goal,
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
    "load_subscription_map",
    "run_state_path",
    "atomic_write",
    "safe_display_path",
    "target_path",
    "save_config",
    "save_run_state",
    "save_subscription",
    "save_subscription_map",
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


def _read_target_document() -> dict[str, Any]:
    path = target_path()
    if not path.is_file():
        return {}
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_subscription() -> str | None:
    payload = _read_target_document()
    value = payload.get("subscription_id")
    return str(value) if value else None


def load_subscription_ids() -> list[str]:
    """Every subscription remembered for the saved scope, user-locally."""
    payload = _read_target_document()
    values = payload.get("subscription_ids")
    resolved = [str(item) for item in values if item] if isinstance(values, list) else []
    primary = payload.get("subscription_id")
    if primary and str(primary) not in resolved:
        resolved.insert(0, str(primary))
    return resolved


def load_subscription_map() -> dict[str, str]:
    """``resource-group/account`` -> subscription ID, remembered user-locally.

    Account names live in ``.tokenlens.yml``; the subscription they belong to is
    a tenant identifier and is therefore kept outside the working tree.
    """
    payload = _read_target_document()
    mapping = payload.get("accounts")
    if not isinstance(mapping, dict):
        return {}
    return {str(key): str(value) for key, value in mapping.items() if key and value}


def save_subscription(subscription_id: str | None) -> Path | None:
    if not subscription_id:
        return None
    return _write_target_document({**_read_target_document(), "subscription_id": subscription_id})


def save_subscription_map(
    subscription_id: str | None,
    *,
    accounts: dict[str, str] | None = None,
    subscription_ids: Sequence[str] | None = None,
) -> Path | None:
    """Remember the primary subscription, the scope, and the per-account map."""
    document = _read_target_document()
    if subscription_id:
        document["subscription_id"] = subscription_id
    if subscription_ids is not None:
        document["subscription_ids"] = [str(item) for item in dict.fromkeys(subscription_ids) if item]
    if accounts is not None:
        merged = {**load_subscription_map(), **{str(k): str(v) for k, v in accounts.items() if k and v}}
        document["accounts"] = merged
    if not document:
        return None
    document["version"] = 2
    return _write_target_document(document)


def _write_target_document(document: dict[str, Any]) -> Path:
    import json

    path = target_path()
    _atomic_write(path, json.dumps(document, indent=2, sort_keys=True) + "\n", mode=0o600)
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
    """Migrate a v1 or v2 configuration block onto the v3 workflow shape.

    Version 1 stored deployments as bare names and the lookback under
    ``monitor``. Version 2 stored exactly one account inline. Migration keeps
    every name, marks undiscovered identity as such, normalizes the analysis
    goal to the full assessment, and leaves every unrelated key untouched.
    """
    foundry = payload.get("foundry") if isinstance(payload.get("foundry"), dict) else {}
    monitor = payload.get("monitor") if isinstance(payload.get("monitor"), dict) else {}
    collection = payload.get("collection") if isinstance(payload.get("collection"), dict) else {}
    migrated = _migrate_deployments(foundry.get("deployments"))
    accounts_raw = foundry.get("accounts")
    accounts: list[dict[str, Any]] = []
    if isinstance(accounts_raw, list):
        for entry in accounts_raw:
            if not isinstance(entry, dict) or not entry.get("account"):
                continue
            accounts.append(
                {
                    **{key: value for key, value in entry.items() if key != "deployments"},
                    "deployments": _migrate_deployments(entry.get("deployments")),
                }
            )
    if not accounts and foundry.get("account") and foundry.get("resource_group"):
        # Version 2: one inline account becomes the first (and only) target.
        accounts.append(
            {
                "resource_group": foundry["resource_group"],
                "account": foundry["account"],
                "region": foundry.get("region"),
                "metrics_endpoint": foundry.get("metrics_endpoint"),
                "deployments": migrated,
            }
        )
    result = dict(payload)
    result["foundry"] = {**foundry, "deployments": migrated, "accounts": accounts}
    merged_collection = {
        "lookback_days": int(collection.get("lookback_days", monitor.get("lookback_days", 14)) or 14),
        "granularity_minutes": int(
            collection.get("granularity_minutes", monitor.get("granularity_minutes", 5)) or 5
        ),
        **{key: value for key, value in collection.items() if key not in {"lookback_days", "granularity_minutes"}},
    }
    merged_collection["analysis_goal"] = normalize_analysis_goal(merged_collection.get("analysis_goal"))
    result["collection"] = merged_collection
    return result


def _migrate_deployments(raw: object) -> list[dict[str, Any]]:
    migrated: list[dict[str, Any]] = []
    for entry in raw or []:  # type: ignore[union-attr]
        if isinstance(entry, str):
            migrated.append({"name": entry})
        elif isinstance(entry, dict) and entry.get("name"):
            migrated.append(dict(entry))
    return migrated


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
    primary_subscription = foundry_raw.get("subscription_id") or load_subscription()
    subscription_by_account = load_subscription_map()
    accounts: list[FoundryAccountTarget] = []
    for entry in foundry_raw.get("accounts") or []:
        if not isinstance(entry, dict) or not entry.get("account"):
            continue
        key = f"{entry.get('resource_group') or ''}/{entry.get('account')}"
        accounts.append(
            FoundryAccountTarget(
                # The subscription is never in the tracked file: it is restored
                # from the user-local map, then from the primary subscription.
                subscription_id=(
                    entry.get("subscription_id") or subscription_by_account.get(key) or primary_subscription
                ),
                resource_group=str(entry.get("resource_group") or ""),
                account=str(entry["account"]),
                region=entry.get("region"),
                metrics_endpoint=entry.get("metrics_endpoint"),
                deployments=[
                    DeploymentRecord.model_validate(item)
                    for item in entry.get("deployments") or []
                    if isinstance(item, dict) and item.get("name")
                ],
            )
        )
    try:
        config = FoundryWorkflowConfig(
            version=int(migrated.get("version", CONFIG_VERSION) or CONFIG_VERSION),
            foundry=FoundryTarget(
                # A legacy in-file value is still honoured so an existing
                # configuration keeps working; new writes never add one.
                subscription_id=primary_subscription,
                subscription_id_env=str(foundry_raw.get("subscription_id_env") or "AZURE_SUBSCRIPTION_ID"),
                subscription_ids=load_subscription_ids(),
                scope=str(foundry_raw.get("scope") or "account"),  # type: ignore[arg-type]
                resource_group=foundry_raw.get("resource_group"),
                account=foundry_raw.get("account"),
                region=foundry_raw.get("region"),
                metrics_endpoint=foundry_raw.get("metrics_endpoint"),
                inference_endpoint=foundry_raw.get("inference_endpoint"),
                deployments=deployments,
                accounts=accounts,
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
            "subscription_ids",
            "scope",
            "resource_group",
            "account",
            "region",
            "metrics_endpoint",
            "inference_endpoint",
            "deployments",
            "accounts",
        }
    }
    foundry["subscription_id_env"] = config.foundry.subscription_id_env
    foundry["scope"] = config.foundry.scope
    targets = config.foundry.targets
    primary = targets[0] if targets else None
    # The subscription is a tenant identifier and is stored user-locally only.
    for key, value in (
        ("resource_group", primary.resource_group if primary else config.foundry.resource_group),
        ("account", primary.account if primary else config.foundry.account),
        ("region", primary.region if primary else config.foundry.region),
        ("metrics_endpoint", config.foundry.metrics_endpoint),
        ("inference_endpoint", config.foundry.inference_endpoint),
    ):
        if value:
            foundry[key] = value
    save_subscription_map(
        config.foundry.subscription_id,
        accounts={
            item.key: item.subscription_id for item in targets if item.subscription_id
        },
        subscription_ids=config.foundry.subscription_ids
        or [item.subscription_id for item in targets if item.subscription_id],
    )
    # The flat deployment list stays for readers of the version 2 shape; the
    # accounts block is the multi-account source of truth.
    foundry["deployments"] = [
        item.model_dump(mode="json", exclude_none=True)
        for item in (primary.deployments if primary else config.foundry.deployments)
    ]
    foundry["accounts"] = [
        {
            **item.model_dump(mode="json", exclude_none=True, exclude={"subscription_id", "deployments"}),
            "deployments": [
                deployment.model_dump(mode="json", exclude_none=True) for deployment in item.deployments
            ],
        }
        for item in targets
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
