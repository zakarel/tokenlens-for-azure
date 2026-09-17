"""Report shell contract: one router, safe metadata, and a readable type floor.

These assertions lock the HTML/CSS/JS contract that the browser checks verified.
Every fixture is synthetic.
"""

from __future__ import annotations

import io
import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.reports import report_html


def request_record(index: int, *, deployment: str = "chat-route", model: str = "gpt-4.1"):
    raw = {
        "timestamp": (datetime(2026, 9, 1, tzinfo=UTC) + timedelta(minutes=5 * index)).isoformat(),
        "deployment_name": deployment,
        "model_name": model,
        "deployment_mode": "global",
        "messages": [{"role": "user", "content": f"synthetic task {index}"}],
        "usage": {"input_tokens": 1200, "output_tokens": 300},
        "status_code": 200,
    }
    return next(iter_records(io.StringIO(json.dumps(raw) + "\n")))


@pytest.fixture(scope="module")
def rendered() -> str:
    records = [request_record(index) for index in range(120)]
    records += [request_record(index, deployment="batch-route", model="gpt-4.1-mini") for index in range(40)]
    report = analyze(
        records,
        "/private/local-traces/a.jsonl, /private/local-traces/b.jsonl",
        generated_at="2026-09-15T08:12:00Z",
        source_files=2,
        data_classification="local_real",
    )
    return report_html(report)


def test_one_router_owns_the_url_fragment(rendered: str):
    assert "window.tokenlensRouter" in rendered
    assert rendered.count("window.tokenlensRouter = (function()") == 1
    # The PTU panel never writes the fragment itself.
    assert '"#ptu=" + slug' not in rendered
    assert "window.tokenlensRouter.setDeployment(slug)" in rendered
    assert "#tab=" in rendered


def test_unknown_fragments_fall_back_to_overview(rendered: str):
    router = rendered.split("window.tokenlensRouter = (function()", 1)[1].split("})();", 1)[0]
    assert 'tab: "overview"' in router
    assert 'names.indexOf(value.toLowerCase()) >= 0' in router
    assert 'names.indexOf(raw.toLowerCase()) >= 0 ? raw.toLowerCase() : "overview"' in router


def test_tab_labels_have_short_mobile_variants(rendered: str):
    assert '<span class="tab-short" aria-hidden="true">Cost</span>' in rendered
    assert '<span class="tab-short" aria-hidden="true">Usage</span>' in rendered
    assert '<span class="tab-short" aria-hidden="true">PTU</span>' in rendered
    assert 'aria-label="Usage and diagnostics"' in rendered
    style = rendered.split("<style>", 1)[1].split("</style>", 1)[0]
    assert ".tab-short{display:none}" in style
    assert ".tab-full{display:none}.tab-short{display:inline}" in style


def test_tab_navigation_is_sticky_and_touch_sized(rendered: str):
    style = rendered.split("<style>", 1)[1].split("</style>", 1)[0]
    tab_list = style.split(".tab-list{", 1)[1].split("}", 1)[0]
    assert "position:sticky" in tab_list
    assert "overflow-x:auto" in tab_list
    tab_rule = style.split(".tab{border:", 1)[1].split("}", 1)[0]
    assert "min-height:44px" in tab_rule
    assert "white-space:nowrap" in tab_rule


def test_header_carries_no_local_paths_and_labels_real_data(rendered: str):
    header = rendered.split("<header>", 1)[1].split("</header>", 1)[0]
    assert "/private/" not in header
    assert ".jsonl" not in header
    assert "2 local telemetry files" in header
    assert "Local offline analysis" in header
    assert "Offline synthetic example" not in header
    # A human-readable timestamp with the ISO value kept in the tooltip.
    assert "15 Sep 2026 · 08:12 UTC" in header
    assert 'title="2026-09-15T08:12:00Z"' in header


def test_no_decision_data_is_rendered_below_eleven_pixels(rendered: str):
    style = rendered.split("<style>", 1)[1].split("</style>", 1)[0]
    sizes = {int(value) for value in re.findall(r"font-size:(\d+)px", style)}
    sizes |= {int(value) for value in re.findall(r"font:700 (\d+)px", style)}
    assert min(sizes) >= 11, sorted(sizes)
    # 11px is reserved for the legal footer; analytical text is 12px or larger.
    footer_rule = style.split("footer{", 1)[1].split("}", 1)[0]
    assert "font-size:11px" in footer_rule


def test_data_quality_banner_precedes_the_kpi_cards():
    records = [request_record(index, model="unknown") for index in range(3)]
    report = analyze(records, "2 local telemetry files", generated_at="2026-09-15T08:12:00Z")
    html = report_html(report)
    assert html.index('class="data-quality') < html.index('class="metrics"')
    assert "Model identity unresolved" in html
    assert 'role="note"' in html


def test_charts_and_tables_expose_text_alternatives(rendered: str):
    usage = rendered.split('id="usage-panel"', 1)[1].split('id="cost-panel"', 1)[0]
    assert usage.count('role="img"') >= 2
    assert "<title id=" in usage
    assert "<desc id=" in usage
    assert usage.count("<caption>") >= 2
    assert 'class="table-scroll"' in usage


def test_task_economics_overview_renders_with_a_request_report():
    """The optional overview branch must stay callable and source-aware."""
    from tokenlens.economics import TaskEconomicsReport
    from tokenlens.reports import task_economics_html

    report = analyze(
        [request_record(index) for index in range(5)],
        "1 local telemetry file",
        generated_at="2026-09-15T08:12:00Z",
    )
    html = task_economics_html(TaskEconomicsReport(), overview_report=report)
    assert "Portfolio usage" in html
    assert "<b>5</b>" in html
