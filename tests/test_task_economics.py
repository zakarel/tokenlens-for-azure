from datetime import UTC, date, datetime, timedelta

import pytest

from tokenlens.analyzer import analyze_task_events
from tokenlens.economics import calculate_task_economics, compare_execution_strategies
from tokenlens.events import (
    EventValidationError,
    HumanReviewEvent,
    ModelCallEvent,
    TaskResultEvent,
    validate_event_stream,
)
from tokenlens.pricing import PriceEntry, PricingCatalog, resolve_event_cost
from tokenlens.tasks import reconstruct_tasks
from tokenlens.cli import app
from typer.testing import CliRunner


BASE = datetime(2026, 9, 14, 10, 0, tzinfo=UTC)


def model(event_id: str, task_id: str, *, attempt: str = "a1", cost: float | None = 0.01, timestamp=BASE):
    return ModelCallEvent(
        event_id=event_id,
        event_type="model_call",
        timestamp=timestamp,
        task_id=task_id,
        task_type="ticket-classification",
        attempt_id=attempt,
        step_index=1,
        execution_strategy="default",
        strategy_version="v1",
        provider="synthetic",
        deployment_name="synthetic-deployment",
        model_name="synthetic-model",
        service_tier="standard",
        region="global",
        usage={"input_tokens": 100, "cached_input_tokens": 20, "cache_write_tokens": 10, "output_tokens": 10, "reasoning_tokens": 4},
        observed_cost_usd=cost,
    )


def result(event_id: str, task_id: str, outcome: str, *, attempt: str = "a1", timestamp=BASE + timedelta(seconds=1)):
    return TaskResultEvent(
        event_id=event_id,
        event_type="task_result",
        timestamp=timestamp,
        task_id=task_id,
        task_type="ticket-classification",
        attempt_id=attempt,
        execution_strategy="default",
        strategy_version="v1",
        outcome=outcome,
        automated_check="passed",
    )


def test_failed_trajectory_is_included_in_solved_task_economics():
    events = [
        model("m1", "task-1", cost=0.01),
        result("r1", "task-1", "failed"),
        model("m2", "task-1", attempt="a2", cost=0.02, timestamp=BASE + timedelta(seconds=2)),
        result("r2", "task-1", "solved", attempt="a2", timestamp=BASE + timedelta(seconds=3)),
    ]
    report = calculate_task_economics(reconstruct_tasks(events))
    cohort = report.task_types[0]
    assert cohort.cost_per_solved_task_usd == 0.03
    assert cohort.retries == 1


def test_open_tasks_are_not_in_success_denominator():
    events = [model("m1", "closed"), result("r1", "closed", "solved"), model("m2", "open")]
    cohort = calculate_task_economics(reconstruct_tasks(events)).task_types[0]
    assert cohort.attempted_tasks == 2
    assert cohort.closed_tasks == 1
    assert cohort.success_rate == 100


def test_reasoning_tokens_are_a_subset_not_an_addition():
    task = reconstruct_tasks([model("m1", "task")])[0]
    assert task.tokens == 110


def test_pricing_precedence_and_formula():
    event = model("m1", "task", cost=None)
    catalog = PricingCatalog(
        catalog_name="customer",
        prices=[PriceEntry(
            provider="synthetic",
            model="synthetic-model",
            effective_from=date(2026, 1, 1),
            input_per_million=1,
            cached_input_per_million=2,
            cache_write_per_million=3,
            output_per_million=4,
        )],
    )
    resolved = resolve_event_cost(event, customer_catalog=catalog)
    assert resolved.source == "customer"
    assert resolved.cost_usd == pytest.approx((70 + 40 + 30 + 40) / 1_000_000)
    observed = resolve_event_cost(event.model_copy(update={"observed_cost_usd": 0.9}), customer_catalog=catalog)
    assert observed.source == "observed" and observed.cost_usd == 0.9


def test_task_economics_never_sums_non_usd_catalog_prices():
    euro_event = model("m1", "task", cost=None).model_copy(
        update={"model_name": "euro-model", "step_index": 1}
    )
    usd_event = model("m2", "task", cost=None).model_copy(
        update={
            "model_name": "usd-model",
            "step_index": 2,
            "timestamp": BASE + timedelta(milliseconds=1),
        }
    )
    customer = PricingCatalog(
        currency="EUR",
        catalog_name="euro-customer",
        prices=[
            PriceEntry(
                provider="synthetic",
                model="euro-model",
                region="global",
                effective_from=date(2026, 1, 1),
                input_per_million=1,
                cached_input_per_million=1,
                output_per_million=1,
            )
        ],
    )
    reference = PricingCatalog(
        currency="USD",
        catalog_name="usd-reference",
        prices=[
            PriceEntry(
                provider="synthetic",
                model="usd-model",
                region="global",
                effective_from=date(2026, 1, 1),
                input_per_million=2,
                cached_input_per_million=2,
                output_per_million=2,
            )
        ],
    )
    report = analyze_task_events(
        [euro_event, usd_event, result("r1", "task", "solved")],
        "fixture",
        customer_catalog=customer,
        reference_catalog=reference,
    )
    assert report.pricing.resolved_billable_events == 1
    assert report.pricing.unresolved_billable_events == 1
    assert report.task_types[0].cost_per_solved_task_usd is None


def test_partial_unresolved_trajectory_has_no_monetary_average():
    events = [model("m1", "task", cost=None), result("r1", "task", "solved")]
    cohort = calculate_task_economics(reconstruct_tasks(events)).task_types[0]
    assert cohort.unresolved_tasks == 1
    assert cohort.cost_per_solved_task_usd is None


def test_observed_cleanup_only():
    events = [
        model("m1", "task"),
        result("r1", "task", "solved"),
        HumanReviewEvent(
            event_id="h1",
            event_type="human_review",
            timestamp=BASE + timedelta(seconds=2),
            task_id="task",
            review_outcome="incorrect",
            escaped_error=True,
            cleanup_cost_usd=0.25,
        ),
    ]
    cohort = calculate_task_economics(reconstruct_tasks(events)).task_types[0]
    assert cohort.observed_cleanup_spend_usd == 0.25
    assert cohort.observed_cost_per_corrected_task_usd == 0.26


def test_conflicting_strategy_metadata_is_rejected():
    changed = model("m2", "task").model_copy(update={"execution_strategy": "other"})
    with pytest.raises(EventValidationError):
        validate_event_stream([model("m1", "task"), changed])


def test_pareto_frontier_and_cross_task_guard():
    first = reconstruct_tasks([model("m1", "task"), result("r1", "task", "solved")])[0]
    second = reconstruct_tasks([
        model("m2", "task-2", cost=0.02),
        result("r2", "task-2", "failed"),
    ])[0]
    comparisons = compare_execution_strategies(
        [first, second],
        task_type="ticket-classification",
        provisional_closed_tasks=0,
        ranked_closed_tasks=2,
    )
    assert all(item.pareto_frontier for item in comparisons)
    with pytest.raises(ValueError):
        compare_execution_strategies([first], task_type="other")


def test_task_json_has_no_internal_ids_and_scenarios_are_independent():
    report = analyze_task_events([model("m1", "secret-id"), result("r1", "secret-id", "solved")], "fixture")
    payload = report.model_dump_json()
    assert "secret-id" not in payload
    assert "event_id" not in payload


def test_no_argument_non_tty_is_help_only():
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "Trace input" not in result.output
