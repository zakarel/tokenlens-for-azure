"""Workload identity, default technical workloads, and workload economics.

Every fixture is synthetic and offline: no Azure client is constructed and no
model call is made.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.pricing import PriceEntry, PricingCatalog
from tokenlens.workloads import (
    UNASSIGNED_WORKLOAD_ID,
    WorkloadIdentity,
    WorkloadMapping,
    WorkloadMappingError,
    canonical_workload_id,
    default_technical_workload,
    default_workload_id,
    internal_workload_key,
    merge_identities,
    record_workload,
    safe_account_scope,
    task_metrics_by_workload,
    validate_mappings,
)

PRICED_MODEL = "priced-model-test-synthetic"
UNPRICED_MODEL = "unpriced-model-test-synthetic"


def catalog() -> PricingCatalog:
    return PricingCatalog(
        catalog_name="synthetic-test-catalog",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                model=PRICED_MODEL,
                effective_from="2026-01-01",
                input_per_million=1.0,
                output_per_million=2.0,
            )
        ],
    )


def request_record(
    index: int,
    *,
    deployment: str,
    model: str = PRICED_MODEL,
    workload: str | None = None,
    input_tokens: int = 1000,
    output_tokens: int = 500,
    day: int = 1,
):
    raw = {
        "timestamp": (datetime(2026, 9, day, tzinfo=UTC) + timedelta(minutes=index)).isoformat(),
        "deployment_name": deployment,
        "model_name": model,
        "deployment_mode": "global",
        "messages": [{"role": "user", "content": "synthetic"}],
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        "status_code": 200,
    }
    if workload:
        raw["metadata"] = {"workload": workload}
    return next(iter_records(io.StringIO(json.dumps(raw) + "\n")))


def analyzed(records, **kwargs):
    return analyze(
        records,
        "fixture",
        generated_at="2026-09-15T08:00:00Z",
        customer_catalog=catalog(),
        use_bundled_reference=False,
        **kwargs,
    )


def test_every_deployment_gets_one_stable_default_technical_workload():
    workload = default_technical_workload("reasoning-prod", account_scope="example-rg/example-account")
    assert workload.workload_id == "deployment:reasoning-prod"
    assert workload.workload_name == "reasoning-prod"
    assert workload.workload_scope == "technical"
    assert workload.workload_type == "ai_deployment"
    assert workload.source == "system_default"
    assert workload.configuration_status == "needs_configuration"
    assert workload.allocation == "deployment_total"
    assert workload.environment is None
    assert workload.business_purpose is None
    assert workload.owner_label is None
    assert workload.cost_center is None
    assert workload.criticality == "unknown"
    # The internal key disambiguates duplicate names across accounts without
    # exposing a subscription or tenant identifier.
    assert workload.internal_key == internal_workload_key("example-rg/example-account", "reasoning-prod")
    assert "subscription" not in workload.internal_key


def test_default_ids_are_stable_across_refresh_and_never_random():
    first = merge_identities(["reasoning-prod", "coding-prod"])
    second = merge_identities(["reasoning-prod", "coding-prod"], existing=first)
    assert [item.workload_id for item in first] == [item.workload_id for item in second]
    assert all(item.workload_id == default_workload_id(item.workload_name) for item in first)


def test_refresh_preserves_enrichment_adds_new_and_marks_stale_without_deleting():
    enriched = default_technical_workload("reasoning-prod").model_copy(
        update={
            "environment": "production",
            "owner_label": "platform-team",
            "configuration_status": "partially_configured",
        }
    )
    removed = default_technical_workload("retired-prod")
    refreshed = merge_identities(
        ["reasoning-prod", "new-prod"], existing=[enriched, removed]
    )
    by_id = {item.workload_id: item for item in refreshed}
    assert by_id["deployment:reasoning-prod"].environment == "production"
    assert by_id["deployment:reasoning-prod"].owner_label == "platform-team"
    assert by_id["deployment:new-prod"].configuration_status == "needs_configuration"
    # Historical workload data is retained; the row is only marked stale.
    assert by_id["deployment:retired-prod"].stale is True


def test_business_mapping_never_overwrites_the_technical_reconciliation_layer():
    mapping = WorkloadMapping(
        id="support-assistant",
        name="Support assistant",
        type="agent",
        environment="production",
        deployments=["reasoning-prod"],
        allocation="dedicated",
    )
    identities = merge_identities(["reasoning-prod"], mappings=[mapping])
    technical = next(item for item in identities if item.workload_scope == "technical")
    business = next(item for item in identities if item.workload_scope == "business")
    assert technical.workload_name == "reasoning-prod"
    assert technical.configuration_status == "configured"
    assert business.workload_name == "Support assistant"
    assert business.allocation == "dedicated"
    assert business.source == "deployment_mapping"


def test_a_deployment_cannot_be_dedicated_to_two_workloads():
    mappings = [
        WorkloadMapping(id="a", name="A", deployments=["shared-prod"], allocation="dedicated"),
        WorkloadMapping(id="b", name="B", deployments=["shared-prod"], allocation="dedicated"),
    ]
    problems = validate_mappings(mappings)
    assert any("dedicated to both" in problem for problem in problems)


def test_a_deployment_cannot_be_both_dedicated_and_shared():
    mappings = [
        WorkloadMapping(id="a", name="A", deployments=["shared-prod"], allocation="dedicated"),
        WorkloadMapping(id="b", name="B", deployments=["shared-prod"], allocation="shared"),
    ]
    assert any("both dedicated and shared" in problem for problem in validate_mappings(mappings))


def test_stale_mapping_is_surfaced_against_the_discovered_inventory():
    mappings = [WorkloadMapping(id="a", name="A", deployments=["gone-prod"], allocation="dedicated")]
    problems = validate_mappings(mappings, known_deployments=["reasoning-prod"])
    assert any("not in the discovered inventory" in problem for problem in problems)


def test_overlapping_mappings_are_rejected_before_any_calculation():
    records = [request_record(i, deployment="shared-prod") for i in range(4)]
    with pytest.raises(WorkloadMappingError):
        analyzed(
            records,
            workload_mappings=[
                WorkloadMapping(id="a", name="A", deployments=["shared-prod"], allocation="dedicated"),
                WorkloadMapping(id="b", name="B", deployments=["shared-prod"], allocation="dedicated"),
            ],
        )


def test_workload_identity_comes_only_from_explicit_provenance():
    tagged = request_record(0, deployment="reasoning-prod", workload="support-assistant")
    assert record_workload(tagged) == ("support-assistant", "request_tag", "exact_request_tag")
    # A deployment name that looks like a workload is never treated as one.
    untagged = request_record(0, deployment="support-assistant-prod")
    assert record_workload(untagged) == (None, None, None)


def test_technical_coverage_is_100_percent_when_deployment_identity_is_complete():
    records = [request_record(i, deployment="reasoning-prod") for i in range(4)]
    portfolio = analyzed(records).workloads
    assert portfolio.technical_workload_coverage_percent == 100.0
    # Technical coverage is never presented as business identity coverage.
    assert portfolio.workload_identity_coverage_percent == 0.0
    assert portfolio.default_scope == "technical"


def test_identity_failure_reduces_technical_coverage_separately_from_pricing():
    records = [request_record(i, deployment="reasoning-prod") for i in range(4)]
    records += [request_record(i, deployment="legacy-prod", model="unknown") for i in range(2)]
    portfolio = analyzed(records).workloads
    assert portfolio.technical_workload_coverage_percent < 100.0
    assert portfolio.identity_unresolved_tokens == 3000


def test_dedicated_deployment_cost_maps_exactly_to_one_business_workload():
    records = [request_record(i, deployment="reasoning-prod") for i in range(4)]
    mapping = WorkloadMapping(
        id="support-assistant", name="Support assistant", deployments=["reasoning-prod"], allocation="dedicated"
    )
    portfolio = analyzed(records, workload_mappings=[mapping]).workloads
    business = portfolio.business_workloads
    assert [item.workload_id for item in business] == ["support-assistant"]
    assert business[0].allocation_confidence == "exact_dedicated_deployment"
    assert business[0].total_tokens == portfolio.workloads[0].total_tokens
    assert business[0].estimated_cost == portfolio.workloads[0].estimated_cost
    assert portfolio.workload_identity_coverage_percent == 100.0


def test_shared_deployment_without_request_tags_stays_unassigned():
    records = [request_record(i, deployment="shared-prod") for i in range(4)]
    mapping = WorkloadMapping(
        id="support-assistant", name="Support assistant", deployments=["shared-prod"], allocation="shared"
    )
    portfolio = analyzed(records, workload_mappings=[mapping]).workloads
    ids = [item.workload_id for item in portfolio.business_workloads]
    assert ids == [UNASSIGNED_WORKLOAD_ID]
    unassigned = portfolio.business_workloads[0]
    assert unassigned.allocation_confidence == "unallocated"
    # Cost is never divided proportionally between the sharing workloads.
    assert unassigned.total_tokens == portfolio.workloads[0].total_tokens
    assert portfolio.shared_deployments_without_tags == ["shared-prod"]


def test_shared_business_allocation_reconciles_exactly_with_the_technical_total():
    records = [
        request_record(i, deployment="shared-prod", workload="support-assistant" if i % 2 == 0 else None)
        for i in range(6)
    ]
    mapping = WorkloadMapping(
        id="support-assistant", name="Support assistant", deployments=["shared-prod"], allocation="shared"
    )
    portfolio = analyzed(records, workload_mappings=[mapping]).workloads
    technical_total = portfolio.workloads[0].total_tokens
    business_total = sum(item.total_tokens or 0 for item in portfolio.business_workloads)
    assert business_total == technical_total
    assert {item.workload_id for item in portfolio.business_workloads} == {
        "support-assistant",
        UNASSIGNED_WORKLOAD_ID,
    }


def test_workload_totals_reconcile_with_deployment_and_model_totals():
    records = [request_record(i, deployment="reasoning-prod") for i in range(4)]
    records += [request_record(i, deployment="coding-prod", model=UNPRICED_MODEL) for i in range(3)]
    report = analyzed(records)
    portfolio = report.workloads
    assert portfolio.total_tokens == report.summary.total_tokens
    assert sum(item.total_tokens or 0 for item in portfolio.workloads) == report.summary.total_tokens
    assert portfolio.unpriced_tokens == report.summary.unresolved_tokens


def test_unresolved_cost_is_never_treated_as_zero():
    records = [request_record(i, deployment="coding-prod", model=UNPRICED_MODEL) for i in range(3)]
    portfolio = analyzed(records).workloads
    rollup = portfolio.workloads[0]
    assert rollup.estimated_cost is None
    assert rollup.unpriced_tokens == 4500
    assert rollup.pricing_coverage_percent == 0.0
    assert portfolio.total_estimated_cost is None


def test_cost_per_request_is_withheld_when_price_coverage_is_incomplete():
    priced = [request_record(i, deployment="mixed-prod") for i in range(2)]
    unpriced = [request_record(i + 10, deployment="mixed-prod", model=UNPRICED_MODEL) for i in range(2)]
    portfolio = analyzed(priced + unpriced).workloads
    rollup = next(item for item in portfolio.workloads if item.workload_name == "mixed-prod")
    assert rollup.estimated_cost is not None
    assert rollup.cost_per_request is None


def test_cost_per_request_is_reported_when_both_denominator_and_price_are_complete():
    records = [request_record(i, deployment="reasoning-prod") for i in range(4)]
    portfolio = analyzed(records).workloads
    rollup = portfolio.workloads[0]
    assert rollup.requests == 4
    assert rollup.cost_per_request == pytest.approx((rollup.estimated_cost or 0) / 4)


def test_daily_statistics_use_observed_dates_and_disclose_partial_days():
    records = [request_record(i, deployment="reasoning-prod", day=1) for i in range(2)]
    records += [request_record(i, deployment="reasoning-prod", day=3) for i in range(2)]
    records += [request_record(i, deployment="other-prod", day=2) for i in range(2)]
    portfolio = analyzed(records).workloads
    rollup = next(item for item in portfolio.workloads if item.workload_name == "reasoning-prod")
    assert rollup.active_days == 2
    assert [day for day, _ in rollup.daily_costs] == ["2026-09-01", "2026-09-03"]
    assert rollup.partial_days == ["2026-09-02"]
    assert rollup.p50_daily_cost is not None and rollup.p90_daily_cost is not None


def test_a_configured_deployment_with_no_traffic_still_appears():
    records = [request_record(i, deployment="reasoning-prod") for i in range(2)]
    identities = merge_identities(["reasoning-prod", "idle-prod"])
    portfolio = analyzed(records, workload_identities=identities).workloads
    names = [item.workload_name for item in portfolio.workloads]
    assert "idle-prod" in names
    idle = next(item for item in portfolio.workloads if item.workload_name == "idle-prod")
    assert idle.total_tokens == 0
    assert idle.estimated_cost is None


def test_otel_attribute_source_is_recorded_separately_from_a_request_tag():
    raw = {
        "schema_version": 3,
        "record_type": "model_request",
        "source": "otel",
        "event_id": "synthetic-otel-1",
        "timestamp": "2026-09-01T00:00:00Z",
        "provider": "azure_foundry",
        "deployment_name": "reasoning-prod",
        "model_name": PRICED_MODEL,
        "workload": "support-assistant",
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
    record = next(iter_records(io.StringIO(json.dumps(raw) + "\n")))
    assert record_workload(record) == ("support-assistant", "otel_attribute", "exact_request_tag")


def test_aggregate_bucket_carries_workload_only_from_a_dedicated_mapping():
    raw = {
        "schema_version": 3,
        "record_type": "foundry_metric_bucket",
        "source": "azure_monitor",
        "event_id": "synthetic-bucket-1",
        "timestamp": "2026-09-01T00:00:00Z",
        "deployment_name": "reasoning-prod",
        "model_name": PRICED_MODEL,
        "workload": "support-assistant",
        "workload_source": "deployment_mapping",
        "allocation_confidence": "exact_dedicated_deployment",
        "metrics": {"input_tokens": 100, "output_tokens": 20, "requests": 2},
    }
    record = next(iter_records(io.StringIO(json.dumps(raw) + "\n")))
    assert record_workload(record) == (
        "support-assistant",
        "deployment_mapping",
        "exact_dedicated_deployment",
    )


def test_safe_account_scope_excludes_subscription_and_tenant_values():
    scope = safe_account_scope("Example Foundry Account", "Example-RG")
    assert scope == "example-rg/example-foundry-account"
    assert canonical_workload_id("Support Assistant!") == "support-assistant"


def test_task_metrics_require_an_explicit_workload_label():
    class _Review:
        def __init__(self) -> None:
            self.event = type("E", (), {"review_outcome": "correct"})()

    class _Task:
        def __init__(self, workload, cost, outcome):
            self.workload = workload
            self.cost_usd = cost
            self.outcome = outcome
            self.closed = outcome is not None
            self.resolved = True
            self.retry_count = 1
            self.model_calls = 2
            self.cleanup_cost_usd = None
            self.reviews = [_Review()]

    metrics = task_metrics_by_workload(
        [
            _Task("support-assistant", 0.5, "solved"),
            _Task("support-assistant", 1.5, "failed"),
            _Task(None, 9.0, "solved"),
        ]
    )
    assert set(metrics) == {"support-assistant"}
    cohort = metrics["support-assistant"]
    assert cohort.attempted_tasks == 2
    assert cohort.solved_tasks == 1
    assert cohort.cost_per_solved_task == pytest.approx(0.5)
    assert cohort.failed_trajectory_cost == pytest.approx(1.5)
    assert cohort.retries_per_task == pytest.approx(1.0)


def test_workload_identity_model_rejects_unknown_fields():
    with pytest.raises(ValueError):
        WorkloadIdentity.model_validate(
            {
                "workload_id": "deployment:x",
                "workload_name": "x",
                "workload_scope": "technical",
                "workload_type": "ai_deployment",
                "source": "system_default",
                "configuration_status": "needs_configuration",
                "allocation": "deployment_total",
                "subscription_id": "00000000-0000-0000-0000-000000000000",
            }
        )


def test_business_scope_reconciles_when_one_deployment_is_never_tagged():
    """Regression: untagged traffic must never vanish from the business view."""
    records = [request_record(i, deployment="tagged-prod", workload="Support assistant") for i in range(3)]
    records += [request_record(i, deployment="untagged-prod") for i in range(3)]
    portfolio = analyzed(records).workloads
    technical_total = sum(item.total_tokens or 0 for item in portfolio.workloads)
    business_total = sum(item.total_tokens or 0 for item in portfolio.business_workloads)
    assert business_total == technical_total
    unassigned = next(
        item for item in portfolio.business_workloads if item.workload_id == UNASSIGNED_WORKLOAD_ID
    )
    assert unassigned.total_tokens == 4500
    assert unassigned.deployments == ["untagged-prod"]


def test_no_unassigned_row_is_invented_when_no_business_identity_exists():
    records = [request_record(i, deployment="reasoning-prod") for i in range(4)]
    portfolio = analyzed(records).workloads
    assert portfolio.business_workloads == []
    assert portfolio.default_scope == "technical"


def test_malformed_provenance_metadata_never_aborts_the_analysis():
    """Regression: record metadata is caller-supplied and is never trusted."""
    raw = {
        "timestamp": "2026-09-01T00:00:00Z",
        "deployment_name": "reasoning-prod",
        "model_name": PRICED_MODEL,
        "deployment_mode": "global",
        "messages": [{"role": "user", "content": "synthetic"}],
        "usage": {"input_tokens": 1000, "output_tokens": 500},
        "metadata": {
            "workload": "support-assistant",
            "workload_source": "definitely-not-a-source",
            "allocation_confidence": "definitely-exact",
        },
    }
    record = next(iter_records(io.StringIO(json.dumps(raw) + "\n")))
    workload, source, confidence = record_workload(record)
    assert workload == "support-assistant"
    assert source == "request_tag"
    assert confidence == "exact_request_tag"
    portfolio = analyzed([record]).workloads
    assert [item.workload_id for item in portfolio.business_workloads] == ["support-assistant"]


def test_task_cost_per_cohort_is_withheld_when_any_task_is_unpriced():
    """Regression: a partial sum would understate the real cost per task."""

    class _Task:
        def __init__(self, resolved, cost, outcome):
            self.workload = "support-assistant"
            self.resolved = resolved
            self.cost_usd = cost
            self.outcome = outcome
            self.closed = True
            self.retry_count = 0
            self.model_calls = 1
            self.cleanup_cost_usd = None
            self.reviews = []

    metrics = task_metrics_by_workload(
        [_Task(True, 0.5, "solved"), _Task(False, None, "solved")]
    )["support-assistant"]
    assert metrics.attempted_tasks == 2
    assert metrics.solved_tasks == 2
    assert metrics.cost_per_solved_task is None
    assert metrics.cost_per_attempted_task is None
    assert metrics.cost_per_closed_task is None


def test_task_cost_per_cohort_uses_the_full_cohort_as_the_denominator():
    class _Task:
        def __init__(self, cost):
            self.workload = "support-assistant"
            self.resolved = True
            self.cost_usd = cost
            self.outcome = "solved"
            self.closed = True
            self.retry_count = 0
            self.model_calls = 1
            self.cleanup_cost_usd = None
            self.reviews = []

    metrics = task_metrics_by_workload([_Task(1.0), _Task(3.0)])["support-assistant"]
    assert metrics.cost_per_attempted_task == pytest.approx(2.0)
    assert metrics.cost_per_solved_task == pytest.approx(2.0)
