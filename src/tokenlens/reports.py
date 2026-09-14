from __future__ import annotations

import base64
import html
import json
import math
from importlib.resources import files
from pathlib import Path

from .models import AnalysisReport, DeploymentAnalysis, Finding
from .economics import TaskEconomicsReport
from .presentation import (
    additional_opportunities,
    chart_label,
    deployment_rollups,
    impact_category,
    model_rollups,
    overview_findings,
    top_recommendations,
)


def _without_internal(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_internal(item) for key, item in value.items() if key != "confidence"}
    if isinstance(value, list):
        return [_without_internal(item) for item in value]
    return value


def report_json(report: AnalysisReport | TaskEconomicsReport) -> str:
    if isinstance(report, TaskEconomicsReport):
        return report.model_dump_json(indent=2) + "\n"
    return json.dumps(_without_internal(report.model_dump(mode="json")), indent=2, ensure_ascii=False) + "\n"


def report_sarif(report: AnalysisReport) -> str:
    results = []
    for finding in report.findings:
        level = "error" if finding.severity == "high" else "warning" if finding.severity == "medium" else "note"
        results.append(
            {
                "ruleId": finding.rule_id,
                "level": level,
                "message": {"text": f"{finding.title}: {finding.detail}"},
                "properties": {
                    "impact_min_percent": finding.impact_min_percent,
                    "impact_max_percent": finding.impact_max_percent,
                    "azure_service": finding.azure_recommendation.service,
                    "azure_capability": finding.azure_recommendation.capability,
                },
            }
        )
    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "TokenLens for Azure", "version": report.version, "rules": _without_internal(report.rules)}},
            "properties": {
                "deployments": [deployment.summary.deployment_name for deployment in report.deployments],
                "deployment_summaries": [
                    {
                        "deployment_name": deployment.summary.deployment_name,
                        "model_name": deployment.summary.model_name,
                        "total_tokens": deployment.summary.total_tokens,
                    }
                    for deployment in report.deployments
                ],
            },
            "results": results,
        }],
    }
    return json.dumps(payload, indent=2) + "\n"


