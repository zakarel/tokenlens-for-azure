import io
import json

from tokenlens.analyzer import analyze
from tokenlens.cli import app
from tokenlens.ingest import iter_records, load_records
from tokenlens.models import AzureRecommendation, Estimate, Finding
from tokenlens.presentation import (
    additional_opportunities,
    deployment_rollups,
    model_rollups,
    overview_findings,
)
from tokenlens.reports import report_html, report_json
from typer.testing import CliRunner


def parsed(value):
    return next(iter_records(io.StringIO(json.dumps(value) + "\n")))


def finding(*, estimate: Estimate, percent: float | None) -> Finding:
    return Finding(
        rule_id="TLTEST",
        severity="low",
        title="Synthetic opportunity",
        detail="Synthetic test finding.",
        estimated_savings=estimate,
        impact_min_percent=percent,
        impact_max_percent=percent,
        confidence="low",
        azure_recommendation=AzureRecommendation(
            service="Azure OpenAI",
            capability="Synthetic action",
            action="Validate this synthetic opportunity.",
        ),
    )


def base_report(*findings: Finding):
    report = analyze(
        [
            parsed(
                {
                    "deployment_name": "reasoning-prod",
                    "model_name": "Claude-opus-5",
                    "messages": [],
                    "usage": {"input_tokens": 1000000, "output_tokens": 100},
                }
            ),
        ],
        "synthetic.jsonl",
    )
    return report.model_copy(update={"findings": list(findings)})


def test_low_volume_point_six_percent_is_hidden_but_retained():
    report = base_report(finding(estimate=Estimate(min_tokens=50000, max_tokens=60000), percent=0.6))
    assert overview_findings(report) == []
    assert len(additional_opportunities(report)) == 1


def test_point_six_percent_large_absolute_volume_is_visible():
    report = base_report(finding(estimate=Estimate(min_tokens=120000, max_tokens=150000), percent=0.6))
    assert len(overview_findings(report)) == 1


def test_one_percent_boundary_and_call_impact_use_their_units():
    boundary = base_report(finding(estimate=Estimate(min_tokens=1, max_tokens=1), percent=1.0))
    calls = base_report(finding(estimate=Estimate(min_tokens=4, max_tokens=4, unit="calls"), percent=0.1))
    assert len(overview_findings(boundary)) == 1
    assert len(overview_findings(calls)) == 1


def test_duplicate_models_combine_but_deployments_stay_separate():
    report = analyze(
        [
            parsed({"deployment_name": "reasoning-prod", "model_name": "Claude-opus-5", "usage": {"input_tokens": 10, "output_tokens": 2}}),
            parsed({"deployment_name": "reasoning-batch", "model_name": "claude-opus-5", "usage": {"input_tokens": 20, "output_tokens": 3}}),
            parsed({"deployment_name": "general-prod", "model_name": "gpt-5.6-luna", "usage": {"input_tokens": 7, "output_tokens": 1}}),
        ],
        "synthetic.jsonl",
    )
    assert [item.deployment_name for item in deployment_rollups(report)] == [
        "reasoning-batch",
        "reasoning-prod",
        "general-prod",
    ]
    models = model_rollups(report)
    assert len(models) == 2
    assert models[0].requests == 2
    assert sum(item.total_tokens for item in models) == report.summary.total_tokens
    assert sum(item.requests or 0 for item in models) == report.summary.requests_analyzed


def test_generated_report_has_two_accessible_chart_views_and_metadata():
    report = base_report(finding(estimate=Estimate(min_tokens=120000, max_tokens=150000), percent=12.0))
    html = report_html(report)
    payload = json.loads(report_json(report))
    assert 'role="tablist"' in html
    assert 'role="tabpanel"' in html
    # One model in scope, so composition replaces a single-slice donut.
    assert 'Token composition' in html
    assert 'Token usage by deployment' in html
    assert "overview_min_impact_tokens" in payload["report_metadata"]["materiality"]
    # Scoped to the executive views: the PTU Advisor tab carries its own
    # documented evidence-confidence sections.
    executive_views = html.split("<body>", 1)[1].split('<section class="tab-panel ptu"', 1)[0]
    assert "confidence" not in executive_views.lower()


def test_multiple_models_keep_the_share_donut():
    report = analyze(
        [
            parsed(
                {
                    "deployment_name": "reasoning-prod",
                    "model_name": "Claude-opus-5",
                    "messages": [],
                    "usage": {"input_tokens": 1000000, "output_tokens": 100},
                }
            ),
            parsed(
                {
                    "deployment_name": "general-prod",
                    "model_name": "gpt-4.1",
                    "messages": [],
                    "usage": {"input_tokens": 400000, "output_tokens": 100},
                }
            ),
        ],
        "synthetic.jsonl",
    )
    html = report_html(report)
    assert "Token share by model" in html
    assert 'class="donut' in html


def test_synthetic_fixture_has_requested_totals():
    records, _ = load_records("examples/multi-deployment-portfolio.jsonl")
    assert len(records) == 7548
    by_deployment = {}
    for record in records:
        by_deployment[record.deployment_name] = by_deployment.get(record.deployment_name, 0) + 1
    assert by_deployment == {
        "reasoning-prod": 3220,
        "general-prod": 2160,
        "reasoning-batch": 1680,
        "security-prod": 488,
    }


def test_cli_applies_report_materiality_configuration(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({"deployment_name": "demo", "model_name": "demo-model", "usage": {"input_tokens": 10}}) + "\n")
    config = tmp_path / ".tokenlens.yml"
    config.write_text("report:\n  overview_min_impact_percent: 2.5\n  overview_min_impact_tokens: 42\n  overview_max_findings: 2\n")
    output = tmp_path / "report.json"
    result = CliRunner().invoke(app, ["analyze", str(trace), "--config", str(config), "--format", "json", "--output", str(output)])
    assert result.exit_code == 0, result.output
    payload = json.loads(output.read_text())
    assert payload["report_metadata"]["materiality"] == {
        "overview_min_impact_percent": 2.5,
        "overview_min_impact_tokens": 42,
        "overview_max_findings": 2,
    }
