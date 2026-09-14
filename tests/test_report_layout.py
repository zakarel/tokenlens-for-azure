"""Full-width responsive report shell assertions.

These are static assertions against the generated CSS/markup (no headless
browser is available in this environment), matching the plan's acceptance
criteria: no fixed 1320px column, no analytical text below 10px, the
desktop-compacting media query is removed, and the KPI grid is fluid.
"""

import re

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.reports import report_html

import io
import json


def parsed(value):
    return next(iter_records(io.StringIO(json.dumps(value) + "\n")))


def _minimal_report_html() -> str:
    record = parsed(
        {
            "timestamp": "2026-09-14T12:00:00Z",
            "deployment_name": "demo",
            "model_name": "layout-test-model",
            "provider": "azure_foundry",
            "deployment_mode": "global",
            "messages": [],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
    )
    report = analyze([record], "fixture", use_bundled_reference=False, generated_at="2026-09-14T13:00:00Z")
    return report_html(report)


def _all_font_sizes(html: str) -> list[int]:
    return [int(value) for value in re.findall(r"font-size:(\d+)px", html)]


def test_report_shell_has_no_fixed_desktop_max_width():
    html = _minimal_report_html()
    assert "max-width:1320px" not in html
    assert "main{width:100%" in html or "main{{width:100%" in html.replace("{{", "{")


def test_report_shell_removes_desktop_compacting_media_query():
    html = _minimal_report_html()
    assert "min-width:961px) and (max-width:1400px)" not in html


def test_report_shell_uses_fluid_kpi_grid():
    html = _minimal_report_html()
    assert "repeat(auto-fit,minmax(170px,1fr))" in html


def test_report_html_has_no_sub_10px_analytical_text():
    html = _minimal_report_html()
    sizes = _all_font_sizes(html)
    assert sizes, "expected at least one font-size declaration"
    assert min(sizes) >= 10


def test_report_html_shell_declares_full_viewport_min_height():
    html = _minimal_report_html()
    assert "min-height:100vh" in html


def test_report_html_body_has_viewport_meta_for_responsive_scaling():
    html = _minimal_report_html()
    assert 'name="viewport" content="width=device-width,initial-scale=1"' in html
