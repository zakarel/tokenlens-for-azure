from __future__ import annotations

import base64
import html
import json
from importlib.resources import files
from pathlib import Path

from .models import AnalysisReport, DeploymentAnalysis, Finding


def _without_internal(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_internal(item) for key, item in value.items() if key != "confidence"}
    if isinstance(value, list):
        return [_without_internal(item) for item in value]
    return value


def report_json(report: AnalysisReport) -> str:
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
    driver_rules = [_without_internal(rule) for rule in report.rules]
    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "TokenLens for Azure", "version": report.version, "rules": driver_rules}},
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
        return f"{estimate.min_tokens or 0:,} calls"
    if estimate.min_tokens is None:
        return "Opportunity"
    if estimate.min_tokens == estimate.max_tokens:
        return f"{estimate.min_tokens:,} tokens"
    return f"{estimate.min_tokens:,}–{estimate.max_tokens or estimate.min_tokens:,} tokens"


def _impact_percent(finding: Finding) -> str:
    if finding.impact_min_percent is None:
        return "Opportunity"
    if finding.impact_min_percent == finding.impact_max_percent:
        return f"{finding.impact_min_percent:.1f}% impact"
    return f"{finding.impact_min_percent:.1f}–{finding.impact_max_percent:.1f}% impact"


def _finding_rows(findings: list[Finding]) -> str:
    if not findings:
        return '<div class="empty">No addressable opportunities were detected in this trace set.</div>'
    rows = []
    for finding in findings:
        rows.append(
            f"""<article class="finding {_escape(finding.severity)}">
              <div class="rule">{_escape(finding.rule_id)}</div>
              <div><strong>{_escape(finding.title)}</strong><small>{_escape(finding.detail)}</small></div>
              <div class="impact"><strong>{_escape(_impact_percent(finding))}</strong><small>{_escape(_finding_impact(finding))}</small></div>
              <div class="action"><strong>{_escape(finding.azure_recommendation.capability)}</strong><small>{_escape(finding.azure_recommendation.action)}</small></div>
            </article>"""
        )
    return "".join(rows)


def _deployment_row(deployment: DeploymentAnalysis, total_tokens: int) -> str:
    summary = deployment.summary
    deployment_name = _display_name(summary.deployment_name, unknown="Unknown deployment")
    model_name = _display_name(summary.model_name, unknown="Unknown model type")
    width = min(100, max(2, round((summary.total_tokens / max(1, total_tokens)) * 100)))
    return f"""<div class="deployment-row">
      <div class="deployment-name"><strong>{_escape(deployment_name)}</strong>
      <small>Model type: {_escape(model_name)} · {_escape(summary.provider)}</small></div>
      <div class="bar-wrap" aria-label="{_escape(deployment_name)} uses {summary.token_share_percent:.1f}% of tokens"><span style="width:{width}%"></span></div>
      <div class="deployment-stat">{summary.total_tokens:,}<small>{summary.token_share_percent:.1f}% of tokens</small></div>
      <div class="deployment-stat">{summary.requests_analyzed:,}<small>{summary.request_share_percent:.1f}% of requests</small></div>
    </div>"""


def _deployment_sections(report: AnalysisReport) -> str:
    sections = []
    for deployment in report.deployments:
        summary = deployment.summary
        deployment_name = _display_name(summary.deployment_name, unknown="Unknown deployment")
        model_name = _display_name(summary.model_name, unknown="Unknown model type")
        sections.append(
            f"""<section class="panel deployment-section">
              <div class="panelhead"><h2>{_escape(deployment_name)}</h2>
              <small>Model type: {_escape(model_name)} · {_escape(summary.provider)} · {summary.requests_analyzed:,} requests</small></div>
              <div class="deployment-metrics">
                <div><span>Tokens</span><strong>{summary.total_tokens:,}</strong></div>
                <div><span>Input / output</span><strong>{summary.input_tokens:,} / {summary.output_tokens:,}</strong></div>
                <div><span>Addressable</span><strong>{summary.addressable_min_percent:.1f}–{summary.addressable_max_percent:.1f}%</strong></div>
                <div><span>Retries</span><strong>{summary.retries:,}</strong></div>
              </div>
              <div class="section-findings">{_finding_rows(deployment.findings)}</div>
            </section>"""
        )
    return "".join(sections) or '<section class="panel"><div class="empty">No deployments observed.</div></section>'


def report_html(report: AnalysisReport) -> str:
    summary = report.summary
    deployment_rows = "".join(_deployment_row(d, summary.total_tokens) for d in report.deployments)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TokenLens for Azure — Report</title>
