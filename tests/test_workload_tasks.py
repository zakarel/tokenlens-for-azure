"""Task economics drilling down beneath workloads, plus terminal compatibility.

The existing task-economics engine is reused rather than duplicated: task
events supply outcomes, workload identity supplies the grouping, and neither is
inferred from the other.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tokenlens import foundry_cli
from tokenlens.cli import app
from tokenlens.events import parse_event
from tokenlens.foundry_workflow.orchestration import WorkflowServices
from tokenlens.foundry_workflow.prompts import Choice, ScriptedPrompter, TyperPrompter, ascii_only, symbol
from tokenlens.reports import report_html
from tokenlens.tasks import reconstruct_tasks

from test_foundry_workflow import SUBSCRIPTION, StubResources, collector

runner = CliRunner()

BASE = datetime(2026, 9, 1, tzinfo=UTC)
TASK_MODEL = "phi-4"


def model_call(task: str, *, workload: str, step: int = 0, attempt: str = "attempt-1") -> dict:
    return {
        "schema_version": 2,
        "event_type": "model_call",
        "event_id": f"{task}-call-{step}",
        "timestamp": (BASE + timedelta(minutes=step)).isoformat(),
        "task_id": task,
        "task_type": "ticket-classification",
        "attempt_id": attempt,
        "execution_strategy": "single-pass",
        "strategy_version": "v1",
        "workload": workload,
        "step_index": step,
        "provider": "azure_foundry",
        "deployment_name": "reasoning-prod",
        "model_name": TASK_MODEL,
        "region": "global",
        "usage": {"input_tokens": 1000, "output_tokens": 200},
    }


def task_result(task: str, outcome: str, *, workload: str, attempt: str = "attempt-1") -> dict:
    return {
        "schema_version": 2,
        "event_type": "task_result",
        "event_id": f"{task}-result",
        "timestamp": (BASE + timedelta(minutes=5)).isoformat(),
        "task_id": task,
        "task_type": "ticket-classification",
        "attempt_id": attempt,
        "execution_strategy": "single-pass",
        "strategy_version": "v1",
        "workload": workload,
        "outcome": outcome,
    }


def test_task_events_carry_an_explicit_workload_through_reconstruction():
    events = [
        parse_event(model_call("task-1", workload="support-assistant")),
        parse_event(task_result("task-1", "solved", workload="support-assistant")),
    ]
    tasks = reconstruct_tasks(events)
    assert [task.workload for task in tasks] == ["support-assistant"]


def test_a_task_event_without_a_workload_is_never_given_one():
    payload = model_call("task-2", workload="support-assistant")
    payload.pop("workload")
    event = parse_event(payload)
    assert event.workload is None
    tasks = reconstruct_tasks([event])
    assert tasks[0].workload is None


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "user-config"))
    monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)

    def _services() -> WorkflowServices:
        return WorkflowServices(
            resource_client=lambda subscription: StubResources(),
            metrics_client=lambda endpoint: {"endpoint": endpoint},
            collect=collector(),
            subscriptions=lambda: [],
            open_report=lambda path: True,
            now=lambda: datetime(2026, 9, 15, tzinfo=UTC),
            missing_packages=lambda: [],
        )

    monkeypatch.setattr(foundry_cli, "_services", _services)
    return repo


def write_task_events(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    events = [
        model_call("task-1", workload="support-assistant"),
        task_result("task-1", "solved", workload="support-assistant"),
        model_call("task-2", workload="support-assistant", step=1),
        task_result("task-2", "failed", workload="support-assistant"),
    ]
    path = directory / "task-events.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    return path


def test_task_metrics_drill_down_beneath_the_workload_they_are_tagged_with(cli):
    write_task_events(cli / "task-traces")
    result = runner.invoke(
        app,
        [
            "foundry",
            "collect",
            "--subscription",
            SUBSCRIPTION,
            "--resource-group",
            "example-rg",
            "--account",
            "example-foundry-account",
            "--deployment",
            "reasoning-prod",
            "--task-events",
            "task-traces",
            "--days",
            "14",
        ],
    )
    assert result.exit_code == 0, result.output
    html = next(Path("reports").glob("*.html")).read_text(encoding="utf-8")
    panel = html[html.index('id="workloads-panel"') : html.index('id="ptu-panel"')]
    assert "Task economics by workload" in panel
    assert "support-assistant" in panel
    assert "Cost/solved" in panel
    # The engine remains the existing task-economics engine.
    assert "Task identity and outcomes were not collected" not in panel


def test_without_task_events_workload_economics_still_renders(cli):
    result = runner.invoke(
        app,
        [
            "foundry",
            "collect",
            "--subscription",
            SUBSCRIPTION,
            "--resource-group",
            "example-rg",
            "--account",
            "example-foundry-account",
            "--deployment",
            "reasoning-prod",
            "--days",
            "14",
        ],
    )
    assert result.exit_code == 0, result.output
    html = next(Path("reports").glob("*.html")).read_text(encoding="utf-8")
    panel = html[html.index('id="workloads-panel"') : html.index('id="ptu-panel"')]
    assert "Task identity and outcomes were not collected" in panel
    assert "Technical · Needs configuration" in panel


# --- Terminal compatibility -------------------------------------------------


def test_unicode_markers_fall_back_to_ascii_when_the_terminal_cannot_encode(monkeypatch):
    monkeypatch.setenv("TOKENLENS_ASCII", "1")
    assert ascii_only() is True
    assert symbol("✓") == "OK"
    assert symbol("⚠") == "!"
    assert symbol("✕") == "x"
    assert symbol("◉") == "(*)"


def test_no_color_is_respected(monkeypatch):
    from tokenlens.foundry_workflow.prompts import use_color

    monkeypatch.setenv("NO_COLOR", "1")
    assert use_color() is False


def test_a_prompter_offers_a_numbered_screen_reader_friendly_fallback():
    prompter = TyperPrompter(interactive=True)
    choices = [Choice("a", "First"), Choice("b", "Second", "detail")]
    from typer.testing import CliRunner as _Runner
    import typer

    inner = typer.Typer()

    @inner.command()
    def run() -> None:
        prompter.select("Pick one", choices)

    result = _Runner().invoke(inner, [], input="2\n")
    assert result.exit_code == 0
    assert "1. First" in result.output
    assert "2. Second   detail" in result.output
    assert "Select a number" in result.output


def test_a_scripted_answer_must_be_one_of_the_offered_choices():
    prompter = ScriptedPrompter(["not-offered"])
    with pytest.raises(AssertionError):
        prompter.select("Pick one", [Choice("a", "First"), Choice("b", "Second")])


def test_multiselect_requires_at_least_one_selection():
    prompter = ScriptedPrompter([[]])
    with pytest.raises(AssertionError):
        prompter.multiselect("Select deployments", [Choice("a", "First")], minimum=1)
