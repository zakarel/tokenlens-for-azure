"""Typed models for the guided Foundry workflow.

Everything here is credential-free by construction: there is no field for a
key, token, connection string, or secret, and the run state deliberately omits
full endpoints, request IDs, tenant values, and absolute local paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..workloads import WorkloadIdentity, WorkloadMapping

#: Version 3 stores every selected Foundry account in ``foundry.accounts`` so a
#: run can span several accounts and subscriptions. Version 2 documents survive
#: unchanged: their single account migrates into the first (and only) entry.
CONFIG_VERSION = 3

#: Canonical deployment modes. ``unknown`` is a real state: PTU sizing and
#: pricing differ per mode, so a missing mode is never defaulted to Global.
DeploymentMode = Literal[
    "global",
    "regional",
    "data_zone",
    "global_provisioned",
    "regional_provisioned",
    "data_zone_provisioned",
    "batch",
    "unknown",
]

ProviderFamily = Literal["azure_openai", "claude_foundry", "partner_model", "unknown"]
InferenceApi = Literal["openai", "anthropic", "unknown"]

#: TokenLens always runs the full assessment. Cost, PTU suitability, and prompt
#: efficiency answer different questions from the same collected window, so
#: asking the user to pick one up front only removed evidence from the report.
#: The literal is kept single-valued so a legacy document normalizes on load.
AnalysisGoal = Literal["full_assessment"]

#: Legacy analysis goals that normalize to ``full_assessment`` on load.
LEGACY_ANALYSIS_GOALS = ("workload_cost", "ptu_suitability", "prompt_efficiency")


def normalize_analysis_goal(value: object) -> AnalysisGoal:
    """Normalize any stored or supplied analysis goal to ``full_assessment``."""
    return "full_assessment"


#: How the run was scoped. ``account`` is one explicitly chosen account,
#: ``subscription`` is every Foundry account in one subscription, and
#: ``all_subscriptions`` is every Foundry account the principal can read.
TargetScope = Literal["account", "subscription", "all_subscriptions"]

#: Lookback choices with the exact purpose of each, so a period is chosen for a
#: reason instead of by habit.
LOOKBACK_CHOICES: tuple[tuple[int, str], ...] = (
    (1, "Connectivity and metric-shape check"),
    (7, "Preliminary operational view"),
    (14, "Recommended default"),
    (30, "Stronger workload evidence"),
    (90, "Long-term seasonality, bounded by service limits"),
)


class WorkflowError(RuntimeError):
    """Raised when the guided workflow cannot proceed safely."""


class NoninteractiveError(WorkflowError):
    """Raised when a required value is missing and prompting is not allowed."""


class SubscriptionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    name: str
    tenant_name: str | None = None
    is_default: bool = False

    @property
    def short_id(self) -> str:
        """Shortened identifier; the full ID is printed only on request."""
        value = self.subscription_id
        return f"{value[:8]}…{value[-4:]}" if len(value) > 14 else value

    @property
    def display_name(self) -> str:
        """A name safe to print. A raw ID is shortened, never echoed in full."""
        return self.short_id if self.name == self.subscription_id else self.name

    def label(self, *, full_id: bool = False) -> str:
        tail = self.subscription_id if full_id else self.short_id
        tenant = f" · {self.tenant_name}" if self.tenant_name else ""
        marker = " (Azure CLI active)" if self.is_default else ""
        return f"{self.display_name} · {tail}{tenant}{marker}"


class AccountOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    resource_group: str
    location: str = ""
    kind: str = ""
    #: The subscription the account was discovered in. It is never written into
    #: ``.tokenlens.yml``; only the user-local target file remembers it.
    subscription_id: str = ""
    subscription_name: str = ""

    @property
    def key(self) -> str:
        return f"{self.resource_group}/{self.name}"

    def label(self) -> str:
        region = self.location or "region unknown"
        subscription = f" · {self.subscription_name}" if self.subscription_name else ""
        return f"{self.name} — {region} · {self.resource_group} · {self.kind or 'kind unknown'}{subscription}"


class DeploymentRecord(BaseModel):
    """One discovered deployment with exact, provenance-carrying identity."""

    model_config = ConfigDict(extra="forbid")

    name: str
    model: str = ""
    model_version: str | None = None
    sku: str = ""
    capacity: int | None = None
    deployment_mode: DeploymentMode = "unknown"
    provider_family: ProviderFamily = "unknown"
    inference_api: InferenceApi = "unknown"
    publisher: str | None = None

    @property
    def mode_label(self) -> str:
        return DEPLOYMENT_MODE_LABELS.get(self.deployment_mode, "Unknown")

    def label(self) -> str:
        version = f" v{self.model_version}" if self.model_version else ""
        return f"{self.name}   {self.model or 'model unknown'}{version}   {self.mode_label}"


DEPLOYMENT_MODE_LABELS: dict[str, str] = {
    "global": "Global Standard",
    "regional": "Regional Standard",
    "data_zone": "Data Zone Standard",
    "global_provisioned": "Global Provisioned",
    "regional_provisioned": "Regional Provisioned",
    "data_zone_provisioned": "Data Zone Provisioned",
    "batch": "Batch",
    "unknown": "Unknown",
}


class CollectionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lookback_days: int = Field(default=14, ge=1, le=90)
    granularity_minutes: int = 5
    output_dir: str = "local-traces/foundry-metrics"
    #: Optional local directory of task-economics events. When present, task
    #: metrics drill down beneath the workloads they are tagged with.
    task_events_dir: str | None = None
    max_concurrency: int = Field(default=3, ge=1, le=8)
    #: Every guided run writes into ``output_dir/runs/<run id>`` so a report is
    #: built from the current run only and historical telemetry is never mixed
    #: in or deleted.
    isolate_runs: bool = True
    analysis_goal: AnalysisGoal = "full_assessment"

    @field_validator("analysis_goal", mode="before")
    @classmethod
    def _normalize_goal(cls, value: object) -> AnalysisGoal:
        return normalize_analysis_goal(value)


class PricingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    public_cache: bool = True
    customer_catalog: str | None = None
    use_reference_catalog: bool = True


class ReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["html", "json"] = "html"
    output_dir: str = "reports"
    open: bool = True


class WorkloadDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Report completeness depends on a technical workload per deployment, so
    #: this cannot be disabled in v1.
    create_for_each_deployment: Literal[True] = True
    scope: Literal["technical"] = "technical"
    type: Literal["ai_deployment"] = "ai_deployment"


class WorkloadSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: WorkloadDefaults = Field(default_factory=WorkloadDefaults)
    mappings: list[WorkloadMapping] = Field(default_factory=list)
    identities: list[WorkloadIdentity] = Field(default_factory=list)


class FoundryAccountTarget(BaseModel):
    """One selected Foundry account and the deployments collected from it."""

    model_config = ConfigDict(extra="forbid")

    #: Resolved at load time from the user-local target file. It is never
    #: serialized into ``.tokenlens.yml``, which can be tracked by git.
    subscription_id: str | None = None
    resource_group: str
    account: str
    region: str | None = None
    metrics_endpoint: str | None = None
    deployments: list[DeploymentRecord] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.resource_group}/{self.account}"

    @property
    def deployment_names(self) -> list[str]:
        return [item.name for item in self.deployments]

    @property
    def complete(self) -> bool:
        return bool(self.subscription_id and self.resource_group and self.account and self.deployments)

    def label(self) -> str:
        return f"{self.account} · {self.resource_group} · {self.region or 'region unknown'}"


class FoundryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_id: str | None = None
    subscription_id_env: str = "AZURE_SUBSCRIPTION_ID"
    #: Every subscription in scope, remembered user-locally only.
    subscription_ids: list[str] = Field(default_factory=list)
    scope: TargetScope = "account"
    resource_group: str | None = None
    account: str | None = None
    region: str | None = None
    #: Explicit override for sovereign or unusual clouds only. Discovery is
    #: preferred, and no key is ever stored beside it.
    metrics_endpoint: str | None = None
    inference_endpoint: str | None = None
    deployments: list[DeploymentRecord] = Field(default_factory=list)
    #: Version 3: every selected account. A version 2 document with a single
    #: account is migrated into exactly one entry on load.
    accounts: list[FoundryAccountTarget] = Field(default_factory=list)

    @property
    def targets(self) -> list[FoundryAccountTarget]:
        """Every account to collect, whether the config is v2-shaped or v3."""
        if self.accounts:
            return list(self.accounts)
        if self.resource_group and self.account:
            return [
                FoundryAccountTarget(
                    subscription_id=self.subscription_id,
                    resource_group=self.resource_group,
                    account=self.account,
                    region=self.region,
                    metrics_endpoint=self.metrics_endpoint,
                    deployments=list(self.deployments),
                )
            ]
        return []

    @property
    def all_deployments(self) -> list[DeploymentRecord]:
        """Deployment records across every selected account, in target order."""
        targets = self.targets
        if len(targets) == 1:
            return list(targets[0].deployments)
        records: list[DeploymentRecord] = []
        for target in targets:
            records.extend(target.deployments)
        return records

    @property
    def deployment_names(self) -> list[str]:
        seen: dict[str, None] = {}
        for item in self.all_deployments:
            seen.setdefault(item.name, None)
        return list(seen)

    @property
    def complete(self) -> bool:
        targets = self.targets
        return bool(targets) and all(item.complete for item in targets)


class FoundryWorkflowConfig(BaseModel):
    """Versioned, credential-free workflow configuration."""

    model_config = ConfigDict(extra="forbid")

    version: int = CONFIG_VERSION
    foundry: FoundryTarget = Field(default_factory=FoundryTarget)
    collection: CollectionSettings = Field(default_factory=CollectionSettings)
    pricing: PricingSettings = Field(default_factory=PricingSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)
    workloads: WorkloadSettings = Field(default_factory=WorkloadSettings)

    @property
    def configured(self) -> bool:
        return self.foundry.complete


class DeploymentOutcome(BaseModel):
    """Per-deployment collection outcome; one failure never hides the others."""

    model_config = ConfigDict(extra="forbid")

    deployment: str
    account: str | None = None
    resource_group: str | None = None
    status: Literal["succeeded", "failed", "skipped"] = "succeeded"
    error_category: str | None = None
    message: str | None = None
    identity_resolved: bool = False
    metrics_available: bool = False
    active_buckets: int = 0
    requests: int | None = None
    tokens: int = 0
    pricing_status: str = "unresolved"
    ptu_evidence: str = "insufficient"
    records_written: int = 0
    duplicates_skipped: int = 0

    @property
    def key(self) -> str:
        return f"{self.account}/{self.deployment}" if self.account else self.deployment


class CollectionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcomes: list[DeploymentOutcome] = Field(default_factory=list)
    lookback_days: int = 14
    window_start: str | None = None
    window_end: str | None = None
    output_dir: str = ""
    #: Relative directory this run wrote to. The report is built from it alone,
    #: so a stale file in the base directory can never re-enter a fresh report.
    run_dir: str = ""
    run_id: str = ""
    accounts: list[str] = Field(default_factory=list)
    records_written: int = 0
    duplicates_skipped: int = 0
    identity_conflicts: list[str] = Field(default_factory=list)
    unresolved_pricing: list[str] = Field(default_factory=list)

    @property
    def succeeded(self) -> list[DeploymentOutcome]:
        return [item for item in self.outcomes if item.status == "succeeded"]

    @property
    def failed(self) -> list[DeploymentOutcome]:
        return [item for item in self.outcomes if item.status == "failed"]

    @property
    def status(self) -> Literal["succeeded", "partial", "failed", "empty"]:
        if not self.outcomes:
            return "empty"
        if not self.succeeded:
            return "failed"
        return "succeeded" if not self.failed else "partial"


class RunState(BaseModel):
    """Non-sensitive run state written beside the configuration.

    It never stores access tokens, tenant secrets, prompts, responses, request
    IDs, full endpoints, or absolute paths containing a username.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    last_collection: str | None = None
    lookback_days: int | None = None
    successful_deployments: int = 0
    failed_deployments: int = 0
    accounts_collected: int = 0
    identity_coverage_percent: float = 0.0
    pricing_coverage_percent: float = 0.0
    business_identity_coverage_percent: float = 0.0
    workloads_needing_configuration: int = 0
    report: str | None = None
    report_generated_at: str | None = None
    #: Relative run directory the report was built from.
    run_directory: str | None = None
    collection_status: str = "empty"

    @classmethod
    def now(cls) -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = [
    "AccountOption",
    "AnalysisGoal",
    "CONFIG_VERSION",
    "CollectionSettings",
    "CollectionSummary",
    "DEPLOYMENT_MODE_LABELS",
    "DeploymentMode",
    "DeploymentOutcome",
    "DeploymentRecord",
    "FoundryAccountTarget",
    "FoundryTarget",
    "FoundryWorkflowConfig",
    "InferenceApi",
    "LEGACY_ANALYSIS_GOALS",
    "LOOKBACK_CHOICES",
    "NoninteractiveError",
    "PricingSettings",
    "ProviderFamily",
    "ReportSettings",
    "RunState",
    "SubscriptionOption",
    "TargetScope",
    "WorkflowError",
    "WorkloadDefaults",
    "WorkloadSettings",
    "normalize_analysis_goal",
]
