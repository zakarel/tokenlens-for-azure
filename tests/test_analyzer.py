import json

from typer.testing import CliRunner

from tokenlens.analyzer import analyze
from tokenlens.cli import app
from tokenlens.ingest import iter_records
from tokenlens.reports import report_html


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


def test_analyzer_reports_findings_without_missing_retrieval_finding():
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
    assert {"TL001", "TL003", "TL006", "TL007", "TL008"} <= ids
    assert "TL004" not in ids
    assert all(finding.title != "Not evaluated" for finding in report.findings)
    assert all(finding.impact_min_percent is not None for finding in report.findings if finding.rule_id != "TL008")
    assert report.summary.addressable_max_tokens > 0


def test_cli_writes_json(tmp_path):
    trace_path = tmp_path / "traces.jsonl"
    trace_path.write_text(json.dumps(record()) + "\n", encoding="utf-8")
    output_path = tmp_path / "report.json"
    result = CliRunner().invoke(app, ["analyze", str(trace_path), "--format", "json", "--output", str(output_path)])
    assert result.exit_code == 0, result.output
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["tool"] == "TokenLens for Azure"


def test_html_report_embeds_logo():
    records = [next(iter_records(__import__("io").StringIO(json.dumps(record()) + "\n")))]
    rendered = report_html(analyze(records, "sample.jsonl"))
    assert 'src="data:image/png;base64,' in rendered
    assert 'alt="TokenLens for Azure logo"' in rendered
    header = rendered.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "<h1>" not in header
    assert "Token efficiency assessment" not in header
    # The header states how much was read, never where it was read from.
    assert "sample.jsonl" not in header
    assert "local telemetry file" in header
    # Internal rule confidence jargon stays out of the executive views. The PTU
    # Advisor tab has its own documented evidence confidence score, so it is
    # scoped out of this assertion.
    executive_views = rendered.split("<body>", 1)[1].split('<section class="tab-panel ptu"', 1)[0]
    assert "confidence" not in executive_views.lower()
    assert "tokenlens-for-azure · created by Tzahi Ariel" in rendered


def test_combined_report_keeps_brand_tabs_and_usage_charts():
    records = [
        next(iter_records(__import__("io").StringIO(json.dumps(record()) + "\n"))),
        next(
            iter_records(
                __import__("io").StringIO(
                    json.dumps(record(model="gpt-4o-mini", deployment_name="second-route", model_name="gpt-4o-mini")) + "\n"
                )
            )
        ),
    ]
    report = analyze(records, "sample.jsonl")
    from tokenlens.economics import TaskEconomicsReport

    rendered = report_html(report.model_copy(update={"task_economics": TaskEconomicsReport()}))
    assert 'alt="TokenLens for Azure logo"' in rendered
    assert rendered.count('<button class="tab"') == 4
    # Full labels stay in the accessibility tree; short labels are shown on
    # narrow viewports so a tab never wraps onto two lines.
    assert "<span class=\"tab-full\">Overview</span>" in rendered
    assert "<span class=\"tab-full\">Cost analysis</span>" in rendered
    assert "<span class=\"tab-full\">Usage &amp; diagnostics</span>" in rendered
    assert "<span class=\"tab-full\">PTU advisor</span>" in rendered
    assert "<span class=\"tab-short\" aria-hidden=\"true\">Cost</span>" in rendered
    assert "Token share by model" in rendered
    assert "Token usage by deployment" in rendered
    assert 'class="donut' in rendered
    assert 'class="columns' in rendered
    assert "Task economics</button>" not in rendered
    assert 'style="fill:none;stroke:#73c7ff;stroke-width:28"' in rendered
    assert "Model summary" in rendered
    assert rendered.index("Model summary") < rendered.index("Evaluated findings")


def test_single_model_replaces_the_donut_with_composition():
    records = [next(iter_records(__import__("io").StringIO(json.dumps(record()) + "\n")))]
    rendered = report_html(analyze(records, "sample.jsonl"))
    assert "Token composition" in rendered
    assert 'class="donut' not in rendered
