#!/usr/bin/env python3
"""Generate the checked-in demo from the production analyzer and renderer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokenlens.analyzer import analyze
from tokenlens.analyzer import analyze_task_events
from tokenlens.economics import SavingsScenario
from tokenlens.ingest import load_records_many
from tokenlens.pricing import PriceEntry, PricingCatalog
from tokenlens.reports import report_html, task_economics_html
from tokenlens.events import HumanReviewEvent, ModelCallEvent, TaskResultEvent


ROOT = Path(__file__).resolve().parents[1]
fixture = ROOT / "examples" / "multi-deployment-portfolio.jsonl"

DEMO_DEPLOYMENTS = (
    ("reasoning-prod", "MAI-Thinking-1", 3220, 640, 112, "reasoning", "global", True),
    ("general-prod", "Phi-4-mini", 2160, 420, 72, "general", "global", False),
    ("reasoning-batch", "gpt-4.1", 1680, 520, 88, "batch", "global", True),
    ("security-prod", "MAI-Cyber-1 Flash", 488, 300, 180, "security", "global", True),
)


def write_fixture() -> None:
    """Create deterministic, clearly synthetic records without storing customer text."""
    with fixture.open("w", encoding="utf-8") as output:
        sequence = 0
        for deployment, model, count, input_tokens, output_tokens, workload, mode, cache_supported in DEMO_DEPLOYMENTS:
            start = datetime(2026, 9, 14, tzinfo=UTC)
            bucket_count = 864 if model == "gpt-4.1" else 288
            if model == "gpt-4.1":
                start = datetime(2026, 9, 11, tzinfo=UTC)
            for index in range(count):
                sequence += 1
                messages = [{"role": "user", "content": f"Process synthetic {workload} task {index % 97:02d}."}]
                if sequence <= 6000:
                    messages.insert(0, {"role": "system", "content": "Synthetic task."})
                payload = {
                    "timestamp": (start + timedelta(minutes=5 * (index % bucket_count), seconds=index // bucket_count)).isoformat(),
                    "request_id": f"demo-{sequence:05d}",
                    "deployment_name": deployment,
                    "model_name": model,
                    "model": deployment,
                    "provider": "azure_foundry",
                    "deployment_mode": mode,
                    "messages": messages,
                    "tools": [],
                    "max_output_tokens": 2048 if workload == "creative" else 4096,
                    "usage": {
                        "input_tokens": input_tokens + (index % 7),
                        "output_tokens": output_tokens + (index % 5),
                        "cached_tokens": (input_tokens // 4) if cache_supported and index % 3 == 0 else 0,
                    },
                    "latency_ms": (
                        6200 + index % 900
                        if model == "gpt-4.1"
                        else 2400 + index % 400
                        if model == "MAI-Thinking-1"
                        else 900 + index % 200
                    ),
                    "status_code": 429 if model == "gpt-4.1" and index % 16 == 0 else 200,
                    "metadata": {"tenant": "tenant-demo-001", "workload": f"workload-{workload}"},
                }
                output.write(json.dumps(payload, separators=(",", ":")) + "\n")


TASK_FIXTURE = ROOT / "examples" / "synthetic-task-events.jsonl"


def write_task_fixture() -> list[dict]:
    """Create deterministic synthetic task events for the task-economics report."""
    events: list[dict] = []
    sequence = 0

    def emit(payload: dict) -> None:
        nonlocal sequence
        sequence += 1
        payload.setdefault("event_id", f"synthetic-event-{sequence:05d}")
        payload.setdefault("timestamp", f"2026-09-14T10:{sequence // 60:02d}:{sequence % 60:02d}Z")
        events.append(payload)

    for index in range(130):
        task_id = f"synthetic-ticket-{index:03d}"
        strategy = "direct" if index < 65 else "fallback"
        model = "synthetic-model-a" if index % 2 else "synthetic-model-b"
        attempt = "attempt-1"
        emit(
            {
                "schema_version": 2,
                "event_type": "model_call",
                "task_id": task_id,
                "task_type": "ticket-classification",
                "attempt_id": attempt,
                "step_index": 1,
                "execution_strategy": strategy,
                "strategy_version": "v1",
                "provider": "synthetic",
                "deployment_name": f"{strategy}-deployment",
                "model_name": model,
                "service_tier": "standard",
                "region": "global",
                "usage": {
                    "input_tokens": 900 + index,
                    "cached_input_tokens": 150 if index % 3 == 0 else 0,
                    "cache_write_tokens": 20 if index % 5 == 0 else 0,
                    "output_tokens": 140,
                    "reasoning_tokens": 40,
                },
                "observed_cost_usd": round(0.011 + index / 100000, 6) if index % 4 == 0 else None,
            }
        )
        if index % 13 == 0:
            emit(
                {
                    "schema_version": 2,
                    "event_type": "tool_step",
                    "task_id": task_id,
                    "task_type": "ticket-classification",
                    "attempt_id": attempt,
                    "step_index": 2,
                    "execution_strategy": strategy,
                    "strategy_version": "v1",
                    "tool_category": "search",
                    "observed_cost_usd": 0.001,
                    "outcome": "success",
                }
            )
        solved = index % 9 != 0 or strategy == "fallback"
        if not solved:
            emit(
                {
                    "schema_version": 2,
                    "event_type": "task_result",
                    "task_id": task_id,
                    "task_type": "ticket-classification",
                    "attempt_id": attempt,
                    "execution_strategy": strategy,
                    "strategy_version": "v1",
                    "outcome": "failed",
                    "automated_check": "failed",
                }
            )
            attempt = "attempt-2"
            emit(
                {
                    "schema_version": 2,
                    "event_type": "model_call",
                    "task_id": task_id,
                    "task_type": "ticket-classification",
                    "attempt_id": attempt,
                    "step_index": 1,
                    "execution_strategy": strategy,
                    "strategy_version": "v1",
                    "provider": "synthetic",
                    "deployment_name": f"{strategy}-deployment",
                    "model_name": "synthetic-model-b",
                    "service_tier": "standard",
                    "region": "global",
                    "usage": {"input_tokens": 420, "output_tokens": 90},
                    "observed_cost_usd": 0.006,
                }
            )
        emit(
            {
                "schema_version": 2,
                "event_type": "task_result",
                "task_id": task_id,
                "task_type": "ticket-classification",
                "attempt_id": attempt,
                "execution_strategy": strategy,
                "strategy_version": "v1",
                "outcome": "solved",
                "automated_check": "passed",
            }
        )
        if index % 4 == 0:
            emit(
                {
                    "schema_version": 2,
                    "event_type": "human_review",
                    "task_id": task_id,
                    "review_outcome": "incorrect" if index % 12 == 0 else "correct",
                    "escaped_error": index % 12 == 0,
                    "policy_compliant": index % 12 != 0,
                    "cleanup_cost_usd": 0.08 if index % 12 == 0 else 0,
                }
            )

    for index in range(40):
        task_id = f"synthetic-document-{index:03d}"
        emit(
            {
                "schema_version": 2,
                "event_type": "model_call",
                "task_id": task_id,
                "task_type": "document-extraction",
                "attempt_id": "attempt-1",
                "step_index": 1,
                "execution_strategy": "structured",
                "strategy_version": "v2",
                "provider": "synthetic",
                "deployment_name": "structured-deployment",
                "model_name": "synthetic-model-a",
                "service_tier": "standard",
                "region": "global",
                "usage": {"input_tokens": 1200, "output_tokens": 180},
                "observed_cost_usd": 0.019,
            }
        )
        emit(
            {
                "schema_version": 2,
                "event_type": "task_result",
                "task_id": task_id,
                "task_type": "document-extraction",
                "attempt_id": "attempt-1",
                "execution_strategy": "structured",
                "strategy_version": "v2",
                "outcome": "solved" if index % 6 else "failed",
                "automated_check": "passed",
            }
        )

    for index in range(8):
        task_id = f"synthetic-policy-{index:03d}"
        emit(
            {
                "schema_version": 2,
                "event_type": "model_call",
                "task_id": task_id,
                "task_type": "policy-controlled-action",
                "attempt_id": "attempt-1",
                "step_index": 1,
                "execution_strategy": "approval-gated",
                "strategy_version": "v1",
                "provider": "synthetic",
                "deployment_name": "policy-deployment",
                "model_name": "synthetic-model-unresolved",
                "service_tier": "standard",
                "region": "global",
                "usage": {"input_tokens": 700, "output_tokens": 100},
            }
        )
    TASK_FIXTURE.write_text("\n".join(json.dumps(item, separators=(",", ":")) for item in events) + "\n", encoding="utf-8")
    return events


write_fixture()
records, _ = load_records_many([str(fixture)])
request_customer_catalog = PricingCatalog(
    catalog_name="synthetic-customer-override",
    prices=[
        PriceEntry(
            provider="azure_foundry",
            model="gpt-4.1",
            region="global",
            effective_from="2026-01-01",
            input_per_million=2.0,
            cached_input_per_million=0.5,
            output_per_million=8.0,
        ),
    ],
)
report = analyze(
    records,
    "examples/multi-deployment-portfolio.jsonl",
    generated_at="2026-09-14T23:59:00+00:00",
    report_config={
        "overview_min_impact_percent": 1.0,
        "overview_min_impact_tokens": 100000,
        "overview_max_findings": 3,
    },
    customer_catalog=request_customer_catalog,
)
events = write_task_fixture()
customer_catalog = PricingCatalog(
    catalog_name="synthetic-customer-catalog",
    prices=[
        PriceEntry(provider="synthetic", model="synthetic-model-a", effective_from="2026-01-01", input_per_million=1.0, cached_input_per_million=0.1, cache_write_per_million=1.2, output_per_million=4.0),
    ],
)
reference_catalog = PricingCatalog(
    catalog_name="synthetic-dated-reference-catalog",
    prices=[
        PriceEntry(provider="synthetic", model="synthetic-model-b", effective_from="2026-01-01", input_per_million=1.2, cached_input_per_million=0.12, cache_write_per_million=1.4, output_per_million=4.5),
    ],
)
task_report = analyze_task_events(
    events,
    "synthetic task events",
    customer_catalog=customer_catalog,
    reference_catalog=reference_catalog,
)
task_report = task_report.model_copy(
    update={
        "scenarios": [
            SavingsScenario(name="caching", label="Caching", min_value=12, max_value=18, unit="%", note="Independent scenario; not additive."),
            SavingsScenario(name="trajectory", label="Retry and trajectory efficiency", min_value=3, max_value=5, unit="%", note="Independent scenario; not additive."),
            SavingsScenario(name="routing", label="Model and tier routing", unit="simulation", note="Requires a quality-preserving replay; not additive."),
        ]
    }
)
combined = report.model_copy(update={"task_economics": task_report})
(ROOT / "docs" / "tokenlens-report-demo.html").write_text(report_html(combined), encoding="utf-8")
(ROOT / "docs" / "tokenlens-task-economics-demo.html").write_text(task_economics_html(task_report), encoding="utf-8")
print(f"Generated {ROOT / 'docs' / 'tokenlens-report-demo.html'} from {len(records):,} synthetic requests and {len(events):,} synthetic task events")