def _logo_data_uri() -> str:
    logo = files("tokenlens").joinpath("assets/tokenlens-logo-dark.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(logo).decode("ascii")


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _display_name(value: str, *, unknown: str) -> str:
    return unknown if value in {"", "unknown", "None"} else value


def _finding_impact(finding: Finding) -> str:
    estimate = finding.estimated_savings
    if estimate.unit == "calls":
        count = estimate.max_tokens or estimate.min_tokens or 0
        return f"{count:,} calls"
    if estimate.min_tokens is None:
        return "Evaluation opportunity"
    maximum = estimate.max_tokens if estimate.max_tokens is not None else estimate.min_tokens
    if estimate.min_tokens == maximum:
        return f"{estimate.min_tokens:,} tokens"
    return f"{estimate.min_tokens:,}–{maximum:,} tokens"


def _impact_percent(finding: Finding) -> str:
    if finding.impact_min_percent is None:
        return "Evaluation opportunity"
    maximum = finding.impact_max_percent if finding.impact_max_percent is not None else finding.impact_min_percent
    if finding.impact_min_percent == maximum:
        return f"{finding.impact_min_percent:.1f}%"
    return f"{finding.impact_min_percent:.1f}–{maximum:.1f}%"


def _finding_card(finding: Finding, *, compact: bool = False) -> str:
    recommendation = finding.azure_recommendation
    return f"""<article class="finding-card {_escape(finding.severity)}">
      <div class="finding-rule">{_escape(finding.rule_id)}</div>
      <div class="finding-copy"><strong>{_escape(finding.title)}</strong>
        <span>{_escape(finding.detail if not compact else recommendation.action)}</span></div>
      <div class="finding-impact"><strong>{_escape(_impact_percent(finding))}</strong>
        <span>{_escape(_finding_impact(finding))} · {_escape(impact_category(finding))}</span></div>
      <div class="finding-action"><strong>{_escape(recommendation.capability)}</strong>
        <span>{_escape(recommendation.action if not compact else recommendation.service)}</span></div>
    </article>"""


def _finding_cards(findings: list[Finding], *, compact: bool = False) -> str:
    if not findings:
        return '<p class="empty">No addressable opportunities were detected in this trace set.</p>'
    return "".join(_finding_card(item, compact=compact) for item in findings)


def _deployment_rows(report: AnalysisReport) -> str:
    rows = []
    for item in deployment_rollups(report):
        name = _display_name(item.deployment_name, unknown="Unknown deployment")
        model = _display_name(item.model_name, unknown="Unknown model")
        rows.append(
            f"""<div class="portfolio-row">
              <div><strong>{_escape(name)}</strong><small>{_escape(model)}</small></div>
              <div class="portfolio-bar" aria-label="{_escape(chart_label(name, item.total_tokens, item.token_share_percent))}"><span style="width:{max(1, item.token_share_percent)}%"></span></div>
              <div class="portfolio-value"><strong>{item.total_tokens:,}</strong><small>{item.token_share_percent:.1f}% · {item.requests:,} requests</small></div>
            </div>"""
        )
    return "".join(rows) or '<p class="empty">No deployments observed.</p>'


def _overview_actions(findings: list[Finding]) -> str:
    if not findings:
        return '<p class="empty">No prioritized Azure actions.</p>'
    return "".join(
        f'<li><strong>{_escape(item.azure_recommendation.capability)}</strong><span>{_escape(item.azure_recommendation.action)}</span></li>'
        for item in findings
    )


def _donut_chart(report: AnalysisReport) -> tuple[str, str]:
    models = model_rollups(report)
    total = max(1, report.summary.total_tokens)
    colors = ["#73c7ff", "#57d68b", "#ffc857", "#ff8fa3", "#b9a7ff", "#5eead4", "#f4a261"]
    radius, circumference = 76, 2 * math.pi * 76
    offset = 0.0
    segments = []
    rows = []
    for index, item in enumerate(models):
        fraction = item.total_tokens / total
        length = circumference * fraction
        label = chart_label(item.model_name, item.total_tokens, item.token_share_percent)
        color = colors[index % len(colors)]
        segments.append(
            f'<circle class="donut-segment" cx="100" cy="100" r="{radius}" fill="none" stroke="{color}" '
            f'stroke-width="28" stroke-dasharray="{length:.4f} {circumference - length:.4f}" '
            f'stroke-dashoffset="{-offset:.4f}" tabindex="0" aria-label="{_escape(label)}"><title>{_escape(label)}</title></circle>'
        )
        rows.append(
            f'<tr><th scope="row"><span class="swatch" style="background:{color}"></span>{_escape(item.model_name)}</th>'
            f'<td>{item.total_tokens:,}</td><td>{item.token_share_percent:.1f}%</td><td>{item.requests:,}</td></tr>'
        )
        offset += length
    svg = f"""<svg class="donut" viewBox="0 0 200 200" role="img" aria-labelledby="donut-title donut-desc">
      <title id="donut-title">Token share by model</title>
      <desc id="donut-desc">Each segment represents a model's share of {report.summary.total_tokens:,} portfolio tokens.</desc>
      <circle cx="100" cy="100" r="{radius}" fill="none" stroke="#294365" stroke-width="28"></circle>
      <g transform="rotate(-90 100 100)">{''.join(segments)}</g>
      <text x="100" y="96" text-anchor="middle" class="donut-total">{report.summary.total_tokens:,}</text>
      <text x="100" y="113" text-anchor="middle" class="donut-label">total tokens</text>
    </svg>"""
    table = f"""<table class="data-table"><caption>Accessible model token share data</caption>
      <thead><tr><th scope="col">Model</th><th scope="col">Tokens</th><th scope="col">Share</th><th scope="col">Requests</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="4">No model usage observed.</td></tr>'}</tbody>
    </table>"""
    return svg, table


def _column_chart(report: AnalysisReport) -> tuple[str, str]:
    deployments = deployment_rollups(report)
    max_total = max((item.total_tokens for item in deployments), default=1)
    width, height, chart_top, chart_bottom = 760, 300, 22, 220
    plot_height = chart_bottom - chart_top
    count = max(1, len(deployments))
    bar_width = min(112, max(28, 680 // count - 10))
    gap = max(8, (680 - count * bar_width) // (count + 1))
    start = 50
    colors = {"input": "#73c7ff", "output": "#57d68b", "cached": "#ffc857"}
    rects = []
    rows = []
    for index, item in enumerate(deployments):
        x = start + index * (bar_width + gap)
        y = chart_bottom
        # Cached input is a subset of input, so chart it as a separate portion
        # while subtracting it from the regular input segment to keep columns
        # reconciled with the report's total token count.
        parts = [
            ("input", max(0, item.input_tokens - item.cached_tokens)),
            ("output", item.output_tokens),
            ("cached", item.cached_tokens),
        ]
        for key, value in parts:
            if not value:
                continue
            part_height = value / max_total * plot_height
            y -= part_height
            label = f"{item.deployment_name}: {'uncached input' if key == 'input' else key} {value:,} tokens"
            rects.append(
                f'<rect x="{x}" y="{y:.2f}" width="{bar_width}" height="{part_height:.2f}" fill="{colors[key]}" '
                f'tabindex="0" aria-label="{_escape(label)}"><title>{_escape(label)}</title></rect>'
            )
        label = item.deployment_name
        rows.append(
            f'<tr><th scope="row">{_escape(label)}</th><td>{_escape(item.model_name)}</td><td>{item.input_tokens:,}</td>'
            f'<td>{item.output_tokens:,}</td><td>{item.cached_tokens:,}</td><td>{item.total_tokens:,}</td></tr>'
        )
        rects.append(
            f'<text x="{x + bar_width / 2:.1f}" y="244" text-anchor="middle" class="axis-label">{_escape(label[:18])}</text>'
        )
        rects.append(
            f'<text x="{x + bar_width / 2:.1f}" y="260" text-anchor="middle" class="axis-value">{item.total_tokens:,}</text>'
        )
    svg = f"""<svg class="columns" viewBox="0 0 {width} {height}" role="img" aria-labelledby="column-title column-desc">
      <title id="column-title">Token usage by deployment</title>
      <desc id="column-desc">Stacked input, output, and cached token columns sorted by total usage.</desc>
      <line x1="38" y1="{chart_bottom}" x2="740" y2="{chart_bottom}" class="axis"></line>
      <line x1="38" y1="{chart_top}" x2="38" y2="{chart_bottom}" class="axis"></line>
      {''.join(rects)}
    </svg>"""
    table = f"""<table class="data-table"><caption>Accessible deployment token usage data</caption>
      <thead><tr><th scope="col">Deployment</th><th scope="col">Model</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col">Cached</th><th scope="col">Total</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="6">No deployment usage observed.</td></tr>'}</tbody>
    </table>"""
    return svg, table


def _analytics_deployment_sections(report: AnalysisReport) -> str:
    sections = []
    for deployment in deployment_rollups(report):
        analysis = next(
            (item for item in report.deployments if item.summary.deployment_name == deployment.deployment_name),
            None,
        )
        findings = analysis.findings if analysis else []
        sections.append(
            f"""<details class="deployment-details"><summary><strong>{_escape(_display_name(deployment.deployment_name, unknown="Unknown deployment"))}</strong>
              <span>{_escape(deployment.model_name)} · {deployment.requests:,} requests · {deployment.total_tokens:,} tokens</span></summary>
              <div class="detail-body"><div class="mini-stats"><span>Input <strong>{deployment.input_tokens:,}</strong></span>
              <span>Output <strong>{deployment.output_tokens:,}</strong></span><span>Cached <strong>{deployment.cached_tokens:,}</strong></span>
              <span>Retries <strong>{deployment.retries:,}</strong></span></div>{_finding_cards(findings)}</div>
            </details>"""
        )
    return "".join(sections) or '<p class="empty">No deployments observed.</p>'


def report_html(report: AnalysisReport) -> str:
    if isinstance(report.task_economics, TaskEconomicsReport):
        return task_economics_html(report.task_economics, overview_report=report)
    summary = report.summary
    overview = overview_findings(report)
    additional = additional_opportunities(report)
    actions = top_recommendations(overview)
    donut_svg, donut_table = _donut_chart(report)
    column_svg, column_table = _column_chart(report)
    hidden_count = max(0, len(report.findings) - len(overview))
    config = report.report_metadata.get("materiality", {})
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TokenLens for Azure — Report</title>
<style>
:root{{--bg:#11213b;--surface:#172a49;--surface-2:#1d3559;--border:#35527a;--text:#f3f7ff;--muted:#b5c5dc;--soft:#d2deee;--accent:#73c7ff;--green:#57d68b;--amber:#ffc857;--pink:#ff8fa3;--shadow:0 12px 30px rgba(0,0,0,.22)}}
*{{box-sizing:border-box}}html{{background:var(--bg)}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 "Segoe UI",Aptos,Calibri,Arial,sans-serif}}main{{max-width:1320px;margin:auto;padding:20px 26px 18px}}header{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:14px;margin-bottom:14px}}.logo{{width:min(275px,55vw);height:auto;display:block}}.meta{{text-align:right;color:var(--muted);font-size:11px;line-height:1.5}}.meta strong{{color:var(--text);font-size:12px;margin-left:5px}}h1,h2,h3,p{{margin:0}}h2{{font-size:17px;letter-spacing:-.02em}}h3{{font-size:14px}}small,.muted{{display:block;color:var(--muted);font-size:11px}}.card,.panel,.chart-card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow)}}.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:12px}}.card{{padding:11px 13px;min-height:76px}}.label{{color:var(--muted);font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em}}.value{{font-size:22px;font-weight:700;letter-spacing:-.04em;margin-top:4px}}.sub{{font-size:10px;color:var(--muted);margin-top:2px}}.accent{{color:var(--accent)}}.overview-grid{{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(280px,.9fr);gap:12px;margin-bottom:12px}}.panel-head{{padding:11px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:baseline;gap:12px}}.panel-head a,.tab{{color:var(--link,var(--accent));font-weight:700}}.portfolio{{padding:0 14px}}.portfolio-row{{display:grid;grid-template-columns:1.15fr 2fr 145px;gap:12px;align-items:center;padding:9px 0;border-bottom:1px solid var(--border)}}.portfolio-row:last-child{{border-bottom:0}}.portfolio-row strong{{font-size:12px}}.portfolio-row small{{margin-top:1px;font-size:10px}}.portfolio-bar{{height:9px;border-radius:9px;background:var(--surface-2);overflow:hidden}}.portfolio-bar span{{height:100%;display:block;background:var(--accent);border-radius:9px}}.portfolio-value{{text-align:right}}.portfolio-value strong{{display:block;font-size:12px}}.portfolio-value small{{font-size:10px}}.actions{{padding:9px 14px 10px}}.actions ol{{padding:0 0 0 20px;margin:0}}.actions li{{padding:5px 0 5px 2px;border-bottom:1px solid var(--border)}}.actions li:last-child{{border-bottom:0}}.actions li strong,.actions li span{{display:block;font-size:11px}}.actions li span{{color:var(--muted);font-size:10px;margin-top:1px}}.finding-panel{{margin-bottom:12px}}.finding-list{{display:grid;grid-template-columns:repeat(3,1fr)}}.finding-card{{display:grid;grid-template-columns:52px minmax(0,1.3fr) minmax(86px,.8fr);gap:9px;align-items:start;padding:10px 12px;border-right:1px solid var(--border);border-bottom:1px solid var(--border)}}.finding-card:nth-child(3n){{border-right:0}}.finding-card:last-child{{border-bottom:0}}.finding-rule{{font:700 10px Consolas,monospace;border-left:4px solid var(--accent);padding-left:6px;color:var(--soft)}}.finding-card.high .finding-rule{{border-color:var(--pink)}}.finding-card.medium .finding-rule{{border-color:var(--amber)}}.finding-card.low .finding-rule{{border-color:var(--accent)}}.finding-copy strong,.finding-impact strong,.finding-action strong{{display:block;font-size:11px}}.finding-copy span,.finding-impact span,.finding-action span{{display:block;color:var(--muted);font-size:10px;margin-top:2px}}.finding-impact strong{{color:var(--accent)}}.finding-action{{grid-column:2 / -1}}.finding-action strong{{color:var(--green)}}.tabs{{margin-top:4px}}.tab-list{{display:flex;gap:5px;border-bottom:1px solid var(--border);margin-bottom:14px}}.tab{{border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;background:transparent;padding:8px 13px;color:var(--muted);cursor:pointer;font:700 12px inherit}}.tab[aria-selected="true"]{{color:var(--text);background:var(--surface);border-color:var(--border)}}.tab:focus-visible,.donut-segment:focus-visible,.columns rect:focus-visible,summary:focus-visible{{outline:3px solid var(--amber);outline-offset:2px}}.js .tab-panel:not(.active){{display:none}}.tab-panel{{min-height:200px}}.analytics-intro{{color:var(--muted);font-size:12px;margin-bottom:12px}}.chart-grid{{display:grid;grid-template-columns:1fr 1.35fr;gap:12px;margin-bottom:12px}}.chart-card{{padding:14px;min-width:0}}.chart-card h2{{margin-bottom:3px}}.chart-card > p{{color:var(--muted);font-size:11px;margin-bottom:10px}}.donut-wrap{{display:flex;align-items:center;gap:12px;min-height:220px}}.donut{{width:220px;max-width:42%;overflow:visible}}.donut-total{{fill:var(--text);font-size:16px;font-weight:700}}.donut-label{{fill:var(--muted);font-size:9px}}.columns{{width:100%;height:auto;min-height:220px;overflow:visible}}.columns .axis{{stroke:var(--border);stroke-width:1}}.axis-label{{fill:var(--soft);font-size:10px}}.axis-value{{fill:var(--muted);font-size:9px}}.legend{{display:grid;gap:4px;min-width:130px}}.legend-row{{font-size:10px;color:var(--soft)}}.swatch{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:0}}.data-table{{width:100%;border-collapse:collapse;font-size:10px;margin-top:10px;color:var(--soft)}}.data-table caption{{text-align:left;color:var(--muted);font-size:10px;margin-bottom:4px}}.data-table th,.data-table td{{padding:5px 6px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}}.data-table th:first-child,.data-table td:first-child{{text-align:left}}.data-table thead th{{color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.04em}}.analytics-section{{margin-bottom:12px}}.analytics-section > .panel-head{{margin-bottom:0}}.details-list{{display:grid;gap:7px}}.deployment-details{{background:var(--surface);border:1px solid var(--border);border-radius:9px;overflow:hidden}}summary{{cursor:pointer;padding:10px 13px;list-style-position:inside}}summary span{{color:var(--muted);font-size:11px;margin-left:10px}}.detail-body{{border-top:1px solid var(--border);padding:10px 13px}}.mini-stats{{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:10px;margin-bottom:7px}}.mini-stats strong{{color:var(--text);margin-left:3px}}.detail-body .finding-card{{background:var(--surface-2);border:1px solid var(--border);border-radius:7px;margin-top:6px;grid-template-columns:52px minmax(0,1.3fr) minmax(86px,.8fr)}}.additional{{border-left:3px solid var(--amber)}}.empty{{padding:12px 14px;color:var(--muted);font-size:11px}}footer{{text-align:center;color:var(--muted);font-size:9px;letter-spacing:.04em;margin-top:12px}}@media(max-width:960px){{main{{padding:16px}}.metrics{{grid-template-columns:repeat(3,1fr)}}.overview-grid,.chart-grid{{grid-template-columns:1fr}}.finding-list{{grid-template-columns:1fr}}.finding-card,.finding-card:nth-child(3n){{border-right:0}}.donut-wrap{{justify-content:center}}}}@media(max-width:620px){{header{{display:block}}.meta{{text-align:left;margin-top:8px}}.metrics{{grid-template-columns:repeat(2,1fr)}}.portfolio-row{{grid-template-columns:1fr 100px}}.portfolio-bar{{grid-column:1 / -1;grid-row:2}}.portfolio-value{{text-align:right}}.donut-wrap{{display:block}}.donut{{display:block;max-width:220px;margin:auto}}.data-table{{display:block;overflow-x:auto}}summary span{{display:block;margin:3px 0 0 21px}}}}@media print{{@page{{size:landscape;margin:.35in}}body{{background:#fff;color:#11213b;font-size:10px}}main{{max-width:none;padding:0}}header{{border-color:#9aa8ba}}.card,.panel,.chart-card,.deployment-details{{box-shadow:none;background:#fff;border-color:#9aa8ba}}.metrics{{gap:5px}}.value{{font-size:16px}}.overview{{min-height:6.8in;page-break-after:always}}.usage{{page-break-before:always}}.tab-list{{display:none}}.js .tab-panel:not(.active){{display:block}}.finding-card,.portfolio-row{{border-color:#b8c2cf}}.finding-copy span,.finding-impact span,.finding-action span,.analytics-intro,.data-table caption,small,.muted{{color:#46556b}}.data-table th,.data-table td{{border-color:#b8c2cf}}footer{{color:#46556b}}}}
@media(min-width:961px) and (max-width:1400px){{main{{padding:10px 20px 6px}}.logo{{width:240px}}.tab-list{{margin-bottom:8px}}.metrics{{gap:7px;margin-bottom:8px}}.card{{padding:8px 10px;min-height:68px}}.overview-grid{{gap:8px;margin-bottom:8px}}.panel-head{{padding:8px 11px}}.portfolio{{padding:0 11px}}.portfolio-row{{padding:6px 0}}.actions{{padding:6px 11px}}.actions li{{padding:3px 0}}.finding-card{{padding:7px 10px}}.finding-panel{{margin-bottom:8px}}footer{{margin-top:7px}}}}
</style></head><body><main>
<header><div><img class="logo" src="data:image/png;base64,{_logo_data_uri().split(',',1)[1]}" alt="TokenLens for Azure logo"></div>
<div class="meta">Generated<strong>{_escape(report.generated_at)}</strong> · Source<strong>{_escape(report.source)}</strong> · Offline synthetic example</div></header>
<div class="tabs"><div class="tab-list" role="tablist" aria-label="Report views">
<button class="tab" id="overview-tab" role="tab" aria-selected="true" aria-controls="overview-panel" tabindex="0">Overview</button>
<button class="tab" id="task-tab" role="tab" aria-selected="false" aria-controls="task-panel" tabindex="-1">Task economics</button>
<button class="tab" id="usage-tab" role="tab" aria-selected="false" aria-controls="usage-panel" tabindex="-1">Usage &amp; diagnostics</button></div>
<section class="tab-panel overview active" id="overview-panel" role="tabpanel" aria-labelledby="overview-tab">
<section class="metrics"><div class="card"><div class="label">Requests</div><div class="value">{summary.requests_analyzed:,}</div><div class="sub">{summary.retries:,} retries observed</div></div>
<div class="card"><div class="label">Total tokens</div><div class="value">{summary.total_tokens:,}</div><div class="sub">{summary.input_tokens:,} input · {summary.output_tokens:,} output</div></div>
<div class="card"><div class="label">Deployments</div><div class="value">{len(report.deployments):,}</div><div class="sub">Separate workload routes</div></div>
<div class="card"><div class="label">Optimisation scenarios</div><div class="value accent">{len(report.report_metadata.get("scenarios", [])):,}</div><div class="sub">Independent ranges · not additive</div></div>
<div class="card"><div class="label">Material findings</div><div class="value">{len(overview):,}</div><div class="sub">{summary.findings:,} total in analytics</div></div></section>
<section class="overview-grid"><section class="panel"><div class="panel-head"><div><h2>Portfolio usage</h2><small>Token share by deployment</small></div><a href="#usage" data-tab-link="usage">View analytics →</a></div><div class="portfolio">{_deployment_rows(report)}</div></section>
<aside class="panel"><div class="panel-head"><div><h2>Azure actions</h2><small>Prioritized for a first decision</small></div></div><div class="actions"><ol>{_overview_actions(actions)}</ol></div></aside></section>
<section class="panel finding-panel"><div class="panel-head"><div><h2>Material findings</h2><small>Overview prioritizes results at or above {float(config.get("overview_min_impact_percent", 1.0)):.1f}% impact or {int(config.get("overview_min_impact_tokens", 100000)):,} tokens.</small></div></div><div class="finding-list">{_finding_cards(overview, compact=True)}</div>
{f'<p class="empty">{hidden_count:,} additional opportunities are available in Usage analytics.</p>' if hidden_count else ""}</section>
<footer>tokenlens-for-azure · created by Tzahi Ariel</footer></section>
<section class="tab-panel usage" id="usage-panel" role="tabpanel" aria-labelledby="usage-tab">
<p class="analytics-intro">Usage analytics retains every finding. Percentages are relative to the relevant deployment or portfolio, and absolute token or call volume is shown alongside them. Low materiality does not mean invalid; it means lower priority for the first executive decision.</p>
<section class="chart-grid"><article class="chart-card"><h2>Token share by model</h2><p>Case variants of the same model family combine into one model slice.</p><div class="donut-wrap">{donut_svg}<div class="legend">{''.join(f'<div class="legend-row"><span class="swatch" style="background:{["#73c7ff","#57d68b","#ffc857","#ff8fa3","#b9a7ff","#5eead4","#f4a261"][i % 7]}"></span>{_escape(m.model_name)} · {m.total_tokens:,} · {m.token_share_percent:.1f}%</div>' for i,m in enumerate(model_rollups(report)))}</div></div>{donut_table}</article>
<article class="chart-card"><h2>Token usage by deployment</h2><p>Input, output, and cached tokens are stacked; deployments remain separate.</p>{column_svg}<div class="legend"><span class="legend-row"><span class="swatch" style="background:#73c7ff"></span>Input (uncached)</span><span class="legend-row"><span class="swatch" style="background:#57d68b"></span>Output</span><span class="legend-row"><span class="swatch" style="background:#ffc857"></span>Cached input</span></div>{column_table}</article></section>
<section class="panel analytics-section"><div class="panel-head"><div><h2>Model summary</h2><small>Totals reconcile to the portfolio summary.</small></div></div>{_model_table(report)}</section>
<section class="panel analytics-section"><div class="panel-head"><div><h2>All findings</h2><small>Material and lower-impact opportunities are retained for investigation.</small></div></div><div class="finding-list">{_finding_cards(report.findings)}</div></section>
<section class="panel analytics-section additional"><div class="panel-head"><div><h2>Additional opportunities</h2><small>{len(additional):,} lower-impact finding(s) retained outside Overview.</small></div></div>{_finding_cards(additional)}</section>
<section class="analytics-section"><div class="panel-head"><div><h2>Deployment details</h2><small>Expand a deployment for its findings and token breakdown.</small></div></div><div class="details-list">{_analytics_deployment_sections(report)}</div></section>
<footer>tokenlens-for-azure · created by Tzahi Ariel</footer></section>
<section class="tab-panel task" id="task-panel" role="tabpanel" aria-labelledby="task-tab">
<section class="panel"><div class="panel-head"><div><h2>Task economics</h2><small>No explicit task telemetry was supplied.</small></div></div>
<p class="empty">Request efficiency was measured successfully. Add explicit <code>task_id</code>, <code>task_type</code>, <code>execution_strategy</code>, and <code>strategy_version</code> events to unlock cost per solved task, cleanup, and strategy comparisons. Run <code>tokenlens-azure instrument --language python</code> for a safe starter.</p></section>
</section></div></main>
<script>
document.documentElement.classList.add("js");
(function() {{
  const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
  const panels = Array.from(document.querySelectorAll('[role="tabpanel"]'));
  function activate(name, moveFocus) {{
    const selected = name === "task" ? tabs[1] : name === "usage" ? tabs[2] : tabs[0];
    tabs.forEach(tab => {{ const active = tab === selected; tab.setAttribute("aria-selected", active); tab.tabIndex = active ? 0 : -1; }});
    panels.forEach(panel => panel.classList.toggle("active", panel.id === selected.getAttribute("aria-controls")));
    if (moveFocus) selected.focus();
    if (history.replaceState) history.replaceState(null, "", "#" + name);
  }}
  tabs.forEach((tab, index) => tab.addEventListener("click", () => activate(["overview", "task", "usage"][index], false)));
  tabs.forEach((tab, index) => tab.addEventListener("keydown", event => {{
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {{ event.preventDefault(); activate(["overview", "task", "usage"][(index + (event.key === "ArrowRight" ? 1 : 2)) % 3], true); }}
    if (event.key === "Home" || event.key === "End") {{ event.preventDefault(); activate(event.key === "Home" ? "overview" : "usage", true); }}
  }}));
  document.querySelectorAll("[data-tab-link]").forEach(link => link.addEventListener("click", event => {{ event.preventDefault(); activate(link.dataset.tabLink, false); }}));
  if (location.hash.toLowerCase() === "#usage") activate("usage", false);
  if (location.hash.toLowerCase() === "#task") activate("task", false);
  if (location.hash.toLowerCase() === "#overview") activate("overview", false);
}})();
</script></body></html>"""


def _model_table(report: AnalysisReport) -> str:
    rows = []
    for item in model_rollups(report):
        rows.append(
            f"<tr><th scope=\"row\">{_escape(item.model_name)}</th><td>{item.requests:,}</td><td>{item.input_tokens:,}</td>"
            f"<td>{item.output_tokens:,}</td><td>{item.cached_tokens:,}</td><td>{item.total_tokens:,}</td><td>{item.token_share_percent:.1f}%</td></tr>"
        )
    return f"""<table class="data-table"><caption>Model summary data</caption><thead><tr><th scope="col">Model</th><th scope="col">Requests</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col">Cached</th><th scope="col">Total</th><th scope="col">Share</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="7">No model usage observed.</td></tr>'}</tbody></table>"""


def write_output(content: str, output: str | None) -> None:
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    else:
        print(content, end="")


def task_economics_html(
    report: TaskEconomicsReport,
    *,
    overview_report: AnalysisReport | None = None,
) -> str:
    """Render the self-contained task economics experience from report data."""
    def money(value: float | None) -> str:
        return "Unresolved" if value is None else f"${value:.6f}"

    def pct(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.1f}%"

    cohorts = report.task_types
    strategies = report.execution_strategies
    max_cost = max((item.cost_per_solved_task_usd or item.cost_per_observed_task_usd or 0 for item in cohorts), default=1) or 1
    bars = []
    for index, item in enumerate(cohorts):
        value = item.cost_per_solved_task_usd or item.cost_per_observed_task_usd or 0
        width = max(2, value / max_cost * 100) if value else 2
        bars.append(
            f'<div class="bar-row"><span>{_escape(item.task_type)}</span><div class="bar-track"><i style="width:{width:.2f}%"></i></div>'
            f'<strong>{_escape(money(item.cost_per_solved_task_usd or item.cost_per_observed_task_usd))}</strong></div>'
        )
    composition_rows = "".join(
        f"<tr><th scope=\"row\">{_escape(item.task_type)}</th><td>{item.fresh_input_tokens:,}</td><td>{item.cached_input_tokens:,}</td>"
        f"<td>{item.cache_write_tokens:,}</td><td>{item.output_tokens:,}</td><td>{_escape(money(item.observed_tool_cost_usd))}</td>"
        f"<td>{_escape(money(item.observed_cleanup_spend_usd))}</td></tr>"
        for item in cohorts
    )
    strategy_points = []
    for index, item in enumerate(strategies):
        x = 48 + (item.cost_per_solved_task_usd or 0) / max(
            1, max((s.cost_per_solved_task_usd or 0 for s in strategies), default=1)
        ) * 560
        y = 230 - (item.eventual_success_rate or 0) / 100 * 180
        strategy_points.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{max(5, min(18, (item.closed_tasks or 1) ** .5)):.1f}" '
            f'tabindex="0" aria-label="{_escape(item.execution_strategy)} version {_escape(item.strategy_version)}, '
            f'{pct(item.eventual_success_rate)} success, {money(item.cost_per_solved_task_usd)} cost per solved task">'
            f'<title>{_escape(item.execution_strategy)} v{_escape(item.strategy_version)}</title></circle>'
        )
    cohort_rows = "".join(
        f"<tr><th scope=\"row\">{_escape(item.task_type)}</th><td>{item.maturity_label}</td><td>{item.attempted_tasks:,}</td>"
        f"<td>{item.closed_tasks:,}</td><td>{item.solved_tasks:,}</td><td>{pct(item.success_rate)}</td>"
        f"<td>{_escape(money(item.cost_per_observed_task_usd))}</td><td>{_escape(money(item.cost_per_solved_task_usd))}</td>"
        f"<td>{_escape(money(item.observed_cleanup_spend_usd))}</td><td>{item.pricing.coverage_percent:.1f}%</td></tr>"
        for item in cohorts
    )
    strategy_rows = "".join(
        f"<tr><th scope=\"row\">{_escape(item.task_type)}</th><td>{_escape(item.execution_strategy)}</td><td>{_escape(item.strategy_version)}</td>"
        f"<td>{'Provisional' if item.provisional else 'Ranked'}</td><td>{item.closed_tasks:,}</td><td>{pct(item.eventual_success_rate)}</td><td>{pct(item.attempt_success_rate)}</td>"
        f"<td>{_escape(money(item.cost_per_task_usd))}</td><td>{_escape(money(item.cost_per_solved_task_usd))}</td>"
        f"<td>{_escape(money(item.p50_cost_usd))} / {_escape(money(item.p90_cost_usd))}</td><td>{_escape(', '.join(item.model_composition) or 'n/a')}</td></tr>"
        for item in strategies
    )
    scenario_rows = "".join(
        f"<li><strong>{_escape(item.label)}</strong> · "
        f"{_escape(str(item.min_value) + '–' + str(item.max_value) + ' ' + item.unit) if item.min_value is not None else 'Simulation required'}"
        f"<small>{_escape(item.note)}</small></li>"
        for item in report.scenarios
    ) or "<li>No independent scenarios available.</li>"
    if overview_report is not None:
        overview_summary = overview_report.summary
        overview_content = f"""<section class="metrics"><div class="kpi"><span>Requests</span><b>{overview_summary.requests_analyzed:,}</b><p>{overview_summary.total_tokens:,} total tokens</p></div>
<div class="kpi"><span>Deployments</span><b>{len(overview_report.deployments):,}</b><p>Consumption routes</p></div>
<div class="kpi"><span>Optimisation scenarios</span><b>{len(overview_report.report_metadata.get("scenarios", [])):,}</b><p>Independent ranges · not additive</p></div>
<div class="kpi"><span>Findings</span><b>{len(overview_report.findings):,}</b><p>Usage diagnostics</p></div></section>
<section class="grid"><article class="panel overview-list"><h2>Portfolio usage</h2><p>Token share by deployment</p>{_deployment_rows(overview_report)}</article>
<article class="panel overview-list"><h2>Azure actions</h2><p>Prioritized for a first decision</p><ol>{_overview_actions(top_recommendations(overview_findings(overview_report)))}</ol></article></section>
<section class="panel overview-list"><h2>Material findings</h2><p>Independent scenarios are retained; no aggregate range is summed.</p>{_finding_cards(overview_findings(overview_report), compact=True)}</section>"""
    else:
        overview_content = '<div class="panel fallback"><h2>Consumption Overview</h2><p>Legacy request/deployment overview remains available when request telemetry is supplied.</p></div>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TokenLens for Azure — Task economics</title>
<style>
:root{{--bg:#11213b;--surface:#172a49;--surface2:#1d3559;--border:#35527a;--text:#f3f7ff;--muted:#b5c5dc;--accent:#73c7ff;--green:#57d68b;--amber:#ffc857}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",Arial,sans-serif}}main{{max-width:1320px;margin:auto;padding:22px}}header{{display:flex;justify-content:space-between;gap:18px;border-bottom:1px solid var(--border);padding-bottom:14px;margin-bottom:14px}}h1{{font-size:22px;margin:0}}h2{{font-size:17px;margin:0}}h3{{font-size:13px;margin:0}}p{{margin:0;color:var(--muted)}}.meta{{color:var(--muted);font-size:11px;text-align:right}}.tabs{{display:flex;gap:5px;border-bottom:1px solid var(--border);margin-bottom:14px}}button{{font:700 12px inherit;color:var(--muted);background:transparent;border:1px solid transparent;padding:9px 14px;border-radius:8px 8px 0 0;cursor:pointer}}button[aria-selected=true]{{color:var(--text);background:var(--surface);border-color:var(--border)}}button:focus-visible,svg circle:focus-visible{{outline:3px solid var(--amber);outline-offset:3px}}.panel,.kpi,.chart{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px}}.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:12px}}.kpi b{{display:block;font-size:22px;color:var(--accent);margin-top:4px}}.kpi span,.label{{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}.chart-head{{display:flex;justify-content:space-between;gap:8px;margin-bottom:10px}}.bar-row{{display:grid;grid-template-columns:150px 1fr 100px;align-items:center;gap:8px;margin:9px 0;font-size:11px}}.bar-track{{height:12px;background:var(--surface2);border-radius:8px;overflow:hidden}}.bar-track i{{display:block;height:100%;background:var(--accent);border-radius:8px}}.overview-list{{padding:14px}}.overview-list h2{{margin-bottom:3px}}.overview-list > p{{margin-bottom:10px}}.overview-list .portfolio-row{{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);font-size:11px}}.overview-list .portfolio-row small{{display:block;color:var(--muted)}}.overview-list .portfolio-bar{{height:8px;background:var(--surface2);border-radius:8px;overflow:hidden}}.overview-list .portfolio-bar span{{display:block;height:100%;background:var(--accent)}}.overview-list .portfolio-value{{text-align:right}}.overview-list ol{{margin:0;padding-left:20px}}.overview-list li{{padding:5px 0;color:var(--muted)}}svg{{width:100%;height:auto;background:var(--surface2);border-radius:8px}}.axis{{stroke:var(--border);stroke-width:1}}svg text{{fill:var(--muted);font-size:10px}}svg circle{{fill:var(--green);stroke:var(--text);stroke-width:1}}table{{width:100%;border-collapse:collapse;font-size:11px;display:block;overflow-x:auto}}caption{{text-align:left;color:var(--muted);padding:0 0 6px}}th,td{{padding:7px;border-bottom:1px solid var(--border);white-space:nowrap;text-align:right}}th:first-child,td:first-child{{text-align:left}}thead th{{color:var(--muted);font-size:9px;text-transform:uppercase}}.table-panel{{margin-bottom:12px}}.scenario-list{{margin:0;padding-left:20px}}.scenario-list li{{padding:6px 0;border-bottom:1px solid var(--border)}}.scenario-list small{{display:block;color:var(--muted)}}.fallback{{padding:24px;text-align:center}}@media(max-width:900px){{.metrics,.grid{{grid-template-columns:1fr 1fr}}.bar-row{{grid-template-columns:110px 1fr 90px}}}}@media(max-width:600px){{main{{padding:14px}}header{{display:block}}.meta{{text-align:left;margin-top:7px}}.metrics,.grid{{grid-template-columns:1fr}}}}@media print{{body{{background:#fff;color:#11213b}}.panel,.kpi,.chart{{background:#fff;border-color:#9aa8ba}}.tabs{{display:none}}}}
</style></head><body><main>
<header><div><h1>TokenLens for Azure · Task economics</h1><p>Explicit task telemetry · offline report · synthetic-safe output</p></div>
<div class="meta">Attempted tasks <strong>{report.total_attempted_tasks:,}</strong><br>Cost coverage <strong>{report.pricing.coverage_percent:.1f}%</strong></div></header>
<nav class="tabs" role="tablist" aria-label="Report views">
<button id="overview-tab" role="tab" aria-selected="false" aria-controls="overview-panel" tabindex="-1">Overview</button>
<button id="task-tab" role="tab" aria-selected="true" aria-controls="task-panel" tabindex="0">Task economics</button>
<button id="usage-tab" role="tab" aria-selected="false" aria-controls="usage-panel" tabindex="-1">Usage &amp; diagnostics</button></nav>
<section id="overview-panel" role="tabpanel" aria-labelledby="overview-tab" hidden>{overview_content}</section>
<section id="task-panel" role="tabpanel" aria-labelledby="task-tab">
<section class="metrics"><div class="kpi"><span>Attempted tasks</span><b>{report.total_attempted_tasks:,}</b><p>Open and closed activity</p></div><div class="kpi"><span>Solved tasks</span><b>{report.total_solved_tasks:,}</b><p>Explicit final outcomes</p></div><div class="kpi"><span>Success rate</span><b>{pct(sum(item.solved_tasks for item in cohorts) / sum(item.closed_tasks for item in cohorts) * 100 if sum(item.closed_tasks for item in cohorts) else None)}</b><p>Closed-task denominator</p></div><div class="kpi"><span>Cost coverage</span><b>{report.pricing.coverage_percent:.1f}%</b><p>Resolved billable events</p></div><div class="kpi"><span>Cleanup spend</span><b>{money(sum(item.observed_cleanup_spend_usd or 0 for item in cohorts) if any(item.observed_cleanup_spend_usd is not None for item in cohorts) else None)}</b><p>Observed only</p></div></section>
<section class="grid"><article class="chart"><div class="chart-head"><div><h2>Cost per solved task</h2><p>Unresolved monetary cohorts are omitted from dollar comparisons.</p></div></div>{''.join(bars) or '<p>No resolved monetary cohorts.</p>'}</article>
<article class="chart"><div class="chart-head"><div><h2>Success versus cost</h2><p>Each point is an execution strategy/version; bubble size is closed-task volume.</p></div></div><svg viewBox="0 0 660 270" role="img" aria-labelledby="scatter-title"><title id="scatter-title">Success versus cost per solved task</title><line class="axis" x1="48" y1="230" x2="620" y2="230"/><line class="axis" x1="48" y1="24" x2="48" y2="230"/>{''.join(strategy_points)}<text x="270" y="258">cost per solved task →</text><text x="5" y="30">success</text></svg></article></section>
<section class="panel table-panel"><h2>Strategy cost composition</h2><p>Fresh input, cached input, cache writes, output/reasoning, observed tools, and observed cleanup are kept separate; unresolved dollars remain visible as unresolved.</p><table><caption>Accessible composition data by task type</caption><thead><tr><th>Task type</th><th>Fresh input tokens</th><th>Cached input tokens</th><th>Cache writes</th><th>Output/reasoning tokens</th><th>Observed tools</th><th>Observed cleanup</th></tr></thead><tbody>{composition_rows or '<tr><td colspan="7">No composition data.</td></tr>'}</tbody></table></section>
<section class="panel table-panel"><h2>Task-type economics</h2><table><caption>Progressive maturity and cost metrics by explicit task type</caption><thead><tr><th>Task type</th><th>Maturity</th><th>Attempted</th><th>Closed</th><th>Solved</th><th>Success</th><th>Cost/task</th><th>Cost/solved</th><th>Cleanup</th><th>Coverage</th></tr></thead><tbody>{cohort_rows or '<tr><td colspan="10">No task cohorts.</td></tr>'}</tbody></table></section>
<section class="panel table-panel"><h2>Execution strategies</h2><table><caption>Equivalent task types only; thresholds are {report.thresholds.get("provisional_closed_tasks", 30)} provisional and {report.thresholds.get("ranked_closed_tasks", 100)} ranked closed tasks</caption><thead><tr><th>Task type</th><th>Strategy</th><th>Version</th><th>Maturity</th><th>Closed</th><th>Eventual success</th><th>Attempt success</th><th>Cost/task</th><th>Cost/solved</th><th>P50 / P90</th><th>Composition</th></tr></thead><tbody>{strategy_rows or '<tr><td colspan="11">No strategy comparisons.</td></tr>'}</tbody></table></section>
<section class="panel"><h2>Independent optimisation scenarios</h2><p>Scenarios are alternatives or partially overlapping levers. They are not summed.</p><ul class="scenario-list">{scenario_rows}</ul></section>
</section>
<section id="usage-panel" role="tabpanel" aria-labelledby="usage-tab" hidden><div class="panel fallback"><h2>Usage &amp; diagnostics</h2><p>Request/token diagnostics remain available offline; no raw event values are rendered here.</p>{_model_table(overview_report) if overview_report is not None else ""}</div></section>
<script>(function(){{const tabs=[...document.querySelectorAll('[role=tab]')],panels=[...document.querySelectorAll('[role=tabpanel]')];function activate(i,focus){{tabs.forEach((t,n)=>{{const on=n===i;t.setAttribute('aria-selected',on);t.tabIndex=on?0:-1;panels[n].hidden=!on}});if(focus)tabs[i].focus();if(history.replaceState)history.replaceState(null,'','#'+tabs[i].id.replace('-tab',''))}}tabs.forEach((tab,i)=>{{tab.addEventListener('click',()=>activate(i,false));tab.addEventListener('keydown',e=>{{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){{e.preventDefault();activate((i+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length,true)}}if(e.key==='Home'){{e.preventDefault();activate(0,true)}}if(e.key==='End'){{e.preventDefault();activate(tabs.length-1,true)}}}})}});const hash=location.hash.toLowerCase();if(hash==='#overview')activate(0,false);if(hash==='#task')activate(1,false);if(hash==='#usage')activate(2,false);}})();</script>
</main></body></html>"""