<style>
:root{{--bg:#11213b;--bg-elevated:#172a49;--surface:#1b3153;--surface-soft:#223b61;--border:#35527a;--text:#f3f7ff;--muted:#b5c5dc;--soft:#d2deee;--accent:#73c7ff;--success:#57d68b;--danger:#ff7b8e;--warning:#ffc857;--link:#8ad2ff;--shadow:0 18px 48px rgba(0,0,0,.32)}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",Aptos,Calibri,Arial,sans-serif}}main{{max-width:1280px;margin:0 auto;padding:30px 28px 24px}}header{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:24px;margin-bottom:20px}}.logo{{width:min(430px,62vw);height:auto;display:block}}.eyebrow{{color:var(--accent);font-weight:700;font-size:11px;letter-spacing:.12em;text-transform:uppercase}}.meta{{text-align:right;color:var(--muted);font-size:12px}}.meta strong{{display:block;color:var(--text);font-size:13px;margin-bottom:4px}}h1,h2{{margin:0;letter-spacing:-.03em}}h2{{font-size:19px}}p,small{{color:var(--muted)}}small{{display:block;font-size:11px;margin-top:3px}}.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px}}.card,.panel{{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow)}}.card{{padding:16px}}.label{{color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}}.value{{font-size:25px;font-weight:700;margin-top:8px;letter-spacing:-.04em}}.accent{{color:var(--accent)}}.sub{{font-size:11px;margin-top:7px}}.grid{{display:grid;grid-template-columns:1.7fr 1fr;gap:16px;align-items:start}}.panel{{overflow:hidden}}.panelhead{{padding:17px 18px;border-bottom:1px solid var(--border)}}.finding{{display:grid;grid-template-columns:72px 1.2fr .8fr 1fr;gap:14px;padding:15px 18px;border-bottom:1px solid var(--border);align-items:center}}.finding:last-child{{border-bottom:0}}.finding.high .rule{{border-left-color:var(--danger)}}.finding.medium .rule{{border-left-color:var(--warning)}}.finding.low .rule{{border-left-color:var(--link)}}.finding.info .rule{{border-left-color:var(--border)}}.rule{{font-family:Consolas,monospace;font-weight:700;border-left:6px solid;padding-left:8px;color:var(--soft)}}.impact strong{{color:var(--accent)}}.action strong{{color:var(--link)}}.side{{padding:18px}}.side .value{{font-size:28px}}.side p{{font-size:12px}}.deployment-panel{{margin-top:16px}}.deployment-row{{display:grid;grid-template-columns:1.4fr 2fr 110px 110px;gap:14px;align-items:center;padding:14px 18px;border-bottom:1px solid var(--border)}}.deployment-row:last-child{{border:0}}.deployment-name strong{{font-size:14px}}.bar-wrap{{background:var(--surface-soft);height:12px;border-radius:99px;overflow:hidden}}.bar-wrap span{{background:var(--accent);height:100%;display:block;border-radius:99px}}.deployment-stat{{text-align:right;font-weight:700}}.deployment-section{{margin-top:16px}}.deployment-metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border)}}.deployment-metrics div{{background:var(--surface);padding:13px 16px}}.deployment-metrics span{{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;font-weight:700}}.deployment-metrics strong{{display:block;margin-top:5px}}.section-findings .finding{{background:var(--surface)}}.empty{{padding:20px;color:var(--muted)}}footer{{margin-top:22px;text-align:center;color:var(--muted);font-size:10px;letter-spacing:.04em}}@media(max-width:960px){{.metrics{{grid-template-columns:repeat(3,1fr)}}.grid{{grid-template-columns:1fr}}.deployment-row{{grid-template-columns:1.4fr 1.5fr 90px 90px}}}}@media(max-width:650px){{main{{padding:20px 14px}}header{{display:block}}.meta{{text-align:left;margin-top:18px}}.logo{{width:min(100%,430px)}}.metrics{{grid-template-columns:repeat(2,1fr)}}.finding{{grid-template-columns:62px 1fr}}.impact,.action{{grid-column:2}}.deployment-row{{grid-template-columns:1fr 80px}}.bar-wrap{{grid-column:1 / -1;grid-row:2}}.deployment-stat{{text-align:left}}.deployment-metrics{{grid-template-columns:repeat(2,1fr)}}}}
@media print{{body{{background:#fff;color:#11213b}}.card,.panel{{box-shadow:none;break-inside:avoid}}}}
</style></head><body><main>
<header><div><img class="logo" src="data:image/png;base64,{_logo_data_uri().split(',',1)[1]}" alt="TokenLens for Azure logo"></div>
<div class="meta">Generated<strong>{_escape(report.generated_at)}</strong>Source<strong>{_escape(report.source)}</strong>Deployments<strong>{len(report.deployments):,}</strong></div></header>
<section class="metrics"><div class="card"><div class="label">Requests</div><div class="value">{summary.requests_analyzed:,}</div><div class="sub">{summary.retries:,} retries</div></div>
<div class="card"><div class="label">Total tokens</div><div class="value">{summary.total_tokens:,}</div><div class="sub">{summary.input_tokens:,} input · {summary.output_tokens:,} output</div></div>
<div class="card"><div class="label">Cached input</div><div class="value">{summary.cached_tokens:,}</div><div class="sub">Observed cache reads</div></div>
<div class="card"><div class="label">Addressable range</div><div class="value accent">{summary.addressable_min_percent:.1f}–{summary.addressable_max_percent:.1f}%</div><div class="sub">{summary.addressable_min_tokens:,}–{summary.addressable_max_tokens:,} tokens</div></div>
<div class="card"><div class="label">Findings</div><div class="value">{summary.findings}</div><div class="sub">Actionable opportunities</div></div></section>
<div class="grid"><section class="panel"><div class="panelhead"><h2>All deployments</h2><small>Portfolio summary · every observed deployment is included</small></div>{deployment_rows}</section>
<aside class="panel side"><div class="panelhead"><h2>Impact</h2><small>Azure-first remediation guidance</small></div><div class="value accent">{summary.addressable_min_percent:.1f}–{summary.addressable_max_percent:.1f}%</div><p>Estimated addressable input-token volume. Findings can overlap; validate quality, safety, tenancy, and cache freshness before acting.</p></aside></div>
<section class="panel deployment-panel"><div class="panelhead"><h2>Portfolio findings</h2><small>Results from the complete trace set, ordered by severity</small></div>{_finding_rows(report.findings)}</section>
{_deployment_sections(report)}
<footer>tokenlens-for-azure · created by Tzahi Ariel</footer>
</main></body></html>"""


def write_output(content: str, output: str | None) -> None:
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    else:
        print(content, end="")
