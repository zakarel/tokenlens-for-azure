from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .economics import TaskEconomicsReport


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
