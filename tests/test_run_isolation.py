"""Run isolation, report-slice counts, and honest per-deployment states.

Every fixture here is synthetic: no Azure SDK is imported, no credential is
constructed, and no request is made. The scenario is the one that produced a
misleading report in practice — a base telemetry directory that still holds
files from earlier runs, including slices whose model identity was never
resolved — and the guarantee asserted is that a fresh run reports only what it
just collected, while never deleting the history beside it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokenlens.foundry_workflow.orchestration import collect_deployments, run_directory
from tokenlens.foundry_workflow.prompts import ScriptedPrompter
from tokenlens.foundry_workflow.wizard import collect_and_report, configure
from tokenlens.telemetry.schema import BucketMetrics, MetricBucketRecord

from test_foundry_workflow import (  # noqa: F401 - shared synthetic doubles and fixtures
    ALL_DEPLOYMENTS,
    PRICED_MODEL,
    SUBSCRIPTION,
    first_run_answers,
    services,
    workspace,
)

BASE_DIR = Path("local-traces/foundry-metrics")
WINDOW_START = datetime(2026, 9, 1, tzinfo=UTC)


def stale_records(directory: Path, *, days: int = 17) -> list[Path]:
    """Seed the base directory the way earlier releases wrote it.

    Each file mixes real deployments with an unattributed ``unknown`` slice,
    exactly like the account-wide collections that predate per-deployment
    isolation.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for day in range(days):
        stamp = WINDOW_START + timedelta(days=day)
        path = directory / f"tokenlens-{stamp:%Y-%m-%d}.jsonl"
        lines = []
        for name, model in (("unknown", "unknown"), ("retired-prod", "gpt-4.1")):
            lines.append(
                json.dumps(
                    {
                        "record_type": "foundry_metric_bucket",
                        "event_id": f"stale-{name}-{day}",
                        "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                        "deployment_name": name,
                        "model_name": model,
                        "deployment_mode": "global",
                        "metrics": {"input_tokens": 500, "output_tokens": 100, "requests": 5},
                    }
                )
            )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(path)
    return written


def configured_workspace():
    prompter = ScriptedPrompter(first_run_answers())
    config, _readiness = configure(prompter, services=services())
    assert config.foundry.deployment_names == ALL_DEPLOYMENTS
    return config


def test_three_inventory_deployments_produce_exactly_three_report_slices(workspace):
    """Regression: 17 stale files in the base directory must not reach a new run."""
    stale = stale_records(BASE_DIR)
    fingerprints = {path: path.read_bytes() for path in stale}
    config = configured_workspace()

    summary, result = collect_and_report(
        ScriptedPrompter([], interactive=False), config, services=services(), open_report=False
    )

    assert summary.status == "succeeded"
    assert len(summary.succeeded) == 3
    assert result is not None
    names = [item.summary.deployment_name for item in result.report.deployments]
    assert sorted(names) == ALL_DEPLOYMENTS
    assert len(names) == 3
    assert "unknown" not in names and "retired-prod" not in names
    # The PTU portfolio is built from the same three slices, not from history.
    assert sorted(item.deployment_name for item in result.report.ptu_analysis.deployments) == ALL_DEPLOYMENTS
    # Historical telemetry is preserved byte for byte; nothing is deleted.
    for path, payload in fingerprints.items():
        assert path.read_bytes() == payload


def test_the_report_counts_only_the_current_runs_files(workspace):
    stale_records(BASE_DIR)
    config = configured_workspace()
    summary, result = collect_and_report(
        ScriptedPrompter([], interactive=False), config, services=services(), open_report=False
    )
    assert result is not None
    run_files = list(Path(summary.run_dir).glob("*.jsonl"))
    assert run_files
    assert result.report.report_metadata["source_files"] == len(run_files)
    rendered = result.path.read_text(encoding="utf-8")
    assert f"{len(run_files)} local metric file" in rendered
    # 17 stale files exist beside the run and are not part of the evidence.
    assert len(list(BASE_DIR.glob("*.jsonl"))) == 17


