from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .economics import TaskEconomicsReport
from .models import AnalysisReport


def timestamp_slug(now: datetime | None = None) -> str:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.strftime("%Y%m%d-%H%M%SZ")


def timestamped_path(
    *,
    report_format: str,
    output_dir: str | Path = ".",
    now: datetime | None = None,
    comparison: bool = False,
) -> Path:
    extension = {"html": "html", "json": "json", "sarif": "sarif", "text": "txt"}[report_format]
    prefix = "tokenlens-comparison" if comparison else "tokenlens-report"
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{prefix}-{timestamp_slug(now)}.{extension}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{prefix}-{timestamp_slug(now)}-{counter}.{extension}"
        counter += 1
    return candidate


def choose_output(
    output: str | None,
    *,
    report_format: str,
    output_dir: str | Path = ".",
    comparison: bool = False,
) -> Path | None:
    if output:
        return Path(output)
    if report_format == "text":
        return None
    return timestamped_path(report_format=report_format, output_dir=output_dir, comparison=comparison)


def economics_json(report: TaskEconomicsReport) -> str:
    """Serialize task economics without event/task/attempt identifiers."""
    return report.model_dump_json(indent=2) + "\n"


def economics_text(report: TaskEconomicsReport) -> str:
    lines = [
        "TokenLens for Azure · Task economics",
        "─" * 68,
        (
            f"Observed {report.total_attempted_tasks:,} tasks · {report.total_solved_tasks:,} solved · "
            f"{report.pricing.coverage_percent:.1f}% billable-event cost coverage"
        ),
    ]
    for cohort in report.task_types:
        success = "n/a" if cohort.success_rate is None else f"{cohort.success_rate:.1f}%"
        cost = "unresolved" if cohort.cost_per_solved_task_usd is None else f"${cohort.cost_per_solved_task_usd:.6f}"
        lines.append(
            f"{cohort.task_type}: {cohort.maturity_label} · {cohort.attempted_tasks:,} attempted · "
            f"{success} solved · cost/solved {cost}"
        )
    if report.execution_strategies:
        lines.append("")
        lines.append("Execution strategies (equivalent task types only):")
        for strategy in report.execution_strategies[:10]:
            cost = "unresolved" if strategy.cost_per_solved_task_usd is None else f"${strategy.cost_per_solved_task_usd:.6f}"
            lines.append(
                f"- {strategy.task_type} · {strategy.execution_strategy} {strategy.strategy_version} · "
                f"{strategy.closed_tasks:,} closed · {strategy.eventual_success_rate if strategy.eventual_success_rate is not None else 'n/a'}% success · {cost}"
            )
    return "\n".join(lines) + "\n"


def pricing_audit_text(report: AnalysisReport) -> str:
    """Render a non-networking pricing coverage audit.

    Reports only model/mode/coverage/provenance facts; it never prints
    endpoints, resource IDs, tenant values, request IDs, or prompt/response
    content.
    """
    from .presentation import model_rollups

    summary = report.summary
    lines = [
        "TokenLens for Azure · Pricing audit",
        "─" * 68,
        (
            f"{summary.requests_analyzed:,} requests · {summary.total_tokens:,} tokens · "
            f"{len(model_rollups(report)):,} unique model/mode combinations"
        ),
        (
            f"Coverage: {summary.pricing_coverage_requests_percent:.1f}% of requests · "
            f"{summary.pricing_coverage_tokens_percent:.1f}% of tokens priced"
        ),
        "",
    ]
    for item in model_rollups(report):
        lines.append(f"model={item.model_name} mode={item.deployment_mode} tier={item.service_tier}")
        lines.append(
            f"  requests={item.requests:,} tokens={item.total_tokens:,} "
            f"coverage={item.pricing_coverage_requests_percent:.1f}%"
        )
        if item.estimated_cost_usd is not None:
            catalog = "customer" if item.pricing_source == "customer" else (
                "reference" if item.pricing_source == "reference" else item.pricing_source
            )
            lines.append(
                f"  selected-catalog={catalog} billing-basis={item.pricing_billing_basis or 'unknown'} "
                f"publisher={item.pricing_publisher or 'unknown'}"
            )
        if item.unresolved_requests:
            reasons = ", ".join(item.unresolved_reasons) or "no-exact-model-mode-price"
            overrides = ", ".join(item.suggested_override_keys) or item.canonical_model_key
            lines.append(
                f"  unresolved: {item.unresolved_requests:,} requests / {item.unresolved_tokens:,} tokens "
                f"· reason={reasons}"
            )
            lines.append(f"  suggested-override-key: {overrides}")
        lines.append("")
    lines.append(
        "Add a customer_catalog entry (see examples/customer-pricing-overrides-example.yml) "
        "for any model listed as unresolved above."
    )
    return "\n".join(lines) + "\n"
