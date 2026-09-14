import io
import json
from datetime import UTC, datetime

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.output import timestamped_path
from tokenlens.reports import report_html, report_json, report_sarif


def parsed(value):
    return next(iter_records(io.StringIO(json.dumps(value) + "\n")))


def test_azure_deployment_and_response_model_are_separate():
    record = parsed(
        {
            "request": {"model": "prod-east", "messages": [{"role": "user", "content": "hello"}]},
            "response": {"model": "gpt-4o-2024-08-06", "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
            "provider": "azure_foundry",
        }
    )
    assert record.deployment_name == "prod-east"
    assert record.model_name == "gpt-4o-2024-08-06"
    assert record.model == "gpt-4o-2024-08-06"


def test_multi_deployment_totals_and_formats(tmp_path):
    records = [
        parsed({"deployment_name": "alpha<script>", "model_name": "gpt-4o", "messages": [], "usage": {"input_tokens": 100, "output_tokens": 5}}),
        parsed({"deployment_name": "beta", "model_name": "gpt-4o", "messages": [], "usage": {"input_tokens": 50, "output_tokens": 5}}),
        parsed({"messages": [], "usage": {"input_tokens": 10, "output_tokens": 1}}),
    ]
    report = analyze(records, "fixture.jsonl")
    assert [item.summary.deployment_name for item in report.deployments] == ["alpha<script>", "beta", "unknown"]
    assert sum(item.summary.total_tokens for item in report.deployments) == report.summary.total_tokens
    assert "alpha&lt;script&gt;" in report_html(report)
    assert "alpha&lt;script&gt;" in report_json(report) or "alpha<script>" in report_json(report)
    assert "alpha&lt;script&gt;" not in report_sarif(report)
    assert "alpha<script>" in report_sarif(report)


def test_timestamped_paths_are_utc_and_collision_safe(tmp_path):
    fixed = datetime(2026, 9, 14, 8, 30, 0, tzinfo=UTC)
    first = timestamped_path(report_format="html", output_dir=tmp_path, now=fixed)
    first.write_text("x")
    second = timestamped_path(report_format="html", output_dir=tmp_path, now=fixed)
    assert first.name == "tokenlens-report-20260914-083000Z.html"
    assert second.name == "tokenlens-report-20260914-083000Z-2.html"
