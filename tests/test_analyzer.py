import json

from typer.testing import CliRunner

from tokenlens.analyzer import analyze
from tokenlens.cli import app
from tokenlens.ingest import iter_records


def record(**overrides):
    value = {
        "request_id": "req-1",
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are a support assistant. Follow policy A."},
            {"role": "user", "content": "Classify this request as billing or support."},
        ],
        "tools": [
            {"type": "function", "function": {"name": "lookup_order", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "unused_tool", "parameters": {"type": "object"}}},
        ],
        "max_output_tokens": 4096,
        "usage": {"input_tokens": 1200, "output_tokens": 80},
        "metadata": {"tenant": "tenant-a", "workload": "support"},
    }
    value.update(overrides)
    return value


def test_normalizes_openai_records():
    source = iter_records(
        __import__("io").StringIO(
            json.dumps({"request": record(), "response": {"model": "gpt-4o", "usage": {"prompt_tokens": 42, "completion_tokens": 8}}}) + "\n"
        )
    )
    parsed = next(source)
    assert parsed.model == "gpt-4o"
    assert parsed.usage.input_tokens == 42
    assert parsed.usage.output_tokens == 8


def test_analyzer_reports_findings_and_missing_retrieval():
    messages = [
        {"role": "system", "content": "You are a support assistant. " + "Follow policy A. " * 80},
        {"role": "user", "content": "Classify this request as billing or support."},
    ]
    records = [
        next(
            iter_records(
                __import__("io").StringIO(
                    json.dumps(record(request_id=f"req-{index}", messages=messages)) + "\n"
                )
            )
        )
        for index in range(3)
    ]
    report = analyze(records, "sample.jsonl")
    ids = {finding.rule_id for finding in report.findings}
    assert {"TL001", "TL003", "TL004", "TL006", "TL007", "TL008"} <= ids
    assert report.summary.not_evaluated == 1
    assert report.summary.addressable_max_tokens > 0


def test_cli_writes_json(tmp_path):
    trace_path = tmp_path / "traces.jsonl"
    trace_path.write_text(json.dumps(record()) + "\n", encoding="utf-8")
    output_path = tmp_path / "report.json"
    result = CliRunner().invoke(app, ["analyze", str(trace_path), "--format", "json", "--output", str(output_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["tool"] == "TokenLens for Azure"
