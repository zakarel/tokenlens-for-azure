from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .models import AnalysisReport, Finding


def report_json(report: AnalysisReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"


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
                    "confidence": finding.confidence,
                    "azure_service": finding.azure_recommendation.service,
                    "azure_capability": finding.azure_recommendation.capability,
                },
            }
        )
    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "TokenLens for Azure", "version": report.version, "rules": report.rules}}, "results": results}],
    }
    return json.dumps(payload, indent=2) + "\n"


def _severity_class(finding: Finding) -> str:
    return finding.severity


def report_html(report: AnalysisReport) -> str:
    summary = report.summary
    finding_rows = []
    for finding in report.findings:
        estimate = finding.estimated_savings
        if estimate.unit == "calls":
            impact = f"{estimate.min_tokens:,} calls"
        elif estimate.min_tokens is None:
            impact = "No estimate"
        else:
            impact = f"{estimate.min_tokens:,}–{estimate.max_tokens:,} tokens"
        finding_rows.append(
            f"""<article class="finding {html.escape(_severity_class(finding))}">
              <div class="rule">{html.escape(finding.rule_id)}</div>
              <div><strong>{html.escape(finding.title)}</strong><small>{html.escape(finding.detail)}</small></div>
              <div class="impact">{html.escape(impact)}<small>{html.escape(finding.confidence)} confidence</small></div>
              <div class="action"><strong>{html.escape(finding.azure_recommendation.capability)}</strong><small>{html.escape(finding.azure_recommendation.action)}</small></div>
            </article>"""
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TokenLens for Azure — Report</title>
<style>
:root{{color-scheme:light;--bg:#f7f4ef;--surface:#fff;--text:#242424;--muted:#5c5c5c;--border:#dedede;--accent:#b11f4b;--success:#16a34a;--danger:#dc2626;--warning:#f59e0b;--link:#0078d4}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",Arial,sans-serif}}main{{max-width:1220px;margin:34px auto;padding:0 22px}}header{{display:flex;justify-content:space-between;align-items:end;border-bottom:1px solid var(--border);padding-bottom:20px;margin-bottom:18px}}h1{{font-size:32px;letter-spacing:-.04em;margin:4px 0}}h2{{font-size:18px;margin:0}}p,small{{color:var(--muted)}}.eyebrow{{color:var(--accent);font-weight:700;font-size:11px;letter-spacing:.1em;text-transform:uppercase}}.meta{{text-align:right;font-size:12px}}.meta strong{{display:block;color:var(--text)}}.metrics{{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:12px}}.card{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px}}.label{{color:var(--muted);font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em}}.value{{font-size:25px;font-weight:700;margin-top:8px;letter-spacing:-.04em}}.accent{{color:var(--accent)}}.sub{{color:var(--muted);font-size:11px;margin-top:7px}}.grid{{display:grid;grid-template-columns:1.8fr 1fr;gap:14px;align-items:start}}.panel{{background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden}}.panelhead{{padding:16px;border-bottom:1px solid var(--border)}}.finding{{display:grid;grid-template-columns:70px 1.2fr .8fr 1fr;gap:12px;padding:14px 16px;border-bottom:1px solid var(--border);align-items:center}}.finding:last-child{{border:0}}.finding.high .rule{{border-left-color:var(--danger)}}.finding.medium .rule{{border-left-color:var(--warning)}}.finding.low .rule{{border-left-color:var(--link)}}.finding.info .rule{{border-left-color:var(--border)}}.rule{{font-family:Consolas,monospace;font-weight:700;border-left:6px solid;padding-left:8px}}small{{display:block;font-size:11px;margin-top:3px}}.impact{{font-weight:700}}.action{{font-size:12px}}.action strong{{color:var(--link)}}.note{{margin-top:14px;padding:12px 14px;border-left:3px solid var(--border);background:var(--surface);color:var(--muted);font-size:11px}}@media(max-width:900px){{.metrics{{grid-template-columns:repeat(3,1fr)}}.grid{{grid-template-columns:1fr}}}}@media(max-width:620px){{header{{display:block}}.meta{{text-align:left;margin-top:14px}}.metrics{{grid-template-columns:repeat(2,1fr)}}.finding{{grid-template-columns:65px 1fr}}.impact,.action{{grid-column:2}}}}
</style></head><body><main>
<header><div><div class="eyebrow">Token efficiency assessment</div><h1>TokenLens for Azure</h1><p>{html.escape(report.source)} · advisory mode · offline analysis</p></div>
<div class="meta">Generated<strong>{html.escape(report.generated_at)}</strong>Version<strong>{html.escape(report.version)}</strong></div></header>
<section class="metrics"><div class="card"><div class="label">Requests</div><div class="value">{summary.requests_analyzed:,}</div><div class="sub">{summary.retries:,} retries</div></div>
<div class="card"><div class="label">Input tokens</div><div class="value">{summary.input_tokens:,}</div><div class="sub">{summary.cached_tokens:,} cached</div></div>
<div class="card"><div class="label">Output tokens</div><div class="value">{summary.output_tokens:,}</div><div class="sub">Observed responses</div></div>
<div class="card"><div class="label">Addressable range</div><div class="value accent">{summary.addressable_min_percent:.1f}–{summary.addressable_max_percent:.1f}%</div><div class="sub">{summary.addressable_min_tokens:,}–{summary.addressable_max_tokens:,} tokens</div></div>
<div class="card"><div class="label">Findings</div><div class="value">{summary.findings}</div><div class="sub">{summary.not_evaluated} not evaluated</div></div></section>
<div class="grid"><section class="panel"><div class="panelhead"><h2>Findings</h2><small>Ordered by severity; estimates are ranges, not guarantees.</small></div>{''.join(finding_rows)}</section>
<aside class="panel"><div class="panelhead"><h2>Interpretation</h2><small>Azure-first remediation guidance</small></div><div class="card" style="border:0;border-radius:0"><div class="label">Measured impact</div><div class="value accent">{summary.addressable_min_percent:.1f}–{summary.addressable_max_percent:.1f}%</div><p class="sub">Estimated addressable input-token volume. Findings can overlap and should be validated before production changes.</p><div class="label" style="margin-top:20px">Data quality</div><div class="value">{summary.findings - summary.not_evaluated}/{len(report.rules)}</div><p class="sub">Rules produced an evaluated result. Missing telemetry is shown explicitly.</p></div></aside></div>
<div class="note">Privacy: analysis is local and model-free. Raw prompt content is excluded from this report by default. Validate quality, safety, tenancy, and cache freshness before acting.</div>
</main></body></html>"""


def write_output(content: str, output: str | None) -> None:
    if output:
        Path(output).write_text(content, encoding="utf-8")
    else:
        print(content, end="")