def test_a_second_run_never_reuses_the_first_runs_directory(workspace):
    config = configured_workspace()
    first = collect_deployments(config, services=services())
    second = collect_deployments(config, services=services())
    assert first.run_dir != second.run_dir
    assert Path(first.run_dir).is_dir() and Path(second.run_dir).is_dir()
    assert first.records_written == second.records_written == 12
    # Each run directory holds only its own buckets.
    for summary in (first, second):
        events = [
            json.loads(line)
            for path in Path(summary.run_dir).glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert {item["deployment_name"] for item in events} == set(ALL_DEPLOYMENTS)


def test_run_directories_are_derived_from_the_configured_output_directory(workspace):
    config = configured_workspace()
    directory, run_id = run_directory(config, now=datetime(2026, 9, 17, 11, 12, 18, tzinfo=UTC))
    assert directory == BASE_DIR / "runs" / "run-20260917T111218Z"
    assert run_id == "run-20260917T111218Z"
    # Opting out keeps the flat layout for anyone who depends on it.
    config.collection.isolate_runs = False
    flat, empty = run_directory(config, now=datetime(2026, 9, 17, tzinfo=UTC))
    assert flat == BASE_DIR and empty == ""


def unresolved_identity_collector():
    """A collector whose metric series never resolve a model name.

    Azure Monitor can answer with token counters and no ``ModelName``
    dimension. The deployment is still known — it is the one that was queried —
    so the record must stay attached to it instead of becoming an ``unknown``
    slice of its own.
    """

    def fake_collect(**kwargs):
        from tokenlens.foundry.monitor import CollectionResult

        window = kwargs["window"]
        result = CollectionResult(
            window_start=window.start, window_end=window.end, collected_at=WINDOW_START
        )
        for name in kwargs["deployments"]:
            for index in range(4):
                result.records.append(
                    MetricBucketRecord(
                        event_id=f"{name}-unresolved-{index}",
                        timestamp=WINDOW_START + timedelta(minutes=5 * index),
                        deployment_name=name,
                        model_name="unknown" if name == "compact-prod" else PRICED_MODEL,
                        deployment_mode="global",
                        metrics=BucketMetrics(input_tokens=900, output_tokens=120, requests=4),
                        window_start=window.start,
                        window_end=window.end,
                        expected_buckets=window.expected_buckets,
                    )
                )
            result.active_buckets += 4
        return result

    return fake_collect


def test_an_unresolved_identity_stays_attached_to_its_deployment(workspace):
    stale_records(BASE_DIR)
    config = configured_workspace()
    summary, result = collect_and_report(
        ScriptedPrompter([], interactive=False),
        config,
        services=services(collect=unresolved_identity_collector()),
        open_report=False,
    )
    assert result is not None
    names = sorted(item.summary.deployment_name for item in result.report.deployments)
    assert names == ALL_DEPLOYMENTS
    # No extra "unknown" deployment slice is invented for the unresolved model.
    assert "unknown" not in names
    unresolved = next(
        item for item in result.report.ptu_analysis.deployments if item.deployment_name == "compact-prod"
    )
    assert unresolved.eligibility_status == "collection_identity_error"
    assert unresolved.recommendation == "Collection identity error"
    assert unresolved.identity_resolved is False
    rendered = result.path.read_text(encoding="utf-8")
    assert "Collection identity error" in rendered
    assert "Model not supported" not in rendered
    # The deployment still reports an outcome under its own name.
    outcome = next(item for item in summary.outcomes if item.deployment == "compact-prod")
    assert outcome.status == "succeeded"


@pytest.mark.parametrize(
    ("scope_answers", "expected_accounts"),
    [
        (["specific", SUBSCRIPTION, "*", "14", "later"], ["example-foundry-account"]),
        (["specific", SUBSCRIPTION, "example-rg/example-foundry-account", "14", "later"], ["example-foundry-account"]),
    ],
)
def test_every_scope_collects_every_deployment_of_the_chosen_accounts(
    workspace, scope_answers, expected_accounts
):
    config, _readiness = configure(ScriptedPrompter(scope_answers), services=services())
    assert [item.account for item in config.foundry.targets] == expected_accounts
    assert config.foundry.deployment_names == ALL_DEPLOYMENTS
    summary = collect_deployments(config, services=services())
    assert sorted(item.deployment for item in summary.succeeded) == ALL_DEPLOYMENTS
    assert summary.accounts == expected_accounts
