from __future__ import annotations

import base64
import html
import json
import math
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from .models import AnalysisReport, AvailabilityState, DeploymentAnalysis, Finding, RuleEvaluation
from .economics import TaskEconomicsReport
from .ptu import ELIGIBILITY_STATUS_LABELS, PtuCostCurve, PtuDeploymentAssessment, PtuPortfolioAssessment, PtuThroughputSeries
from .ptu_report import (
    PTU_DASHBOARD_CSS,
    PTU_DASHBOARD_JS,
    deployment_slugs,
    money,
    render_deployment,
    selector,
)
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


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _human_timestamp(value: str) -> str:
    """Render a generated timestamp for people; ISO stays in JSON and tooltips."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    parsed = parsed.astimezone(UTC)
    return f"{parsed.day} {_MONTHS[parsed.month - 1]} {parsed.year} · {parsed:%H:%M} UTC"


def _source_summary(report: AnalysisReport) -> str:
    """Describe the input without ever rendering a local path.

    Source paths are user-private: they can contain account names, project
    names, and directory structure. The report states how much was read and
    over what window instead.
    """
    metadata = report.report_metadata or {}
    files = metadata.get("source_files")
    if not isinstance(files, int) or files <= 0:
        files = len([part for part in str(report.source).split(",") if part.strip()]) or 1
    aggregate = report.summary.aggregate
    if aggregate is not None and aggregate.window_start and aggregate.window_end:
        days = max(1, round((aggregate.window_end - aggregate.window_start).total_seconds() / 86_400))
        window = f"{days}-day window"
    else:
        window = "observed window"
    unit = "file" if files == 1 else "files"
    kind = "metric" if report.summary.analysis_unit == "metric_buckets" else "telemetry"
    return f"{files:,} local {kind} {unit} · {window} · offline analysis"


_CLASSIFICATION_LABELS = {
    "synthetic": "Offline synthetic example",
    "local_real": "Local offline analysis",
    "unknown": "Local offline analysis",
}


def _classification_label(report: AnalysisReport) -> str:
    """Only demo generation may claim synthetic data; it is never inferred."""
    return _CLASSIFICATION_LABELS.get(report.data_classification, "Local offline analysis")


def _requests_value(summary) -> str:
    if not summary.requests_available or summary.requests_observed is None:
        return "Unavailable"
    return f"{summary.requests_observed:,}"


def _requests_sub(summary) -> str:
    aggregate = summary.aggregate
    if aggregate is not None:
        detail = f"{aggregate.active_buckets:,} active of {aggregate.elapsed_buckets:,} elapsed {aggregate.bucket_minutes}-minute buckets"
        if not summary.requests_available:
            return f"Request metric unavailable · {detail}"
        return detail
    return f"{summary.retries:,} retries observed" if summary.retries_available else "Retries unavailable"


def _tokens_sub(summary) -> str:
    cached = (
        f"{summary.cached_tokens:,} cached"
        if summary.cached_tokens_available
        else "cached metric unavailable"
    )
    fresh = f"{summary.input_tokens:,} input" if summary.input_tokens_available else "input metric unavailable"
    output = f"{summary.output_tokens:,} output" if summary.output_tokens_available else "output metric unavailable"
    return f"{fresh} · {cached} · {output}"


def _total_tokens_value(summary) -> str:
    """A total is only a total when every component was actually reported."""
    if summary.input_tokens_available and summary.output_tokens_available:
        return f"{summary.total_tokens:,}"
    return f"{summary.total_tokens:,} partial"


_PRICING_STATUS_COPY = {
    "priced": ("Priced", "Exact rate matched for every observed token"),
    "partial": ("Partially priced", "Some tokens have no exact rate"),
    "catalog_missing": ("Model identified, exact rate missing", "Run pricing-audit or add an exact override"),
    "identity_unresolved": ("Pricing not attempted", "Model identity unresolved during collection"),
    "currency_mismatch": ("Currency conversion required", "Configure a reporting currency; no conversion is performed"),
}


def _pricing_status_copy(summary) -> tuple[str, str]:
    return _PRICING_STATUS_COPY.get(summary.pricing_status, ("Unresolved", "No exact model/mode price"))


def _data_quality_banner(report: AnalysisReport) -> str:
    """State what is trustworthy before any number is read."""
    issues = list(report.data_quality)
    if not issues:
        return ""
    blockers = [item for item in issues if item.severity == "blocker"]
    warnings = [item for item in issues if item.severity != "blocker"]
    rows = "".join(
        f'<li class="dq-{_escape(item.severity)}"><strong>{_escape(item.title)}</strong>'
        f"<span>{_escape(item.detail)}</span></li>"
        for item in blockers + warnings
    )
    headline = (
        f"{len(blockers)} blocking data-quality issue(s)"
        if blockers
        else f"{len(warnings)} data-quality limitation(s)"
    )
    trustworthy = (
        "Token volumes and window coverage remain usable."
        if blockers
        else "All other values reconcile with the analyzed telemetry."
    )
    return f"""<section class="data-quality{' blocking' if blockers else ''}" role="note" aria-label="Data quality">
      <div class="dq-head"><strong>{_escape(headline)}</strong><span>{_escape(trustworthy)}</span></div>
      <ul>{rows}</ul></section>"""


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


def _finding_card(finding: Finding, *, compact: bool = False, denominator: str = "") -> str:
    recommendation = finding.azure_recommendation
    # Impact share and evidence strength are different statements and are
    # rendered as different values.
    share = _impact_percent(finding)
    basis = f" of {denominator}" if denominator and finding.impact_max_percent is not None else ""
    return f"""<article class="finding-card {_escape(finding.severity)}">
      <div class="finding-rule">{_escape(finding.rule_id)}</div>
      <div class="finding-copy"><strong>{_escape(finding.title)}</strong>
        <span>{_escape(finding.detail if not compact else recommendation.action)}</span></div>
      <div class="finding-impact"><strong>{_escape(share)}{_escape(basis)}</strong>
        <span>{_escape(_finding_impact(finding))} · {_escape(impact_category(finding))} · evidence {_escape(finding.confidence)}</span></div>
      <div class="finding-action"><strong>{_escape(recommendation.capability)}</strong>
        <span>{_escape(recommendation.action if not compact else recommendation.service)}</span></div>
    </article>"""


def _finding_cards(
    findings: list[Finding],
    *,
    compact: bool = False,
    empty: str | None = None,
    denominator: str = "",
) -> str:
    if not findings:
        message = empty or "No addressable opportunity was detected by the diagnostics that ran."
        return f'<p class="empty">{_escape(message)}</p>'
    return "".join(_finding_card(item, compact=compact, denominator=denominator) for item in findings)


def _no_findings_message(report: AnalysisReport) -> str:
    """Distinguish "nothing to fix" from "nothing could be checked"."""
    if report.diagnostics.source == "aggregate":
        return (
            "No request-level diagnostic can run against aggregate metric buckets, so no finding was produced. "
            "This is a coverage gap, not a clean bill of health."
        )
    if report.diagnostics.not_evaluated and not report.diagnostics.no_issue:
        return "Every applicable diagnostic lacked the evidence it needs; see Not evaluated for the exact gaps."
    return "Every applicable diagnostic ran and found no addressable opportunity."


def _deployment_rows(report: AnalysisReport) -> str:
    rollups = deployment_rollups(report)
    if not rollups:
        return '<p class="empty">No deployments observed.</p>'
    if len(rollups) == 1:
        # A single deployment always holds 100% of its own portfolio. An
        # absolute usage summary carries information; a full-width bar does not.
        item = rollups[0]
        requests = f"{item.requests:,}" if item.requests is not None else "Unavailable"
        window = _window_text(item)
        cached = f"{item.cached_tokens:,}" if item.cached_tokens_available else "Metric unavailable"
        return f"""<div class="single-usage">
          <div><strong>{_escape(_display_name(item.deployment_name, unknown="Unknown deployment"))}</strong>
            <small>{_escape(_model_label(item))} · {_escape(item.deployment_mode.title())}</small></div>
          <dl class="single-usage-grid">
            <div><dt>Total tokens</dt><dd>{item.total_tokens:,}</dd></div>
            <div><dt>Input</dt><dd>{item.input_tokens:,}</dd></div>
            <div><dt>Cached input</dt><dd>{_escape(cached)}</dd></div>
            <div><dt>Output</dt><dd>{item.output_tokens:,}</dd></div>
            <div><dt>Requests</dt><dd>{_escape(requests)}</dd></div>
            <div><dt>Observed window</dt><dd>{_escape(window)}</dd></div>
          </dl></div>"""
    rows = []
    for item in rollups:
        name = _display_name(item.deployment_name, unknown="Unknown deployment")
        model = _display_name(item.model_name, unknown="Unknown model")
        volume = f"{item.requests:,} requests" if item.requests is not None else f"{item.active_buckets:,} active buckets"
        rows.append(
            f"""<div class="portfolio-row">
              <div><strong>{_escape(name)}</strong><small>{_escape(model)}</small></div>
              <div class="portfolio-bar" role="img" aria-label="{_escape(chart_label(name, item.total_tokens, item.token_share_percent))}"><span style="width:{max(1, item.token_share_percent)}%"></span></div>
              <div class="portfolio-value"><strong>{item.total_tokens:,}</strong><small>{item.token_share_percent:.1f}% · {_escape(volume)}</small></div>
            </div>"""
        )
    return "".join(rows)


def _model_label(item) -> str:
    model = _display_name(item.model_name, unknown="Unknown model")
    version = getattr(item, "model_version", None)
    return f"{model} · version {version}" if version else model


def _window_text(item) -> str:
    start, end = getattr(item, "window_start", None), getattr(item, "window_end", None)
    if not start or not end:
        return "Not reported by source"
    try:
        first = datetime.fromisoformat(start)
        last = datetime.fromisoformat(end)
    except ValueError:
        return "Not reported by source"
    days = max(1, round((last - first).total_seconds() / 86_400))
    active = getattr(item, "active_days", 0)
    return f"{days} day(s) · {active} active day(s)"


def _overview_actions(findings: list[Finding], report: AnalysisReport) -> str:
    """Only actions backed by an evaluated finding are promoted.

    Missing evidence is reported as missing evidence. It is never presented as
    an optimisation recommendation.
    """
    if findings:
        return "".join(
            f'<li><strong>{_escape(item.azure_recommendation.capability)}</strong><span>{_escape(item.azure_recommendation.action)}</span></li>'
            for item in findings
        )
    if report.diagnostics.source == "aggregate" or report.diagnostics.not_evaluated:
        return (
            '<li class="not-evaluated"><strong>Request-level optimisation not evaluated</strong>'
            "<span>Instrument the SDK or import OpenTelemetry to analyse prompt, retry, and cache efficiency. "
            "No request-level diagnostic ran against this telemetry.</span></li>"
        )
    return '<li class="not-evaluated"><strong>No prioritized Azure actions</strong><span>Every applicable diagnostic ran and found no addressable issue.</span></li>'


def _diagnostic_sections(report: AnalysisReport) -> str:
    """Mutually exclusive diagnostic sections.

    A rule appears exactly once: as an evaluated finding, as evaluated with no
    issue, or in the not-evaluated coverage matrix. "Not evaluated" is never
    rendered as an opportunity and never counted as a finding.
    """
    evaluations = report.diagnostics.evaluations
    findings_by_rule = {finding.rule_id: finding for finding in report.findings}
    evaluated = [item for item in evaluations if item.status == "finding" and item.rule_id in findings_by_rule]
    no_issue = [item for item in evaluations if item.status == "no_issue"]
    not_evaluated = [item for item in evaluations if item.status == "not_evaluated"]
    material = {finding.rule_id for finding in overview_findings(report)}
    additional = [
        findings_by_rule[item.rule_id]
        for item in evaluated
        if item.rule_id not in material
    ]

    denominator = f"{report.summary.input_tokens:,} input tokens"
    finding_cards = _finding_cards(
        [findings_by_rule[item.rule_id] for item in evaluated],
        empty=_no_findings_message(report),
        denominator=denominator,
    )
    no_issue_rows = "".join(
        f'<li><strong>{_escape(item.rule_id)} {_escape(item.title)}</strong><span>{_escape(item.detail)}</span></li>'
        for item in no_issue
    ) or '<li class="empty">No rule completed without a finding.</li>'
    coverage_rows = "".join(
        f'<tr><th scope="row">{_escape(item.rule_id)} {_escape(item.title)}</th>'
        f'<td>{_escape(", ".join(item.applicable_sources) or "request")}</td>'
        f'<td>{_escape(", ".join(item.missing_fields) or "—")}</td>'
        f"<td>{_escape(item.detail)}</td></tr>"
        for item in not_evaluated
    )
    coverage = (
        f"""<section class="panel analytics-section not-evaluated-panel"><div class="panel-head"><div>
          <h2>Not evaluated</h2><small>{len(not_evaluated):,} rule(s) had no supporting evidence. These are coverage gaps, not findings, and are excluded from every finding count.</small></div></div>
          <div class="table-scroll"><table class="data-table"><caption>Diagnostic applicability and missing evidence</caption>
          <thead><tr><th scope="col">Rule</th><th scope="col">Applies to</th><th scope="col">Missing evidence</th><th scope="col">Why</th></tr></thead>
          <tbody>{coverage_rows}</tbody></table></div></section>"""
        if not_evaluated
        else ""
    )
    return f"""<section class="panel analytics-section"><div class="panel-head"><div><h2>Evaluated findings</h2>
      <small>{len(evaluated):,} rule(s) ran against supporting evidence and detected an addressable issue.</small></div></div>
      <div class="finding-list">{finding_cards}</div></section>
    <section class="panel analytics-section"><div class="panel-head"><div><h2>Evaluated — no issue detected</h2>
      <small>{len(no_issue):,} rule(s) ran and found nothing addressable.</small></div></div>
      <ul class="status-list">{no_issue_rows}</ul></section>
    {coverage}
    <section class="panel analytics-section additional"><div class="panel-head"><div><h2>Additional evaluated opportunities</h2>
      <small>{len(additional):,} evaluated finding(s) below the Overview materiality threshold.</small></div></div>
      {_finding_cards(additional, denominator=denominator) if additional else '<p class="empty">Every evaluated finding is already shown in Overview.</p>'}</section>"""


def _composition_card(report: AnalysisReport) -> str:
    """Single-model composition instead of a meaningless one-slice donut."""
    summary = report.summary
    models = model_rollups(report)
    item = models[0] if models else None
    cached_label = f"{summary.cached_tokens:,}" if summary.cached_tokens_available else "Metric unavailable"
    parts = [
        ("Input (uncached)", max(0, summary.input_tokens - (summary.cached_tokens if summary.cached_tokens_available else 0)), "#73c7ff"),
        ("Cached input", summary.cached_tokens if summary.cached_tokens_available else 0, "#ffc857"),
        ("Output", summary.output_tokens, "#57d68b"),
    ]
    total = max(1, sum(value for _, value, _ in parts))
    bars = "".join(
        f'<div class="bar-row"><span>{_escape(label)}</span>'
        f'<div class="bar-track"><i style="width:{value / total * 100:.1f}%;background:{color}"></i></div>'
        f"<strong>{value:,}</strong></div>"
        for label, value, color in parts
    )
    name = _escape(_model_label(item)) if item else "Unknown model"
    return f"""<article class="chart-card"><h2>Token composition</h2>
      <p>One model is in scope, so composition replaces a single-slice share chart.</p>
      <div class="composition" role="img" aria-label="Token composition for {name}: {summary.input_tokens:,} input, {_escape(cached_label)} cached, {summary.output_tokens:,} output">{bars}</div>
      <div class="table-scroll"><table class="data-table"><caption>Token composition for {name}</caption>
      <thead><tr><th scope="col">Component</th><th scope="col">Tokens</th></tr></thead>
      <tbody><tr><th scope="row">Input</th><td>{summary.input_tokens:,}</td></tr>
      <tr><th scope="row">Cached input</th><td>{_escape(cached_label)}</td></tr>
      <tr><th scope="row">Output</th><td>{summary.output_tokens:,}</td></tr>
      <tr><th scope="row">Total</th><td>{summary.total_tokens:,}</td></tr></tbody></table></div></article>"""


def _donut_chart(report: AnalysisReport) -> tuple[str, str]:
    models = model_rollups(report)
    total = max(1, report.summary.total_tokens)
    colors = ["#73c7ff", "#57d68b", "#ffc857", "#ff8fa3", "#b9a7ff", "#5eead4", "#f4a261"]
    radius, circumference = 76, 2 * math.pi * 76
    offset = 0.0
    segments = []
    for index, item in enumerate(models):
        fraction = item.total_tokens / total
        length = circumference * fraction
        label = chart_label(item.model_name, item.total_tokens, item.token_share_percent)
        color = colors[index % len(colors)]
        segments.append(
            f'<circle class="donut-segment" cx="100" cy="100" r="{radius}" style="fill:none;stroke:{color};stroke-width:28" '
            f'stroke-dasharray="{length:.4f} {circumference - length:.4f}" '
            f'stroke-dashoffset="{-offset:.4f}" tabindex="0" aria-label="{_escape(label)}"><title>{_escape(label)}</title></circle>'
        )
        offset += length
    svg = f"""<svg class="donut" viewBox="0 0 200 200" role="img" aria-labelledby="donut-title donut-desc">
      <title id="donut-title">Token share by model</title>
      <desc id="donut-desc">Each segment represents a model's share of {report.summary.total_tokens:,} portfolio tokens.</desc>
      <circle class="donut-track" cx="100" cy="100" r="{radius}" style="fill:none;stroke:#294365;stroke-width:28"></circle>
      <g transform="rotate(-90 100 100)">{''.join(segments)}</g>
      <text x="100" y="96" text-anchor="middle" class="donut-total">{report.summary.total_tokens:,}</text>
      <text x="100" y="113" text-anchor="middle" class="donut-label">total tokens</text>
    </svg>"""
    # The donut's own text alternative stays compact; the full model summary is
    # a separate panel, so the same table is never rendered twice.
    rows = "".join(
        f'<tr><th scope="row">{_escape(_model_label(item))}</th><td>{item.total_tokens:,}</td>'
        f"<td>{item.token_share_percent:.1f}%</td></tr>"
        for item in models
    )
    table = f"""<div class="table-scroll"><table class="data-table"><caption>Token share by model</caption>
      <thead><tr><th scope="col">Model</th><th scope="col">Tokens</th><th scope="col">Share</th></tr></thead>
      <tbody>{rows or '<tr><td colspan="3">No model usage observed.</td></tr>'}</tbody></table></div>"""
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
        # Cached input is a subset of input. When the metric is unavailable it is
        # omitted entirely rather than drawn as a zero-height segment.
        cached_value = item.cached_tokens if item.cached_tokens_available else 0
        parts = [
            ("input", max(0, item.input_tokens - cached_value)),
            ("output", item.output_tokens),
            ("cached", cached_value),
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
            f'<td>{item.output_tokens:,}</td><td>{_escape(f"{item.cached_tokens:,}" if item.cached_tokens_available else "Unavailable")}</td><td>{item.total_tokens:,}</td>'
            f'<td>{_escape(_money(item.estimated_cost_usd, currency=item.pricing_currency))}</td></tr>'
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
    table = f"""<div class="table-scroll"><table class="data-table"><caption>Accessible deployment token usage data</caption>
      <thead><tr><th scope="col">Deployment</th><th scope="col">Model</th><th scope="col">Input</th><th scope="col">Output</th><th scope="col">Cached</th><th scope="col">Total</th><th scope="col">Est. cost</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="7">No deployment usage observed.</td></tr>'}</tbody>
    </table></div>"""
    return svg, table


def _analytics_deployment_sections(report: AnalysisReport) -> str:
    sections = []
    for deployment in deployment_rollups(report):
        analysis = next(
            (
                item
                for item in report.deployments
                if item.summary.deployment_name == deployment.deployment_name
                and item.summary.canonical_model_key == deployment.canonical_model_key
                and item.summary.deployment_mode.casefold() == deployment.deployment_mode.casefold()
                and item.summary.resource_name == deployment.resource_name
                and item.summary.project_name == deployment.project_name
            ),
            None,
        )
        findings = analysis.findings if analysis else []
        aggregate = analysis.summary.aggregate if analysis else None
        requests = f"{deployment.requests:,}" if deployment.requests is not None else "Unavailable"
        cached = f"{deployment.cached_tokens:,}" if deployment.cached_tokens_available else "Metric unavailable"
        retries = f"{deployment.retries:,}" if deployment.retries_available else "Unavailable"
        status_codes = (
            " · ".join(f"HTTP {code}: {count:,}" for code, count in aggregate.status_codes.items())
            if aggregate and aggregate.status_codes
            else ("Outcome metric unavailable" if aggregate else "")
        )
        bucket_line = (
            f'<span>Buckets <strong>{aggregate.active_buckets:,} active / {aggregate.observed_buckets:,} observed / {aggregate.elapsed_buckets:,} elapsed</strong></span>'
            if aggregate
            else ""
        )
        outcome_line = f'<span>Outcomes <strong>{_escape(status_codes)}</strong></span>' if status_codes else ""
        not_evaluated = analysis.diagnostics.not_evaluated if analysis else 0
        coverage_line = (
            f'<p class="muted">{not_evaluated:,} diagnostic(s) not evaluated for this deployment; only evaluated findings are listed.</p>'
            if not_evaluated
            else ""
        )
        sections.append(
            f"""<details class="deployment-details"><summary><strong>{_escape(_display_name(deployment.deployment_name, unknown="Unknown deployment"))}</strong>
              <span>{_escape(_model_label(deployment))} · {_escape(requests)} requests · {deployment.total_tokens:,} tokens</span></summary>
              <div class="detail-body"><div class="mini-stats"><span>Input <strong>{deployment.input_tokens:,}</strong></span>
              <span>Output <strong>{deployment.output_tokens:,}</strong></span><span>Cached <strong>{_escape(cached)}</strong></span>
              <span>Retries <strong>{_escape(retries)}</strong></span>{bucket_line}{outcome_line}</div>{coverage_line}{_finding_cards(findings)}</div>
            </details>"""
        )
    return "".join(sections) or '<p class="empty">No deployments observed.</p>'


def _money(value: float | None, *, currency: str = "USD", precision: int = 4) -> str:
    return money(value, currency, precision=precision, unavailable="Pricing unavailable")


def _rate(value: float | None, *, currency: str = "USD") -> str:
    return "N/A" if value is None else _money(value, currency=currency, precision=3)


_BILLING_BASIS_LABELS = {
    "token_rate": "Azure token rate",
    "claude_ccu_equivalent": "CCU-equivalent estimate",
    "marketplace_partner_token_rate": "Marketplace partner rate",
    "observed_cost": "Observed cost",
    "mixed": "Mixed",
}

_UNRESOLVED_REASON_LABELS = {
    "no-exact-model-mode-price": "No exact model/mode price in any catalog",
    "cached-input-rate-unavailable": "Cached input observed but no cached rate is priced",
    "currency-conversion-required": "Requires currency conversion (not performed)",
}

_PRICING_SOURCE_LABELS = {
    "reference": "Public snapshot",
    "customer": "Customer override",
    "observed": "Observed cost",
    "mixed": "Mixed",
    "unresolved": "Unresolved",
}


def _billing_basis_label(value: str | None) -> str:
    if not value:
        return "Unresolved"
    return _BILLING_BASIS_LABELS.get(value, value.replace("_", " ").title())


def _unresolved_reason_label(value: str) -> str:
    return _UNRESOLVED_REASON_LABELS.get(value, value.replace("-", " ").capitalize())


def _pricing_source_label(value: str) -> str:
    return _PRICING_SOURCE_LABELS.get(value, value.replace("_", " ").title())


def _status_badge(item) -> str:
    if item.estimated_cost_usd is not None and item.unresolved_requests == 0:
        return f"{_escape(_pricing_source_label(item.pricing_source))}"
    if item.estimated_cost_usd is not None:
        return f"{_escape(_pricing_source_label(item.pricing_source))} · {item.unresolved_requests:,} unresolved"
    reasons = ", ".join(_unresolved_reason_label(r) for r in item.unresolved_reasons) or "no exact model/mode price"
    return f"Unresolved · {_escape(reasons)}"


def _availability_badge(state: AvailabilityState) -> str:
    """One shared renderer. Colour never carries meaning on its own."""
    reason = f'<span class="state-reason">{_escape(state.reason)}</span>' if state.reason else ""
    return (
        f'<span class="state state-{state.tone}">'
        f'<span class="state-symbol" aria-hidden="true">{_escape(state.symbol)}</span>'
        f'<span class="state-label">{_escape(state.label)}</span></span>{reason}'
    )


def _identity_missing(item) -> bool:
    return (item.model_name or "unknown").strip().casefold() in {"", "unknown", "none"}


def _model_pricing_state(item) -> AvailabilityState:
    """Separate a collection identity failure from a missing rate."""
    if _identity_missing(item):
        return AvailabilityState(
            status="missing_blocking",
            label="Model identity missing",
            reason="Recollect or enrich this slice before any rate can apply",
            action_id="identity",
        )
    if item.estimated_cost_usd is not None and not item.unresolved_requests:
        return AvailabilityState(status="available", label="Priced")
    if item.estimated_cost_usd is not None:
        return AvailabilityState(
            status="partial",
            label="Partly priced",
            reason=f"{item.unresolved_tokens:,} tokens excluded",
            action_id="pricing",
        )
    if item.pricing_status == "currency_mismatch":
        return AvailabilityState(
            status="missing_actionable",
            label="Currency mismatch",
            reason="TokenLens never converts currencies",
            action_id="currency",
        )
    return AvailabilityState(
        status="missing_actionable",
        label="Exact rate missing",
        reason=", ".join(_unresolved_reason_label(r) for r in item.unresolved_reasons) or None,
        action_id="pricing",
    )


def _cost_state(report: AnalysisReport) -> dict[str, object]:
    """Derive the single decision state for the whole Cost analysis tab."""
    summary = report.summary
    models = model_rollups(report)
    identity_missing = [item for item in models if _identity_missing(item)]
    needs_price = [
        item
        for item in models
        if not _identity_missing(item) and (item.estimated_cost_usd is None or item.unresolved_requests)
    ]
    identity_tokens = sum(item.total_tokens for item in identity_missing)
    coverage = summary.pricing_coverage_tokens_percent
    if summary.estimated_cost_usd is not None and not summary.unresolved_tokens:
        banner = AvailabilityState(status="available", label="Pricing complete")
    elif summary.estimated_cost_usd is not None:
        banner = AvailabilityState(
            status="partial",
            label=f"Partial estimate · {coverage:.0f}% of tokens priced",
            reason=(
                f"{summary.unresolved_tokens:,} tokens across {len(needs_price)} model(s) are not priced."
            ),
            action_id="pricing",
        )
    elif identity_missing and not needs_price:
        banner = AvailabilityState(
            status="missing_blocking",
            label="Model identity missing",
            reason=(
                f"{len(identity_missing)} telemetry slice(s) must be recollected or enriched before "
                "pricing."
            ),
            action_id="identity",
        )
    else:
        banner = AvailabilityState(
            status="missing_actionable",
            label="Pricing setup required",
            reason=(
                f"{summary.unresolved_tokens:,} tokens across {len(needs_price)} model(s) are not "
                "priced, so cost totals are withheld."
            ),
            action_id="pricing",
        )
    return {
        "banner": banner,
        "models": models,
        "identity_missing": identity_missing,
        "needs_price": needs_price,
        "identity_tokens": identity_tokens,
    }


#: One remediation panel per report. The command never repeats in a card.
_REMEDIATION = {
    "pricing": (
        "Resolve pricing",
        "Add an exact contracted rate, or keep usage analysis without cost.",
        "tokenlens-azure foundry pricing",
    ),
    "identity": (
        "Fix collection identity",
        "The model and version must resolve before any rate can apply.",
        "tokenlens-azure foundry collect --days 14",
    ),
    "currency": (
        "Choose one reporting currency",
        "TokenLens never converts currencies; align the catalog and the report.",
        "tokenlens-azure pricing verify",
    ),
}


def _remediation_panel(state: AvailabilityState, models) -> str:
    action = state.action_id
    if action is None:
        return ""
    title, detail, command = _REMEDIATION[action]
    affected = [
        item
        for item in models
        if (_model_pricing_state(item).action_id == action)
    ]
    rows = "".join(
        f"<li><strong>{_escape(_model_label(item))}</strong>"
        f"<span>{_escape(item.deployment_mode.title())} · {item.total_tokens:,} tokens · "
        f"{_escape(_model_pricing_state(item).label)}</span>"
        + (
            f"<span>Suggested override key: <code>{_escape(', '.join(item.suggested_override_keys) or item.canonical_model_key)}</code></span>"
            if action == "pricing" and not _identity_missing(item)
            else ""
        )
        + "</li>"
        for item in affected
    )
    return (
        f'<details class="remediation" id="resolve-pricing"><summary>{_escape(title)}</summary>'
        f'<div class="detail-body"><p class="muted">{_escape(detail)}</p>'
        f'<ul class="remediation-list">{rows or "<li>No model is affected.</li>"}</ul>'
        f'<p class="muted">Run:</p><pre><code>{_escape(command)}</code></pre></div></details>'
    )


def _cost_composition(report: AnalysisReport) -> str:
    """Three honest states: nothing priced, partly priced, fully priced."""
    summary = report.summary
    if summary.estimated_cost_usd is None:
        return (
            '<div class="cost-empty"><strong>No costs to chart until pricing is configured.</strong>'
            f'<span>{summary.unresolved_tokens:,} tokens remain unpriced.</span>'
            '<a href="#resolve-pricing" data-open-remediation="1">Resolve pricing</a></div>'
        )
    components = [
        ("Fresh input", summary.fresh_input_cost_usd, "var(--accent)", summary.input_tokens - summary.cached_tokens),
        ("Cached input", summary.cached_input_cost_usd, "var(--amber)", summary.cached_tokens),
        ("Output", summary.output_cost_usd, "var(--green)", summary.output_tokens),
    ]
    # A component with no tokens is omitted rather than drawn as a zero-width
    # bar that reads as a resolved $0 cost.
    priced = [
        (label, value, color)
        for label, value, color, tokens in components
        if value is not None and (value > 0 or tokens > 0)
    ]
    max_component = max((value for _, value, _ in priced), default=0) or 1
    rows = [
        f'<div class="cost-bar"><span>{_escape(label)}</span><div class="cost-track"><i style="width:{value / max_component * 100:.1f}%;background:{color}"></i></div><strong>{_escape(_money(value, currency=summary.pricing_currency))}</strong></div>'
        for label, value, color in priced
    ]
    if not rows:
        rows = [
            f'<div class="cost-bar"><span>Analyzed cost</span><div class="cost-track"><i style="width:100%;background:var(--accent)"></i></div>'
            f'<strong>{_escape(_money(summary.estimated_cost_usd, currency=summary.pricing_currency))}</strong></div>'
        ]
    if summary.unresolved_tokens:
        excluded_share = summary.unresolved_tokens / max(1, summary.total_tokens) * 100
        rows.append(
            '<div class="cost-bar partial"><span>Unpriced</span>'
            f'<div class="cost-track"><i class="excluded" style="width:{excluded_share:.1f}%"></i></div>'
            f'<strong>{summary.unresolved_tokens:,} tokens</strong></div>'
        )
        rows.append(
            '<p class="cost-note">'
            + _availability_badge(
                AvailabilityState(
                    status="partial",
                    label=f"Partial estimate · {summary.pricing_coverage_tokens_percent:.0f}% of tokens priced",
                )
            )
            + "</p>"
        )
    return "".join(rows)


def _pricing_provenance(report: AnalysisReport, models) -> str:
    """Two lines by default; everything technical is collapsed behind a disclosure."""
    summary = report.summary
    pricing = report.report_metadata.get("pricing", {})
    matched = summary.estimated_cost_usd is not None
    catalogs = pricing.get("catalogs_consulted") or []
    if not matched:
        headline = f"No catalog entry matched · {len(catalogs)} catalog{'' if len(catalogs) == 1 else 's'} checked"
    else:
        retrieved = pricing.get("retrieved_at")
        headline = f"{_escape(pricing.get('catalog_selected') or 'Selected catalog')}" + (
            f" · Retrieved {_escape(_human_timestamp(str(retrieved)))}" if retrieved else ""
        )
    details: list[str] = [
        f"<li>Catalogs consulted: {_escape(', '.join(catalogs) or 'None')}</li>",
        f"<li>Catalog entry selected: {_escape(pricing.get('catalog_selected') or 'None — no exact entry matched')}</li>",
    ]
    if matched:
        source_url = pricing.get("source_url", "")
        source_link = (
            f'<a href="{_escape(source_url)}">Published pricing source</a>'
            if isinstance(source_url, str) and source_url.startswith("https://")
            else "No source URL recorded"
        )
        details.extend(
            [
                f"<li>Publisher: {_escape(summary.pricing_publisher or 'Unresolved')}</li>",
                f"<li>Billing basis: {_escape(_billing_basis_label(summary.pricing_billing_basis))}</li>",
                f"<li>Currency policy: one reporting currency ({_escape(summary.pricing_currency)}), no conversion</li>",
                f"<li>Retrieved: {_escape(str(pricing.get('retrieved_at') or 'not recorded'))}</li>",
                f"<li>{source_link}</li>",
            ]
        )
    customer_catalog = pricing.get("customer_catalog_name")
    if customer_catalog:
        details.append(f"<li>Customer override catalog: {_escape(customer_catalog)}</li>")
    if any((item.pricing_publisher or "").casefold() == "anthropic" for item in models):
        details.append(
            "<li>Claude-family billing basis is a CCU-derived dollar-equivalent estimate, not an "
            "Azure token meter.</li>"
        )
    details.append(
        "<li>Rates are applied as an analysis-date estimate. Agreements, private offers, regions, "
        "and later prices may differ.</li>"
    )
    return f"""<article class="chart-card"><h2>Pricing source</h2>
      <p class="provenance-headline">{headline}</p>
      <details class="provenance-details"><summary>Technical pricing details</summary>
      <ul class="detail-list">{''.join(details)}</ul></details></article>"""


def _how_pricing_works() -> str:
    return (
        '<details class="how-pricing"><summary>How pricing works</summary><div class="detail-body">'
        "<p class=\"muted\">Costs use observed values first, then an exact customer rate, then an exact "
        "entry in a verified catalog. No rate is ever taken from a related model, a model family, or a "
        "currency conversion.</p>"
        "<p class=\"muted\">Unmatched models keep their token volume, their unresolved reason, and their "
        "remediation path. They are never folded into a total.</p></div></details>"
    )


def _cost_view_rows(report: AnalysisReport, view: str) -> str:
    """One scannable table per cost view. Unavailable is never rendered as $0."""
    portfolio = getattr(report, "workloads", None)
    summary = report.summary
    if view == "workload" and portfolio is not None:
        scope = "business" if portfolio.workload_identity_coverage_percent > 0 else "technical"
        rollups = portfolio.business_workloads if scope == "business" else portfolio.workloads
        rows = []
        for item in rollups:
            state = _workload_pricing_state(item)
            cost = (
                _money(item.estimated_cost, currency=item.pricing_currency)
                if item.estimated_cost is not None
                else "Unavailable"
            )
            rows.append(
                f'<tr><th scope="row">{_escape(item.workload_name)}<small>{_escape(_workload_badge_text(item))}</small></th>'
                f"<td>{item.pricing_coverage_percent:.0f}%</td>"
                f"<td>{_escape(cost)}</td><td>{_availability_badge(state)}</td></tr>"
            )
        body = "".join(rows) or '<tr><td colspan="4">No workload traffic observed.</td></tr>'
        return f'<table class="data-table"><caption>Cost by workload</caption><thead><tr><th scope="col">Workload</th><th scope="col">Coverage</th><th scope="col">Cost</th><th scope="col">Status</th></tr></thead><tbody>{body}</tbody></table>'
    if view == "component":
        components = [
            ("Fresh input", summary.fresh_input_cost_usd, summary.input_tokens - summary.cached_tokens),
            ("Cached input", summary.cached_input_cost_usd, summary.cached_tokens),
            ("Output", summary.output_cost_usd, summary.output_tokens),
        ]
        rows = []
        for label, value, tokens in components:
            state = (
                AvailabilityState(status="available", label="Priced")
                if value is not None
                else AvailabilityState(
                    status="missing_actionable", label="Exact rate missing", action_id="pricing"
                )
            )
            rows.append(
                f'<tr><th scope="row">{_escape(label)}<small>{max(0, tokens):,} tokens</small></th>'
                f"<td>{summary.pricing_coverage_tokens_percent:.0f}%</td>"
                f"<td>{_escape(_money(value, currency=summary.pricing_currency) if value is not None else 'Unavailable')}</td>"
                f"<td>{_availability_badge(state)}</td></tr>"
            )
        return f'<table class="data-table"><caption>Cost by token component</caption><thead><tr><th scope="col">Component</th><th scope="col">Coverage</th><th scope="col">Cost</th><th scope="col">Status</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'
    items = deployment_rollups(report) if view == "deployment" else model_rollups(report)
    rows = []
    for item in items:
        state = _model_pricing_state(item)
        name = item.deployment_name if view == "deployment" else _model_label(item)
        secondary = (
            f"{item.model_name} · {item.deployment_mode.title()}"
            if view == "deployment"
            else f"{item.deployment_mode.title()} · {_billing_basis_label(item.pricing_billing_basis)}"
        )
        cost = (
            _money(item.estimated_cost_usd, currency=item.pricing_currency)
            if item.estimated_cost_usd is not None
            else "Unavailable"
        )
        detail = (
            f"<tr class=\"row-detail\"><td colspan=\"4\"><details><summary>Provenance and remediation</summary>"
            f"<ul class=\"detail-list\"><li>Tokens: {item.total_tokens:,} ({item.input_tokens:,} input / "
            f"{item.cached_tokens:,} cached / {item.output_tokens:,} output)</li>"
            f"<li>Rates I/C/O: {_rate(item.input_price_per_million, currency=item.pricing_currency)} / "
            f"{_rate(item.cached_input_price_per_million, currency=item.pricing_currency)} / "
            f"{_rate(item.output_price_per_million, currency=item.pricing_currency)}</li>"
            f"<li>Source: {_escape(_pricing_source_label(item.pricing_source))}</li>"
            + (
                f"<li>Suggested override key: <code>{_escape(', '.join(item.suggested_override_keys) or item.canonical_model_key)}</code></li>"
                if item.unresolved_requests and not _identity_missing(item)
                else ""
            )
            + "</ul></details></td></tr>"
        )
        rows.append(
            f'<tr><th scope="row">{_escape(name)}<small>{_escape(secondary)}</small></th>'
            f"<td>{item.pricing_coverage_requests_percent:.0f}%</td>"
            f"<td>{_escape(cost)}</td><td>{_availability_badge(state)}</td></tr>{detail}"
        )
    caption = "Cost by deployment" if view == "deployment" else "Cost by model"
    heading = "Deployment" if view == "deployment" else "Model"
    body = "".join(rows) or f'<tr><td colspan="4">No {heading.casefold()} usage observed.</td></tr>'
    return f'<table class="data-table"><caption>{caption}</caption><thead><tr><th scope="col">{heading}</th><th scope="col">Coverage</th><th scope="col">Cost</th><th scope="col">Status</th></tr></thead><tbody>{body}</tbody></table>'


def _cost_view_toggle(report: AnalysisReport) -> str:
    portfolio = getattr(report, "workloads", None)
    has_workloads = portfolio is not None and bool(portfolio.workloads)
    default = "workload" if has_workloads else "deployment"
    views = [
        ("workload", "Workload"),
        ("deployment", "Deployment"),
        ("model", "Model"),
        ("component", "Token component"),
    ]
    if not has_workloads:
        views = views[1:]
    buttons = "".join(
        f'<button type="button" class="view-tab" data-cost-view="{value}" '
        f'aria-pressed="{"true" if value == default else "false"}">{_escape(label)}</button>'
        for value, label in views
    )
    panels = "".join(
        f'<div class="cost-view" data-cost-panel="{value}"{"" if value == default else " hidden"}>'
        f'<div class="table-scroll">{_cost_view_rows(report, value)}</div></div>'
        for value, _label in views
    )
    note = ""
    if has_workloads and portfolio.workload_identity_coverage_percent <= 0:
        note = (
            '<p class="muted">Showing deployment-backed technical workloads. Configure business '
            "workload names with <code>tokenlens-azure foundry workloads configure</code> to see "
            "application, agent, and process cost.</p>"
        )
    return (
        '<section class="panel table-panel"><div class="view-toggle" role="group" aria-label="Cost view">'
        f'<span class="label">View by</span>{buttons}</div>{note}{panels}</section>'
    )


def _cost_analysis_panel(report: AnalysisReport) -> str:
    summary = report.summary
    state = _cost_state(report)
    banner: AvailabilityState = state["banner"]  # type: ignore[assignment]
    models = state["models"]
    identity_missing = state["identity_missing"]
    needs_price = state["needs_price"]
    priced_models = [item for item in models if item.estimated_cost_usd is not None]
    top_model = max(priced_models, key=lambda item: item.estimated_cost_usd or 0, default=None)
    pricing = report.report_metadata.get("pricing", {})
    matched = summary.estimated_cost_usd is not None
    cost_display = (
        _money(summary.estimated_cost_usd, currency=summary.pricing_currency) if matched else "Unavailable"
    )
    cards = [
        f'<div class="card"><div class="label">Estimated cost</div>'
        f'<div class="value{"" if matched else " cost-name"}">{_escape(cost_display)}</div>'
        f'<div class="sub">{_availability_badge(banner if not matched else AvailabilityState(status="available", label=f"{summary.pricing_coverage_tokens_percent:.0f}% priced"))}</div></div>',
        f'<div class="card"><div class="label">Pricing coverage</div><div class="value">{summary.pricing_coverage_tokens_percent:.0f}%</div>'
        f'<div class="sub">{summary.total_tokens - summary.unresolved_tokens:,} of {summary.total_tokens:,} tokens</div></div>',
        f'<div class="card"><div class="label">Unpriced tokens</div><div class="value cost-name">{summary.unresolved_tokens:,}</div>'
        f'<div class="sub">{_escape("Excluded from the total" if summary.unresolved_tokens else "Every observed token is priced")}</div></div>',
        f'<div class="card"><div class="label">Models requiring pricing</div><div class="value cost-name">{len(needs_price):,}</div>'
        f'<div class="sub">{_escape(f"{len(identity_missing):,} separate identity failure(s)" if identity_missing else "No identity failures")}</div></div>',
    ]
    # Conditional cards appear only when they carry information.
    if top_model is not None:
        cards.append(
            f'<div class="card"><div class="label">Highest cost model</div>'
            f'<div class="value cost-name">{_escape(_model_label(top_model))}</div>'
            f'<div class="sub">{_money(top_model.estimated_cost_usd, currency=top_model.pricing_currency)}</div></div>'
        )
    if matched and pricing.get("retrieved_at"):
        cards.append(
            f'<div class="card"><div class="label">Pricing snapshot</div>'
            f'<div class="value cost-name">{_escape(str(pricing.get("retrieved_at")))}</div>'
            f'<div class="sub">{_escape(summary.pricing_currency)} · matched catalog entry</div></div>'
        )
    if matched and summary.pricing_billing_basis:
        cards.append(
            f'<div class="card"><div class="label">Rate source</div>'
            f'<div class="value cost-name">{_escape(_pricing_source_label(summary.pricing_source))}</div>'
            f'<div class="sub">{_escape(_billing_basis_label(summary.pricing_billing_basis))}</div></div>'
        )
    identity_banner = ""
    if identity_missing and banner.action_id != "identity":
        identity_banner = (
            '<section class="state-banner state-banner-danger">'
            + _availability_badge(
                AvailabilityState(
                    status="missing_blocking",
                    label="Model identity missing",
                    reason=(
                        f"{len(identity_missing)} telemetry slice(s) must be recollected or enriched "
                        f"before pricing. {state['identity_tokens']:,} tokens are affected."
                    ),
                )
            )
            + "</section>"
        )
    action_label = _REMEDIATION[banner.action_id][0] if banner.action_id else ""
    action_link = (
        f'<a class="banner-action" href="#resolve-pricing" data-open-remediation="1">{_escape(action_label)}</a>'
        if banner.action_id
        else ""
    )
    return f"""<section class="state-banner state-banner-{banner.tone}">{_availability_badge(banner)}{action_link}</section>
    {identity_banner}
    {_remediation_panel(banner, models)}
    <section class="metrics cost-metrics">{''.join(cards)}</section>
    <section class="grid"><article class="chart-card"><h2>Cost composition</h2>{_cost_composition(report)}</article>
    {_pricing_provenance(report, models)}</section>
    {_cost_view_toggle(report)}
    {_how_pricing_works()}
    <footer>tokenlens-for-azure · created by Tzahi Ariel</footer>"""



def _workload_badge_text(item) -> str:
    """The visible badge that tells technical defaults from configured business rows."""
    if item.workload_scope == "technical":
        status = {
            "needs_configuration": "Needs configuration",
            "partially_configured": "Partly configured",
            "configured": "Configured",
        }.get(item.configuration_status, "Needs configuration")
        return f"Technical · {status}"
    if item.workload_id == "unassigned":
        return "Unassigned · request tagging required"
    return "Business · Configured"


def _workload_pricing_state(item) -> AvailabilityState:
    total = item.total_tokens or 0
    # A blocking identity failure is only claimed when the whole rollup is
    # unidentified. A mixed rollup reports the rate problem and names the
    # affected slice, because the two have different remediations.
    if total and item.identity_unresolved_tokens >= total:
        return AvailabilityState(
            status="missing_blocking",
            label="Model identity missing",
            reason="Recollect or enrich this slice before any rate can apply",
            action_id="identity",
        )
    if item.estimated_cost is not None and not item.unpriced_tokens:
        return AvailabilityState(status="available", label="Priced")
    if item.estimated_cost is not None:
        return AvailabilityState(
            status="partial",
            label=f"Partly priced · {item.pricing_coverage_percent:.0f}%",
            reason=f"{item.unpriced_tokens:,} tokens excluded",
            action_id="pricing",
        )
    if not total:
        return AvailabilityState(
            status="not_measured",
            label="No traffic in this window",
        )
    return AvailabilityState(
        status="missing_actionable",
        label="Exact rate missing",
        reason=(
            f"{item.identity_unresolved_tokens:,} of these tokens also need collection identity"
            if item.identity_unresolved_tokens
            else None
        ),
        action_id="pricing",
    )


def _workload_value(value, *, currency: str = "USD", precision: int = 4) -> str:
    return _money(value, currency=currency, precision=precision) if value is not None else "Unavailable"


def _workload_rows(rollups, *, scope: str) -> str:
    rows = []
    for item in rollups:
        tasks = item.tasks
        cost_per_request = (
            _money(item.cost_per_request, currency=item.pricing_currency, precision=6)
            if item.cost_per_request is not None
            else "Not measured"
        )
        cost_per_solved = (
            _money(tasks.cost_per_solved_task, currency=item.pricing_currency, precision=6)
            if tasks is not None and tasks.cost_per_solved_task is not None
            else "Not measured"
        )
        requests = f"{item.requests:,}" if item.requests is not None else "Not measured"
        detail = (
            '<tr class="row-detail"><td colspan="9"><details><summary>Workload detail</summary>'
            f'<ul class="detail-list">'
            f'<li>Deployments: {_escape(", ".join(item.deployments) or "none")}</li>'
            f'<li>Models: {_escape(", ".join(item.models) or "none")}</li>'
            f'<li>Token mix: {item.input_tokens or 0:,} input / {item.cached_tokens or 0:,} cached / {item.output_tokens or 0:,} output</li>'
            f'<li>Allocation: {_escape(item.allocation_confidence.replace("_", " "))} · source {_escape(item.identity_source.replace("_", " "))}</li>'
            f'<li>Daily cost: average {_workload_value(item.average_daily_cost, currency=item.pricing_currency)} · '
            f'P50 {_workload_value(item.p50_daily_cost, currency=item.pricing_currency)} · '
            f'P90 {_workload_value(item.p90_daily_cost, currency=item.pricing_currency)} over {item.active_days:,} active day(s)</li>'
            + (
                f'<li>Partial days in window: {_escape(", ".join(item.partial_days[:5]))}</li>'
                if item.partial_days
                else ""
            )
            + (
                f'<li>Task economics: {tasks.attempted_tasks:,} attempted · {tasks.solved_tasks or 0:,} solved · '
                f'retries per task {tasks.retries_per_task if tasks.retries_per_task is not None else "not measured"} · '
                f'model calls per task {tasks.model_calls_per_task if tasks.model_calls_per_task is not None else "not measured"}</li>'
                if tasks is not None and tasks.measured
                else '<li>Task economics: Not measured — task identity and outcomes were not collected</li>'
            )
            + "</ul></details></td></tr>"
        )
        rows.append(
            f'<tr><th scope="row">{_escape(item.workload_name)}<small>{_escape(_workload_badge_text(item))}</small></th>'
            f"<td>{_escape(item.workload_type.replace('_', ' '))}</td>"
            f"<td>{_escape(', '.join(item.deployments) or 'none')}</td>"
            f"<td>{_escape(requests)}</td>"
            f"<td>{item.total_tokens or 0:,}</td>"
            f"<td>{_escape(_workload_value(item.estimated_cost, currency=item.pricing_currency))}</td>"
            f"<td>{_escape(cost_per_request)}</td>"
            f"<td>{_escape(cost_per_solved)}</td>"
            f"<td>{_availability_badge(_workload_pricing_state(item))}</td></tr>{detail}"
        )
    empty = f'<tr><td colspan="9">No {scope} workload traffic observed.</td></tr>'
    return "".join(rows) or empty


def _workload_cost_chart(rollups) -> str:
    priced = [item for item in rollups if item.estimated_cost is not None and item.estimated_cost > 0]
    if not priced:
        tokens = [item for item in rollups if (item.total_tokens or 0) > 0]
        if not tokens:
            return '<p class="muted">No workload traffic was observed in this window.</p>'
        largest = max(item.total_tokens or 0 for item in tokens) or 1
        bars = "".join(
            f'<div class="bar-row"><span>{_escape(item.workload_name)}</span>'
            f'<div class="bar-track"><i style="width:{(item.total_tokens or 0) / largest * 100:.1f}%"></i></div>'
            f"<strong>{item.total_tokens or 0:,} tokens</strong></div>"
            for item in tokens
        )
        return (
            '<p class="muted">Cost is unavailable, so workloads are compared by token volume.</p>'
            + bars
        )
    largest = max(item.estimated_cost or 0 for item in priced) or 1
    return "".join(
        f'<div class="bar-row"><span>{_escape(item.workload_name)}</span>'
        f'<div class="bar-track"><i style="width:{(item.estimated_cost or 0) / largest * 100:.1f}%"></i></div>'
        f"<strong>{_escape(_money(item.estimated_cost, currency=item.pricing_currency))}</strong></div>"
        for item in priced
    )


def _workload_daily_trend(rollups, *, currency: str = "USD") -> str:
    """Daily cost trend. One workload gets a trend instead of a 100% share chart."""
    days: dict[str, dict[str, float]] = {}
    for item in rollups:
        for day, value in item.daily_costs:
            days.setdefault(day, {})[item.workload_name] = value
    if not days:
        return '<p class="muted">Daily cost requires at least one resolved rate. Not measured.</p>'
    ordered = sorted(days)
    names = sorted({name for values in days.values() for name in values})
    header = "".join(f"<th scope=\"col\">{_escape(name)}</th>" for name in names)
    rows = "".join(
        f'<tr><th scope="row">{_escape(day)}</th>'
        + "".join(
            f"<td>{_escape(_money(days[day].get(name), currency=currency)) if days[day].get(name) is not None else 'Not measured'}</td>"
            for name in names
        )
        + "</tr>"
        for day in ordered
    )
    return (
        '<table class="data-table"><caption>Daily cost by workload · partial days are listed in each '
        f'workload detail</caption><thead><tr><th scope="col">Day</th>{header}</tr></thead>'
        f"<tbody>{rows}</tbody></table>"
    )


def _workloads_panel(report: AnalysisReport) -> str:
    portfolio = getattr(report, "workloads", None)
    if portfolio is None or not portfolio.workloads:
        return (
            '<section class="panel"><div class="empty">Workload economics needs at least one resolved '
            "deployment. Collect Azure Monitor metrics with <code>tokenlens-azure foundry collect</code> "
            "or instrument requests with a <code>workload</code> tag.</div></section>"
            "<footer>tokenlens-for-azure · created by Tzahi Ariel</footer>"
        )
    has_business = portfolio.workload_identity_coverage_percent > 0
    default_scope = "business" if has_business else "technical"
    business_rows = [
        item for item in portfolio.business_workloads if item.workload_id != "unassigned"
    ]
    unassigned = next(
        (item for item in portfolio.business_workloads if item.workload_id == "unassigned"), None
    )
    needs_configuration = [
        item for item in portfolio.workloads if item.configuration_status == "needs_configuration"
    ]
    scoped = portfolio.business_workloads if has_business else portfolio.workloads
    top = max(
        (item for item in scoped if item.estimated_cost is not None),
        key=lambda item: item.estimated_cost or 0,
        default=None,
    )
    intro = ""
    if not has_business:
        intro = (
            '<section class="state-banner state-banner-info">'
            + _availability_badge(
                AvailabilityState(
                    status="not_applicable",
                    label="Showing deployment-backed technical workloads",
                    reason=(
                        "Configure business workload names to see application, agent, and process cost. "
                        f"{len(needs_configuration)} deployment(s) need configuration."
                    ),
                )
            )
            + '<code class="banner-action">tokenlens-azure foundry workloads configure</code></section>'
        )
    cards = [
        f'<div class="card"><div class="label">Priced workload cost</div>'
        f'<div class="value{"" if portfolio.priced_cost is not None else " cost-name"}">'
        f'{_escape(_workload_value(portfolio.priced_cost, currency=portfolio.reporting_currency))}</div>'
        f'<div class="sub">{portfolio.pricing_coverage_percent:.0f}% of tokens priced</div></div>',
        f'<div class="card"><div class="label">Technical deployment coverage</div>'
        f'<div class="value">{portfolio.technical_workload_coverage_percent:.0f}%</div>'
        f'<div class="sub">{len(portfolio.workloads):,} technical workload(s)</div></div>',
        f'<div class="card"><div class="label">Business identity coverage</div>'
        f'<div class="value">{portfolio.workload_identity_coverage_percent:.0f}%</div>'
        f'<div class="sub">{len(business_rows):,} business workload(s)</div></div>',
        f'<div class="card"><div class="label">Unpriced tokens</div>'
        f'<div class="value cost-name">{portfolio.unpriced_tokens:,}</div>'
        f'<div class="sub">of {portfolio.total_tokens:,} observed tokens</div></div>',
    ]
    if top is not None:
        cards.append(
            f'<div class="card"><div class="label">Highest cost workload</div>'
            f'<div class="value cost-name">{_escape(top.workload_name)}</div>'
            f'<div class="sub">{_escape(_money(top.estimated_cost, currency=top.pricing_currency))}</div></div>'
        )
    unassigned_section = ""
    if unassigned is not None:
        unassigned_section = (
            '<section class="state-banner state-banner-warning">'
            + _availability_badge(
                AvailabilityState(
                    status="missing_actionable",
                    label="Unassigned workload",
                    reason=(
                        (
                            f"{_money(unassigned.estimated_cost, currency=unassigned.pricing_currency)} "
                            f"({unassigned.total_tokens or 0:,} tokens)"
                            if unassigned.estimated_cost is not None
                            else f"{unassigned.total_tokens or 0:,} tokens"
                        )
                        + " cannot be attributed to a business workload."
                        " Tag requests with tokenlens.workload or map a dedicated deployment."
                    ),
                )
            )
            + "</section>"
        )
    readiness_rows = "".join(
        f'<tr><th scope="row">{_escape(item.workload_name)}</th>'
        f'<td>{_availability_badge(AvailabilityState(status="available", label="Exact") if item.allocation_confidence != "unallocated" else AvailabilityState(status="missing_actionable", label="Unassigned"))}</td>'
        f"<td>{_availability_badge(_workload_pricing_state(item))}</td>"
        f"<td>{_escape(_workload_result(item))}</td></tr>"
        for item in (portfolio.business_workloads if has_business else portfolio.workloads)
    )
    scope_tabs = (
        '<div class="view-toggle" role="group" aria-label="Workload scope"><span class="label">Workload scope</span>'
        f'<button type="button" class="view-tab" data-workload-scope="business" aria-pressed="{"true" if has_business else "false"}">Business</button>'
        f'<button type="button" class="view-tab" data-workload-scope="technical" aria-pressed="{"false" if has_business else "true"}">Technical deployments</button></div>'
    )
    table_header = (
        '<thead><tr><th scope="col">Workload</th><th scope="col">Type</th><th scope="col">Deployments</th>'
        '<th scope="col">Requests</th><th scope="col">Tokens</th><th scope="col">Est. cost</th>'
        '<th scope="col">Cost/request</th><th scope="col">Cost/solved task</th>'
        '<th scope="col">Pricing</th></tr></thead>'
    )
    business_panel = (
        f'<div class="workload-scope" data-workload-panel="business"{"" if default_scope == "business" else " hidden"}>'
        f'<div class="table-scroll"><table class="data-table"><caption>Business workload portfolio</caption>'
        f'{table_header}<tbody>{_workload_rows(portfolio.business_workloads, scope="business")}</tbody></table></div></div>'
    )
    technical_panel = (
        f'<div class="workload-scope" data-workload-panel="technical"{"" if default_scope == "technical" else " hidden"}>'
        f'<div class="table-scroll"><table class="data-table"><caption>Technical deployment workloads</caption>'
        f'{table_header}<tbody>{_workload_rows(portfolio.workloads, scope="technical")}</tbody></table></div></div>'
    )
    stale = (
        f'<p class="muted">Stale deployments retained for reconciliation: '
        f'{_escape(", ".join(portfolio.stale_deployments))}</p>'
        if portfolio.stale_deployments
        else ""
    )
    shared = (
        f'<p class="muted">Shared deployments without request tags: '
        f'{_escape(", ".join(portfolio.shared_deployments_without_tags))}. Azure Monitor can report '
        "deployment cost, but not cost per workload.</p>"
        if portfolio.shared_deployments_without_tags
        else ""
    )
    return f"""{intro}{unassigned_section}
    <section class="metrics">{''.join(cards)}</section>
    <section class="panel table-panel">{scope_tabs}{business_panel}{technical_panel}{shared}{stale}</section>
    <section class="grid"><article class="chart-card"><h2>Estimated cost by workload</h2>
      <p>Unavailable cost is never drawn as zero.</p>{_workload_cost_chart(scoped)}</article>
    <article class="chart-card"><h2>Daily cost trend</h2>
      <div class="table-scroll">{_workload_daily_trend(scoped, currency=portfolio.reporting_currency)}</div></article></section>
    <section class="panel table-panel"><h2>Workload readiness</h2>
      <p class="muted">Identity and pricing are separate dimensions with separate remediations.</p>
      <div class="table-scroll"><table class="data-table"><caption>Identity versus pricing readiness</caption>
      <thead><tr><th scope="col">Workload</th><th scope="col">Identity</th><th scope="col">Pricing</th><th scope="col">Result</th></tr></thead>
      <tbody>{readiness_rows or '<tr><td colspan="4">No workload observed.</td></tr>'}</tbody></table></div></section>
    {_workload_task_section(report)}
    <footer>tokenlens-for-azure · created by Tzahi Ariel</footer>"""


def _workload_result(item) -> str:
    if item.allocation_confidence == "unallocated":
        return (
            "Cost known; workload allocation unavailable"
            if item.estimated_cost is not None
            else "Usage only; cost and allocation unavailable"
        )
    if item.estimated_cost is not None and not item.unpriced_tokens:
        return "Full workload economics"
    if item.estimated_cost is not None:
        return "Partial workload economics"
    return "Usage only; cost unavailable"


def _workload_task_section(report: AnalysisReport) -> str:
    """Task economics drills down beneath workloads when task evidence exists."""
    portfolio = getattr(report, "workloads", None)
    measured = [
        item
        for item in ((portfolio.business_workloads + portfolio.workloads) if portfolio else [])
        if item.tasks is not None and item.tasks.measured
    ]
    if not measured:
        return (
            '<section class="panel table-panel"><h2>Task economics</h2>'
            f'<p class="muted">{_availability_badge(AvailabilityState(status="not_measured", label="Not measured", reason="Task identity and outcomes were not collected. Workload economics remains available."))}</p>'
            "</section>"
        )
    rows = "".join(
        f'<tr><th scope="row">{_escape(item.workload_name)}</th>'
        f"<td>{item.tasks.attempted_tasks or 0:,}</td><td>{item.tasks.closed_tasks or 0:,}</td>"
        f"<td>{item.tasks.solved_tasks or 0:,}</td>"
        f"<td>{_escape(_workload_value(item.tasks.cost_per_attempted_task, currency=item.pricing_currency, precision=6))}</td>"
        f"<td>{_escape(_workload_value(item.tasks.cost_per_solved_task, currency=item.pricing_currency, precision=6))}</td>"
        f"<td>{_escape(_workload_value(item.tasks.cost_per_correct_task, currency=item.pricing_currency, precision=6))}</td>"
        f"<td>{_escape(_workload_value(item.tasks.failed_trajectory_cost, currency=item.pricing_currency))}</td></tr>"
        for item in measured
    )
    return (
        '<section class="panel table-panel"><h2>Task economics by workload</h2>'
        '<p class="muted">Cost per business task requires explicit task identity and outcomes.</p>'
        '<div class="table-scroll"><table class="data-table"><caption>Task economics drill-down</caption>'
        '<thead><tr><th scope="col">Workload</th><th scope="col">Attempted</th><th scope="col">Closed</th>'
        '<th scope="col">Solved</th><th scope="col">Cost/attempted</th><th scope="col">Cost/solved</th>'
        '<th scope="col">Cost/correct</th><th scope="col">Failed spend</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div></section>"
    )


def _ptu_throughput_chart(series: PtuThroughputSeries) -> tuple[str, str]:
    if not series.points:
        return "", ""
    width, height = 760, 240
    left, right, top, bottom = 54, 730, 18, 200
    max_tpm = max([point.tpm for point in series.points] + [series.reference_tpm, series.ptu_capacity_tpm or 0]) or 1
    max_minutes = series.points[-1].minutes_from_start or 1

    def sx(minutes: float) -> float:
        return left + minutes / max_minutes * (right - left)

    def sy(tpm: float) -> float:
        return bottom - tpm / max_tpm * (bottom - top)

    path = " ".join(
        f"{sx(point.minutes_from_start):.1f},{sy(point.tpm):.1f}" for point in series.points
    )
    # Cap focusable markers so keyboard users can step through a manageable
    # set of exact values rather than every raw bucket.
    marker_stride = max(1, len(series.points) // 24)
    markers = "".join(
        f'<circle cx="{sx(point.minutes_from_start):.1f}" cy="{sy(point.tpm):.1f}" r="3.2" tabindex="0" '
        f'aria-label="Minute {point.minutes_from_start}: {point.tpm:,.0f} TPM"><title>Minute {point.minutes_from_start}: {point.tpm:,.0f} TPM</title></circle>'
        for point in series.points[::marker_stride]
    )
    reference_y = sy(series.reference_tpm)
    average_y = sy(series.average_tpm)
    capacity_line = (
        f'<line x1="{left}" y1="{sy(series.ptu_capacity_tpm):.1f}" x2="{right}" y2="{sy(series.ptu_capacity_tpm):.1f}" '
        f'class="ptu-capacity-line" stroke="var(--amber)" stroke-width="1.5"></line>'
        f'<text x="{right}" y="{sy(series.ptu_capacity_tpm) - 4:.1f}" text-anchor="end" class="axis-value">PTU capacity {series.ptu_capacity_tpm:,.0f}</text>'
        if series.ptu_capacity_tpm
        else ""
    )
    svg = f"""<svg class="columns ptu-throughput" viewBox="0 0 {width} {height}" role="img" aria-labelledby="ptu-tp-title ptu-tp-desc">
      <title id="ptu-tp-title">Throughput over time</title>
      <desc id="ptu-tp-desc">Weighted tokens-per-minute across {len(series.points)} five-minute buckets. Average {series.average_tpm:,.0f} TPM, P95 reference {series.reference_tpm:,.0f} TPM{f", PTU capacity {series.ptu_capacity_tpm:,.0f} TPM" if series.ptu_capacity_tpm else ""}.</desc>
      <line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis"></line>
      <line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" class="axis"></line>
      <line x1="{left}" y1="{average_y:.1f}" x2="{right}" y2="{average_y:.1f}" stroke="var(--accent)" stroke-width="1.5" stroke-dasharray="5 4"></line>
      <text x="{right}" y="{average_y - 4:.1f}" text-anchor="end" class="axis-value">Average {series.average_tpm:,.0f}</text>
      <line x1="{left}" y1="{reference_y:.1f}" x2="{right}" y2="{reference_y:.1f}" stroke="var(--pink)" stroke-width="1.2" stroke-dasharray="2 3"></line>
      <text x="{left}" y="{reference_y - 4:.1f}" class="axis-value">P95 {series.reference_tpm:,.0f}</text>
      {capacity_line}
      <polyline points="{path}" fill="none" stroke="var(--green)" stroke-width="2"></polyline>
      {markers}
      <text x="{left}" y="{height - 4}" class="axis-label">0 min</text>
      <text x="{right}" y="{height - 4}" text-anchor="end" class="axis-label">{max_minutes:,} min</text>
    </svg>"""
    sample_stride = max(1, len(series.points) // 30)
    rows = "".join(
        f"<tr><td>{point.minutes_from_start:,}</td><td>{point.tpm:,.0f}</td></tr>"
        for point in series.points[::sample_stride]
    )
    table = f"""<table class="data-table"><caption>Accessible throughput sample ({len(series.points):,} buckets total, sampled every {sample_stride})</caption>
      <thead><tr><th scope="col">Minute</th><th scope="col">Weighted TPM</th></tr></thead><tbody>{rows}</tbody></table>"""
    return svg, table


def _ptu_cost_explorer_chart(curve: PtuCostCurve) -> tuple[str, str]:
    if not curve.sustained_tpm:
        return "", ""
    width, height = 760, 260
    left, right, top, bottom = 60, 730, 18, 210
    max_x = max(curve.sustained_tpm) or 1
    max_y = max(curve.payg_monthly + curve.hybrid_monthly + [curve.payg_at_observed_average or 0, curve.hybrid_at_observed_average or 0]) or 1

    def sx(value: float) -> float:
        return left + value / max_x * (right - left)

    def sy(value: float) -> float:
        return bottom - value / max_y * (bottom - top)

    payg_path = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(curve.sustained_tpm, curve.payg_monthly))
    hybrid_path = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(curve.sustained_tpm, curve.hybrid_monthly))
    shade_start = curve.lower_break_even_tpm if curve.lower_break_even_tpm is not None else 0.0
    shade_end = curve.upper_break_even_tpm if curve.upper_break_even_tpm is not None else max_x
    shade = (
        f'<rect x="{sx(shade_start):.1f}" y="{top}" width="{max(0.0, sx(shade_end) - sx(shade_start)):.1f}" height="{bottom - top}" '
        f'fill="var(--green)" opacity="0.12"></rect>'
        if curve.lower_break_even_tpm is not None or curve.upper_break_even_tpm is not None
        else ""
    )
    markers = []
    for label, x_value, color in (
        ("Lower break-even", curve.lower_break_even_tpm, "var(--amber)"),
        ("Upper break-even", curve.upper_break_even_tpm, "var(--amber)"),
        ("Selected PTU capacity", curve.ptu_capacity_tpm, "var(--pink)"),
        ("Observed average TPM", curve.observed_average_tpm, "var(--accent)"),
    ):
        if x_value is None:
            continue
        markers.append(
            f'<line x1="{sx(x_value):.1f}" y1="{top}" x2="{sx(x_value):.1f}" y2="{bottom}" stroke="{color}" stroke-width="1.2" stroke-dasharray="3 3"></line>'
            f'<circle cx="{sx(x_value):.1f}" cy="{bottom}" r="3.4" tabindex="0" fill="{color}" '
            f'aria-label="{label}: {x_value:,.0f} TPM"><title>{label}: {x_value:,.0f} TPM</title></circle>'
        )
    current_markers = []
    if curve.payg_at_observed_average is not None:
        current_markers.append(
            f'<circle cx="{sx(curve.observed_average_tpm):.1f}" cy="{sy(curve.payg_at_observed_average):.1f}" r="4.5" tabindex="0" fill="var(--accent)" '
            f'aria-label="Current PAYG cost: {_escape(_money(curve.payg_at_observed_average))} per month"><title>Current PAYG: {_escape(_money(curve.payg_at_observed_average))}/month</title></circle>'
        )
    if curve.hybrid_at_observed_average is not None:
        current_markers.append(
            f'<circle cx="{sx(curve.observed_average_tpm):.1f}" cy="{sy(curve.hybrid_at_observed_average):.1f}" r="4.5" tabindex="0" fill="var(--green)" '
            f'aria-label="Current hybrid PTU + PAYG spillover cost: {_escape(_money(curve.hybrid_at_observed_average))} per month"><title>Current hybrid: {_escape(_money(curve.hybrid_at_observed_average))}/month</title></circle>'
        )
    svg = f"""<svg class="columns ptu-cost-explorer" viewBox="0 0 {width} {height}" role="img" aria-labelledby="ptu-cost-title ptu-cost-desc">
      <title id="ptu-cost-title">PAYG versus PTU hybrid cost explorer</title>
      <desc id="ptu-cost-desc">Estimated monthly cost across sustained token-per-minute levels. Selected PTU capacity {curve.selected_ptu:,} units ({curve.ptu_capacity_tpm:,.0f} TPM). {f"Lower break-even at {curve.lower_break_even_tpm:,.0f} TPM." if curve.lower_break_even_tpm is not None else "No break-even within the plotted range."}{f" Upper break-even at {curve.upper_break_even_tpm:,.0f} TPM." if curve.upper_break_even_tpm is not None else ""}</desc>
      {shade}
      <line x1="{left}" y1="{bottom}" x2="{right}" y2="{bottom}" class="axis"></line>
      <line x1="{left}" y1="{top}" x2="{left}" y2="{bottom}" class="axis"></line>
      <polyline points="{payg_path}" fill="none" stroke="var(--soft)" stroke-width="2" stroke-dasharray="6 4"></polyline>
      <polyline points="{hybrid_path}" fill="none" stroke="var(--green)" stroke-width="2"></polyline>
      {''.join(markers)}
      {''.join(current_markers)}
      <text x="{left}" y="{height - 4}" class="axis-label">0 TPM</text>
      <text x="{right}" y="{height - 4}" text-anchor="end" class="axis-label">{max_x:,.0f} TPM</text>
    </svg>"""
    legend = (
        '<div class="legend"><span class="legend-row"><span class="swatch" style="background:var(--soft)"></span>PAYG (dashed)</span>'
        '<span class="legend-row"><span class="swatch" style="background:var(--green)"></span>PTU + spillover (solid)</span>'
        '<span class="legend-row"><span class="swatch" style="background:var(--amber)"></span>Break-even</span>'
        '<span class="legend-row"><span class="swatch" style="background:var(--pink)"></span>Selected PTU capacity</span>'
        '<span class="legend-row"><span class="swatch" style="background:var(--accent)"></span>Observed average TPM</span></div>'
    )
    sample_indexes = sorted({0, len(curve.sustained_tpm) // 4, len(curve.sustained_tpm) // 2, 3 * len(curve.sustained_tpm) // 4, len(curve.sustained_tpm) - 1})
    rows = "".join(
        f"<tr><td>{curve.sustained_tpm[i]:,.0f}</td><td>{_money(curve.payg_monthly[i])}</td><td>{_money(curve.hybrid_monthly[i])}</td></tr>"
        for i in sample_indexes
    )
    table = f"""<table class="data-table"><caption>Accessible cost curve sample (selected PTU {curve.selected_ptu:,} · capacity {curve.ptu_capacity_tpm:,.0f} TPM)</caption>
      <thead><tr><th scope="col">Sustained TPM</th><th scope="col">PAYG / month</th><th scope="col">Hybrid / month</th></tr></thead><tbody>{rows}</tbody></table>"""
    return legend + svg, table


_ELIGIBILITY_EXPLANATIONS = {
    "eligible_sufficient_evidence": "Eligible and sufficient evidence: capacity, mode, workload history, and pricing are all available.",
    "eligible_insufficient_evidence": "Eligible but insufficient evidence: capacity and mode are supported, but the observed history is too sparse for a confident recommendation or cost curve.",
    "model_capacity_unavailable": "Capacity data required: this exact model and version is not in TokenLens's verified PTU capacity catalog, so capacity is unavailable rather than unsupported. A related model's capacity is never substituted.",
    "ptu_not_applicable": "PTU not applicable: this is a partner/Marketplace model billed per token or in provider credit units (Anthropic CCUs). Azure PTU capacity is a first-party purchase that cannot be bought for it.",
    "pricing_unavailable": "Pricing unavailable: workload evidence is sufficient, but exact USD PAYG pricing is required before a cost curve can be shown.",
    "deployment_mode_unavailable": "Deployment mode unavailable: PTU sizing requires a Global or Regional deployment mode.",
    "collection_identity_error": "Collection identity error: this deployment's exact model was not resolved during collection, so capacity, pricing, and PTU conclusions are withheld for it. Re-run collection for that deployment.",
}


def _ptu_panel(report: AnalysisReport) -> str:
    analysis = report.ptu_analysis
    if not isinstance(analysis, PtuPortfolioAssessment):
        return '<section class="panel"><p class="empty">PTU analysis is unavailable for this report.</p></section>'
    pairs = deployment_slugs(analysis)
    dashboards = []
    for index, (item, slug) in enumerate(pairs):
        throughput_svg, throughput_table = (
            _ptu_throughput_chart(item.throughput_series) if item.throughput_series else ("", "")
        )
        cost_svg, cost_table = _ptu_cost_explorer_chart(item.cost_curve) if item.cost_curve else ("", "")
        dashboards.append(
            render_deployment(
                item,
                slug,
                selected=index == 0,
                throughput_svg=throughput_svg,
                throughput_table=throughput_table,
                cost_svg=cost_svg,
                cost_table=cost_table,
                eligibility_label=ELIGIBILITY_STATUS_LABELS.get(item.eligibility_status, item.eligibility_status),
                eligibility_explanation=_ELIGIBILITY_EXPLANATIONS.get(item.eligibility_status, ""),
            )
        )
    # The selected deployment's recommendation comes first. Portfolio counters
    # are context for that decision, not the headline.
    return f"""<p class="analytics-intro">The PTU Advisor renders one deployment at a time. Recommendation, confidence, metrics, evidence charts, capacity sizing, economics, and exports all describe the selected deployment only; unrelated model or deployment slices are never merged into one recommendation.</p>
    {selector(pairs)}
    {''.join(dashboards) or '<p class="empty">No deployments observed.</p>'}
    <section class="metrics ptu-metrics" aria-label="Portfolio summary counters">
      <div class="card"><div class="label">PTU recommended</div><div class="value">{analysis.recommended_deployments:,}</div><div class="sub">Strong workload fit</div></div>
      <div class="card"><div class="label">Borderline</div><div class="value">{analysis.borderline_deployments:,}</div><div class="sub">Validate with load testing</div></div>
      <div class="card"><div class="label">PAYG recommended</div><div class="value">{analysis.payg_deployments:,}</div><div class="sub">PAYG fits observed demand</div></div>
      <div class="card"><div class="label">Insufficient / unsupported</div><div class="value">{analysis.insufficient_deployments:,}</div><div class="sub">Needs evidence or capacity data</div></div>
      <div class="card"><div class="label">Time bucket</div><div class="value">{analysis.bucket_minutes} min</div><div class="sub">Offline trace aggregation</div></div>
    </section>
    {_ptu_legacy_summary(analysis)}
    <section class="panel ptu-source"><strong>Method source</strong><span>Adapted from the MIT-licensed <a href="{_escape(analysis.source_repository)}">PTU Advisor</a> at revision <code>{_escape(analysis.source_revision[:12])}</code>.</span></section>
    <footer>tokenlens-for-azure · created by Tzahi Ariel</footer>"""


def _ptu_legacy_summary(analysis: PtuPortfolioAssessment) -> str:
    """Keep the compact portfolio table so every deployment stays visible at once."""
    rows = []
    for item in analysis.deployments:
        eligibility_label = ELIGIBILITY_STATUS_LABELS.get(item.eligibility_status, item.eligibility_status)
        rows.append(
            f'<tr><th scope="row">{_escape(item.deployment_name)}<small>{_escape(item.model_name)} · {_escape(item.deployment_mode.title())}</small></th>'
            f'<td><span class="eligibility-badge {item.eligibility_status}">{_escape(eligibility_label)}</span></td>'
            f"<td>{_escape(item.recommendation)}</td>"
            f"<td>{item.average_tpm:,.0f}</td><td>{item.p95_tpm:,.0f}</td>"
            f"<td>{item.suggested_ptu if item.suggested_ptu is not None else 'Unavailable'}</td>"
            f"<td>{_money(item.payg_monthly_usd, precision=0)}</td><td>{_money(item.hybrid_monthly_usd, precision=0)}</td>"
            f"<td>{_escape(item.economic_result)}</td></tr>"
        )
    return f"""<section class="panel table-panel ptu-portfolio"><h2>Portfolio summary</h2>
      <p class="muted">All analyzed deployments, including those that cannot receive a PTU recommendation.</p>
      <div class="table-scroll"><table class="data-table"><caption>PTU assessment by deployment</caption>
      <thead><tr><th scope="col">Deployment</th><th scope="col">Eligibility</th><th scope="col">Recommendation</th><th scope="col">Avg TPM</th>
      <th scope="col">P95 TPM</th><th scope="col">Suggested PTU</th><th scope="col">PAYG / month</th><th scope="col">Hybrid / month</th><th scope="col">Economic result</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="9">No deployments observed.</td></tr>'}</tbody></table></div></section>"""


def report_html(report: AnalysisReport) -> str:
    summary = report.summary
    overview = overview_findings(report)
    actions = top_recommendations(overview)
    models = model_rollups(report)
    # A donut needs at least two slices to communicate anything.
    multi_model = len(models) > 1
    donut_svg, donut_table = _donut_chart(report) if multi_model else ("", "")
    column_svg, column_table = _column_chart(report)
    hidden_count = max(0, len(report.findings) - len(overview))
    config = report.report_metadata.get("materiality", {})
    cost_panel = _cost_analysis_panel(report)
    workloads_panel = _workloads_panel(report)
    ptu_panel = _ptu_panel(report)
    banner = _data_quality_banner(report)
    pricing_headline, pricing_reason = _pricing_status_copy(summary)
    deployment_label = "Single selected route" if len(report.deployments) == 1 else "Separate workload routes"
    deployment_word = "deployment" if len(report.deployments) == 1 else "deployments"
    cost_sub_overview = (
        f"{summary.pricing_coverage_tokens_percent:.1f}% of tokens priced"
        if summary.estimated_cost_usd is not None
        else pricing_reason
    )
    model_chart = (
        f'''<article class="chart-card"><h2>Token share by model</h2><p>Case variants of the same model family combine into one model slice.</p><div class="donut-wrap">{donut_svg}<div class="legend">{"".join(f'<div class="legend-row"><span class="swatch" style="background:{["#73c7ff","#57d68b","#ffc857","#ff8fa3","#b9a7ff","#5eead4","#f4a261"][i % 7]}"></span>{_escape(m.model_name)} · {m.total_tokens:,} · {m.token_share_percent:.1f}%</div>' for i, m in enumerate(models))}</div></div>{donut_table}</article>'''
        if multi_model
        else _composition_card(report)
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TokenLens for Azure — Report</title>
<style>
:root{{--bg:#11213b;--surface:#172a49;--surface-2:#1d3559;--border:#35527a;--text:#f3f7ff;--muted:#b5c5dc;--soft:#d2deee;--accent:#73c7ff;--green:#57d68b;--amber:#ffc857;--pink:#ff8fa3;--shadow:0 12px 30px rgba(0,0,0,.22);--status-success-fg:#8bf0b6;--status-success-bg:rgba(87,214,139,.16);--status-warning-fg:#ffd98a;--status-warning-bg:rgba(255,200,87,.16);--status-danger-fg:#ffb4c2;--status-danger-bg:rgba(255,143,163,.18);--status-info-fg:#a9dbff;--status-info-bg:rgba(115,199,255,.16);--status-neutral-fg:#ccd9ea;--status-neutral-bg:rgba(181,197,220,.14)}}
*{{box-sizing:border-box}}html,body{{min-height:100%}}html{{background:var(--bg)}}body{{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font:14px/1.4 "Segoe UI",Aptos,Calibri,Arial,sans-serif}}main{{width:100%;min-height:100vh;margin:0;padding:clamp(16px,1.5vw,28px)}}header{{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:14px;margin-bottom:14px}}.logo{{width:min(275px,55vw);height:auto;display:block}}.meta{{text-align:right;color:var(--muted);font-size:12px;line-height:1.5}}.meta strong{{color:var(--text);font-size:12px;margin-left:5px}}h1,h2,h3,p{{margin:0}}h2{{font-size:17px;letter-spacing:-.02em}}h3{{font-size:14px}}small,.muted{{display:block;color:var(--muted);font-size:12px}}.card,.panel,.chart-card,.kpi,.chart{{background:var(--surface);border:1px solid var(--border);border-radius:12px;box-shadow:var(--shadow)}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:12px}}.card,.kpi{{padding:11px 13px;min-height:76px}}.kpi b{{display:block;font-size:22px;color:var(--accent);margin-top:4px}}.kpi span,.label{{color:var(--muted);font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em}}.value{{font-size:22px;font-weight:700;letter-spacing:-.04em;margin-top:4px}}.sub{{font-size:12px;color:var(--muted);margin-top:2px}}.accent{{color:var(--accent)}}.overview-grid{{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(280px,.9fr);gap:12px;margin-bottom:12px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}.panel-head{{padding:11px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:baseline;gap:12px}}.panel-head a,.tab{{color:var(--link,var(--accent));font-weight:700}}.portfolio{{padding:0 14px}}.portfolio-row{{display:grid;grid-template-columns:1.15fr 2fr 145px;gap:12px;align-items:center;padding:9px 0;border-bottom:1px solid var(--border)}}.portfolio-row:last-child{{border-bottom:0}}.portfolio-row strong{{font-size:12px}}.portfolio-row small{{margin-top:1px;font-size:12px}}.portfolio-bar{{height:9px;border-radius:9px;background:var(--surface-2);overflow:hidden}}.portfolio-bar span{{height:100%;display:block;background:var(--accent);border-radius:9px}}.portfolio-value{{text-align:right}}.portfolio-value strong{{display:block;font-size:12px}}.portfolio-value small{{font-size:12px}}.actions{{padding:9px 14px 10px}}.actions ol{{padding:0 0 0 20px;margin:0}}.actions li{{padding:5px 0 5px 2px;border-bottom:1px solid var(--border)}}.actions li:last-child{{border-bottom:0}}.actions li strong,.actions li span{{display:block;font-size:12px}}.actions li span{{color:var(--muted);font-size:12px;margin-top:1px}}.finding-panel{{margin-bottom:12px}}.finding-list{{display:grid;grid-template-columns:repeat(3,1fr)}}.finding-card{{display:grid;grid-template-columns:52px minmax(0,1.3fr) minmax(86px,.8fr);gap:9px;align-items:start;padding:10px 12px;border-right:1px solid var(--border);border-bottom:1px solid var(--border)}}.finding-card:nth-child(3n){{border-right:0}}.finding-card:last-child{{border-bottom:0}}.finding-rule{{font:700 12px Consolas,monospace;border-left:4px solid var(--accent);padding-left:6px;color:var(--soft)}}.finding-card.high .finding-rule{{border-color:var(--pink)}}.finding-card.medium .finding-rule{{border-color:var(--amber)}}.finding-card.low .finding-rule{{border-color:var(--accent)}}.finding-copy strong,.finding-impact strong,.finding-action strong{{display:block;font-size:12px}}.finding-copy span,.finding-impact span,.finding-action span{{display:block;color:var(--muted);font-size:12px;margin-top:2px}}.finding-impact strong{{color:var(--accent)}}.finding-action{{grid-column:2 / -1}}.finding-action strong{{color:var(--green)}}.tabs{{margin-top:4px}}.tab-list{{display:flex;gap:5px;border-bottom:1px solid var(--border);margin-bottom:14px;position:sticky;top:0;z-index:6;background:var(--bg);overflow-x:auto;scrollbar-width:none;max-width:100%}}.tab-list::-webkit-scrollbar{{display:none}}.tab{{border:1px solid transparent;border-bottom:0;border-radius:8px 8px 0 0;background:transparent;padding:10px 13px;color:var(--muted);cursor:pointer;font:700 13px inherit;white-space:nowrap;min-height:44px;flex:0 0 auto}}.tab-short{{display:none}}.tab[aria-selected="true"]{{color:var(--text);background:var(--surface);border-color:var(--border)}}.tab:focus-visible,.donut-segment:focus-visible,.columns rect:focus-visible,summary:focus-visible{{outline:3px solid var(--amber);outline-offset:2px}}.js .tab-panel:not(.active){{display:none}}.tab-panel{{min-height:200px}}.analytics-intro{{color:var(--muted);font-size:12px;margin-bottom:12px}}.chart-grid{{display:grid;grid-template-columns:1fr 1.35fr;gap:12px;margin-bottom:12px}}.chart-card,.chart{{padding:14px;min-width:0}}.chart-card h2,.chart h2{{margin-bottom:3px}}.chart-card > p,.chart p{{color:var(--muted);font-size:12px;margin-bottom:10px}}.bar-row{{display:grid;grid-template-columns:150px 1fr 100px;align-items:center;gap:8px;margin:9px 0;font-size:12px}}.bar-track{{height:12px;background:var(--surface-2);border-radius:8px;overflow:hidden}}.bar-track i{{display:block;height:100%;background:var(--accent);border-radius:8px}}.donut-wrap{{display:flex;align-items:center;gap:12px;min-height:220px}}.donut{{width:220px;max-width:42%;overflow:visible}}.donut-total{{fill:var(--text);font-size:16px;font-weight:700}}.donut-label{{fill:var(--muted);font-size:12px}}.columns{{width:100%;height:auto;min-height:220px;overflow:visible}}.columns .axis,.axis{{stroke:var(--border);stroke-width:1}}.axis-label{{fill:var(--soft);font-size:12px}}.axis-value{{fill:var(--muted);font-size:12px}}svg text{{fill:var(--muted)}}svg circle{{fill:var(--green);stroke:var(--text);stroke-width:1}}.legend{{display:grid;gap:4px;min-width:130px}}.legend-row{{font-size:12px;color:var(--soft)}}.swatch{{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:0}}.data-table,.task table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px;color:var(--soft)}}.data-table caption,.task caption{{text-align:left;color:var(--muted);font-size:12px;margin-bottom:4px}}.data-table th,.data-table td,.task th,.task td{{padding:5px 6px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}}.data-table th:first-child,.data-table td:first-child,.task th:first-child,.task td:first-child{{text-align:left}}.data-table thead th,.task thead th{{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}}.table-panel{{padding:14px;margin-bottom:12px;overflow:auto}}.table-scroll{{max-width:100%;overflow-x:auto}}.chart-card .data-table,.table-panel .data-table{{max-width:none}}.scenario-list{{margin:0;padding-left:20px}}.scenario-list li{{padding:6px 0;border-bottom:1px solid var(--border)}}.scenario-list small{{display:block;color:var(--muted)}}.analytics-section{{margin-bottom:12px}}.analytics-section > .panel-head{{margin-bottom:0}}.details-list{{display:grid;gap:7px}}.deployment-details{{background:var(--surface);border:1px solid var(--border);border-radius:9px;overflow:hidden}}summary{{cursor:pointer;padding:10px 13px;list-style-position:inside}}summary span{{color:var(--muted);font-size:12px;margin-left:10px}}.detail-body{{border-top:1px solid var(--border);padding:10px 13px}}.mini-stats{{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-bottom:7px}}.mini-stats strong{{color:var(--text);margin-left:3px}}.detail-body .finding-card{{background:var(--surface-2);border:1px solid var(--border);border-radius:7px;margin-top:6px;grid-template-columns:52px minmax(0,1.3fr) minmax(86px,.8fr)}}.additional{{border-left:3px solid var(--amber)}}.empty{{padding:12px 14px;color:var(--muted);font-size:12px}}footer{{text-align:center;color:var(--muted);font-size:11px;letter-spacing:.04em;margin-top:12px}}@media(max-width:960px){{main{{padding:16px}}.metrics{{grid-template-columns:repeat(3,1fr)}}.overview-grid,.chart-grid,.grid{{grid-template-columns:1fr}}.finding-list{{grid-template-columns:1fr}}.finding-card,.finding-card:nth-child(3n){{border-right:0}}.donut-wrap{{justify-content:center}}}}@media(max-width:620px){{header{{display:block}}.meta{{text-align:left;margin-top:8px;font-size:12px}}.tab-full{{display:none}}.tab-short{{display:inline}}.tab{{padding:11px 14px;font-size:13px}}.single-usage-grid{{grid-template-columns:1fr 1fr}}.metrics{{grid-template-columns:repeat(2,1fr)}}.portfolio-row{{grid-template-columns:1fr 100px}}.portfolio-bar{{grid-column:1 / -1;grid-row:2}}.portfolio-value{{text-align:right}}.donut-wrap{{display:block}}.donut{{display:block;max-width:220px;margin:auto}}.data-table{{display:block;overflow-x:auto}}summary span{{display:block;margin:3px 0 0 21px}}}}@media print{{@page{{size:landscape;margin:.35in}}body{{background:#fff;color:#11213b;font-size:11px}}main{{max-width:none;padding:0}}header{{border-color:#9aa8ba}}.card,.panel,.chart-card,.kpi,.chart,.deployment-details{{box-shadow:none;background:#fff;border-color:#9aa8ba}}.metrics{{gap:5px}}.value{{font-size:16px}}.overview{{min-height:6.8in;page-break-after:always}}.usage{{page-break-before:always}}.tab-list{{display:none}}.js .tab-panel:not(.active){{display:block}}.finding-card,.portfolio-row{{border-color:#b8c2cf}}.finding-copy span,.finding-impact span,.finding-action span,.analytics-intro,.data-table caption,small,.muted{{color:#46556b}}.data-table th,.data-table td{{border-color:#b8c2cf}}footer{{color:#46556b}}}}
.donut-segment,.donut-track{{fill:none}}.cost-name{{font-size:15px;line-height:1.2}}.cost-bar{{display:grid;grid-template-columns:130px 1fr 110px;gap:9px;align-items:center;margin:12px 0;font-size:12px}}.cost-track{{height:14px;background:var(--surface-2);border-radius:8px;overflow:hidden;display:flex}}.cost-track i.excluded,.cost-track i.covered.partial{{background-image:repeating-linear-gradient(45deg,rgba(255,255,255,.4) 0 5px,rgba(255,255,255,.08) 5px 10px);background-color:var(--muted)}}.cost-bar.partial span{{color:var(--amber)}}.cost-empty{{display:grid;gap:6px;padding:14px;border:1px dashed var(--border);border-radius:9px;background:var(--surface-2)}}.cost-empty strong{{font-size:13px}}.cost-empty span{{font-size:12px;color:var(--muted)}}.cost-empty a{{font-size:12px;font-weight:700}}.unresolved-list{{display:grid;gap:8px;margin-top:8px}}.unresolved-row{{display:grid;grid-template-columns:1.4fr 1fr 1fr;gap:8px;padding:8px 10px;border:1px solid var(--border);border-left:3px solid var(--amber);border-radius:7px;background:var(--surface-2);font-size:12px}}.unresolved-row strong{{display:block;font-size:12px}}.unresolved-row span{{display:block;color:var(--muted);font-size:12px;margin-top:2px}}.cost-track i{{display:block;height:100%;border-radius:8px}}.cost-bar strong{{text-align:right}}.provenance{{display:grid;gap:7px;font-size:12px}}.provenance{{font-size:12px}}.provenance span,.provenance small{{color:var(--muted)}}a{{color:var(--accent)}}.ptu-list{{display:grid;gap:12px}}.ptu-card{{padding:14px}}.ptu-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:11px}}.ptu-recommendation{{color:var(--accent);font-size:14px}}.dimension-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:7px;margin-bottom:11px}}.dimension{{padding:8px;border:1px solid var(--border);border-left:4px solid var(--muted);border-radius:7px;background:var(--surface-2)}}.dimension.positive{{border-left-color:var(--green)}}.dimension.neutral{{border-left-color:var(--amber)}}.dimension.negative{{border-left-color:var(--pink)}}.dimension strong,.dimension span,.dimension small{{display:block}}.dimension strong{{font-size:12px}}.dimension span{{font-size:12px;color:var(--soft);margin-top:2px}}.dimension small{{font-size:12px;margin-top:2px}}.ptu-stats{{display:grid;grid-template-columns:repeat(8,1fr);gap:7px}}.ptu-stats span{{font-size:12px;color:var(--muted);text-transform:uppercase}}.ptu-stats strong{{display:block;color:var(--text);font-size:12px;text-transform:none;margin-top:3px}}.ptu-note{{font-size:12px;color:var(--muted);margin-top:10px}}.ptu-evidence-note{{font-size:12px;color:var(--amber);margin:10px 0;padding:10px 12px;border:1px dashed var(--border);border-radius:8px;background:var(--surface-2)}}.eligibility-badge{{font-weight:700;padding:1px 6px;border-radius:5px;background:var(--surface-2);color:var(--soft)}}.eligibility-badge.eligible_sufficient_evidence{{color:var(--green)}}.eligibility-badge.eligible_insufficient_evidence,.eligibility-badge.pricing_unavailable,.eligibility-badge.deployment_mode_unavailable{{color:var(--amber)}}.eligibility-badge.model_capacity_unavailable,.eligibility-badge.ptu_not_applicable,.eligibility-badge.collection_identity_error{{color:var(--pink)}}.ptu-graphs{{margin-top:12px}}.ptu-source{{display:flex;gap:8px;padding:12px;margin-top:12px;font-size:12px}}@media(max-width:960px){{.dimension-grid{{grid-template-columns:1fr 1fr}}.ptu-stats{{grid-template-columns:repeat(4,1fr)}}}}@media(max-width:620px){{.cost-bar{{grid-template-columns:80px 1fr}}.cost-bar strong{{grid-column:2}}.dimension-grid,.ptu-stats{{grid-template-columns:1fr 1fr}}.ptu-head{{display:block}}.ptu-recommendation{{display:block;margin-top:5px}}}}
.state{{display:inline-flex;align-items:center;gap:6px;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;border:1px solid currentColor;line-height:1.5}}.state-symbol{{font-weight:700}}.state-success{{color:var(--status-success-fg);background:var(--status-success-bg)}}.state-warning{{color:var(--status-warning-fg);background:var(--status-warning-bg)}}.state-danger{{color:var(--status-danger-fg);background:var(--status-danger-bg)}}.state-info{{color:var(--status-info-fg);background:var(--status-info-bg)}}.state-neutral{{color:var(--status-neutral-fg);background:var(--status-neutral-bg)}}.state-reason{{display:block;color:var(--muted);font-size:12px;margin-top:4px;max-width:78ch}}.state-banner{{display:flex;flex-wrap:wrap;align-items:center;gap:10px 14px;padding:11px 14px;margin-bottom:12px;border:1px solid var(--border);border-left-width:4px;border-radius:10px;background:var(--surface)}}.state-banner-warning{{border-left-color:var(--status-warning-fg)}}.state-banner-danger{{border-left-color:var(--status-danger-fg)}}.state-banner-success{{border-left-color:var(--status-success-fg)}}.state-banner-info{{border-left-color:var(--status-info-fg)}}.state-banner-neutral{{border-left-color:var(--status-neutral-fg)}}.banner-action{{margin-left:auto;font-weight:700;white-space:nowrap}}.view-toggle{{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:10px}}.view-toggle .label{{margin-right:4px}}.view-tab{{border:1px solid var(--border);background:var(--surface-2);color:var(--muted);border-radius:8px;padding:7px 12px;font:700 12px inherit;cursor:pointer;min-height:36px}}.view-tab[aria-pressed="true"]{{color:var(--text);background:var(--surface);border-color:var(--accent)}}.view-tab:focus-visible{{outline:3px solid var(--amber);outline-offset:2px}}.remediation,.how-pricing,.provenance-details{{background:var(--surface);border:1px solid var(--border);border-radius:9px;margin-bottom:12px;overflow:hidden}}.provenance-details{{margin:8px 0 0}}.provenance-headline{{font-size:13px;color:var(--soft)}}.remediation-list{{margin:0;padding-left:18px}}.remediation-list li{{padding:5px 0;border-bottom:1px solid var(--border)}}.remediation-list li:last-child{{border-bottom:0}}.remediation-list span{{display:block;color:var(--muted);font-size:12px}}.detail-list{{margin:0;padding-left:18px;color:var(--muted);font-size:12px}}.detail-list li{{padding:3px 0}}.row-detail td{{padding-top:0;border-bottom:1px solid var(--border)}}.row-detail summary{{padding:4px 0;font-size:12px;color:var(--muted)}}.cost-note{{margin-top:10px}}pre{{margin:6px 0 0;padding:8px 10px;background:var(--surface-2);border-radius:7px;overflow-x:auto}}pre code{{font:12px Consolas,monospace;color:var(--soft)}}@media(max-width:620px){{.state-banner{{display:block}}.banner-action{{display:inline-block;margin:8px 0 0}}.view-toggle{{overflow-x:auto}}}}@media print{{.state{{border-color:#46556b;color:#11213b;background:#fff}}.state-banner{{background:#fff;border-color:#9aa8ba}}.view-tab{{background:#fff;color:#11213b}}details{{display:block}}details > *{{display:block}}}}
.data-quality{{display:grid;gap:8px;padding:12px 14px;margin-bottom:12px;border:1px solid var(--border);border-left:5px solid var(--amber);border-radius:10px;background:var(--surface)}}.data-quality.blocking{{border-left-color:var(--pink)}}.dq-head{{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}}.dq-head strong{{font-size:13px}}.dq-head span{{color:var(--muted);font-size:12px}}.data-quality ul{{margin:0;padding-left:18px;display:grid;gap:4px}}.data-quality li{{font-size:12px;color:var(--soft)}}.data-quality li strong{{display:inline;font-size:12px}}.data-quality li span{{display:block;color:var(--muted);font-size:12px}}.data-quality li.dq-blocker strong::before{{content:"Blocker · ";color:var(--pink)}}.data-quality li.dq-warning strong::before{{content:"Limitation · ";color:var(--amber)}}.single-usage{{padding:12px 14px;display:grid;gap:8px}}.single-usage-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin:0}}.single-usage-grid div{{background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:8px 10px}}.single-usage-grid dt{{color:var(--muted);font-size:12px}}.single-usage-grid dd{{margin:2px 0 0;font-size:13px;font-weight:700}}.status-list{{margin:0;padding:10px 14px 12px 32px;display:grid;gap:6px}}.status-list li{{font-size:13px;color:var(--soft)}}.status-list li span{{display:block;color:var(--muted);font-size:12px}}.status-list li.empty{{list-style:none;margin-left:-18px;color:var(--muted)}}.actions li.not-evaluated strong{{color:var(--amber)}}.composition{{display:grid;gap:4px;margin-bottom:8px}}.not-evaluated-panel{{border-left:3px solid var(--muted)}}

{PTU_DASHBOARD_CSS}
</style></head><body><main>
<header><div><img class="logo" src="data:image/png;base64,{_logo_data_uri().split(',',1)[1]}" alt="TokenLens for Azure logo"></div>
<div class="meta">Generated<strong title="{_escape(report.generated_at)}">{_escape(_human_timestamp(report.generated_at))}</strong>
· Source<strong>{_escape(_source_summary(report))}</strong> · {_escape(_classification_label(report))}</div></header>
{banner}
<div class="tabs"><div class="tab-list" role="tablist" aria-label="Report views">
<button class="tab" id="overview-tab" role="tab" aria-selected="true" aria-controls="overview-panel" tabindex="0"><span class="tab-full">Overview</span><span class="tab-short" aria-hidden="true">Overview</span></button>
<button class="tab" id="cost-tab" role="tab" aria-selected="false" aria-controls="cost-panel" tabindex="-1" aria-label="Cost analysis"><span class="tab-full">Cost analysis</span><span class="tab-short" aria-hidden="true">Cost</span></button>
<button class="tab" id="workloads-tab" role="tab" aria-selected="false" aria-controls="workloads-panel" tabindex="-1" aria-label="Workload economics"><span class="tab-full">Workloads</span><span class="tab-short" aria-hidden="true">Workloads</span></button>
<button class="tab" id="usage-tab" role="tab" aria-selected="false" aria-controls="usage-panel" tabindex="-1" aria-label="Usage and diagnostics"><span class="tab-full">Usage &amp; diagnostics</span><span class="tab-short" aria-hidden="true">Usage</span></button>
<button class="tab" id="ptu-tab" role="tab" aria-selected="false" aria-controls="ptu-panel" tabindex="-1" aria-label="PTU advisor"><span class="tab-full">PTU advisor</span><span class="tab-short" aria-hidden="true">PTU</span></button></div>
<section class="tab-panel overview active" id="overview-panel" role="tabpanel" aria-labelledby="overview-tab">
<section class="metrics"><div class="card"><div class="label">Requests</div><div class="value">{_escape(_requests_value(summary))}</div><div class="sub">{_escape(_requests_sub(summary))}</div></div>
<div class="card"><div class="label">Total tokens</div><div class="value">{_escape(_total_tokens_value(summary))}</div><div class="sub">{_escape(_tokens_sub(summary))}</div></div>
<div class="card"><div class="label">Deployments</div><div class="value">{len(report.deployments):,} {_escape(deployment_word)}</div><div class="sub">{_escape(deployment_label)}</div></div>
<div class="card"><div class="label">Estimated cost</div><div class="value accent">{_money(summary.estimated_cost_usd, currency=summary.pricing_currency)}</div><div class="sub">{_escape(cost_sub_overview)}</div></div>
<div class="card"><div class="label">Pricing coverage</div><div class="value">{summary.pricing_coverage_tokens_percent:.1f}% priced</div><div class="sub">{_escape(pricing_headline)} · {_escape(pricing_reason)}</div></div></section>
<section class="overview-grid"><section class="panel"><div class="panel-head"><div><h2>Portfolio usage</h2><small>Token share by deployment</small></div><a href="#usage" data-tab-link="usage">View analytics →</a></div><div class="portfolio">{_deployment_rows(report)}</div></section>
<aside class="panel"><div class="panel-head"><div><h2>Azure actions</h2><small>Prioritized for a first decision</small></div></div><div class="actions"><ol>{_overview_actions(actions, report)}</ol></div></aside></section>
<section class="panel finding-panel"><div class="panel-head"><div><h2>Material findings</h2><small>Only diagnostics that ran against supporting evidence appear here, at or above {float(config.get("overview_min_impact_percent", 1.0)):.1f}% impact or {int(config.get("overview_min_impact_tokens", 100000)):,} tokens. Impact share and evidence strength are stated separately, and rules that could not be evaluated are excluded.</small></div></div><div class="finding-list">{_finding_cards(overview, compact=True, empty=_no_findings_message(report), denominator=f"{summary.input_tokens:,} input tokens")}</div>
{f'<p class="empty">{hidden_count:,} additional evaluated opportunities are available in Usage analytics.</p>' if hidden_count else ""}
{f'<p class="empty">{report.diagnostics.not_evaluated:,} diagnostic(s) were not evaluated for lack of evidence and are listed under Usage &rarr; Not evaluated.</p>' if report.diagnostics.not_evaluated else ""}</section>
<footer>tokenlens-for-azure · created by Tzahi Ariel</footer></section>
<section class="tab-panel usage" id="usage-panel" role="tabpanel" aria-labelledby="usage-tab">
<p class="analytics-intro">Usage analytics retains every finding. Percentages are relative to the relevant deployment or portfolio, and absolute token or call volume is shown alongside them. Low materiality does not mean invalid; it means lower priority for the first executive decision.</p>
<section class="chart-grid">{model_chart}
<article class="chart-card"><h2>Token usage by deployment</h2><p>Input, output, and cached tokens are stacked; deployments remain separate. Cached input is omitted, never drawn as zero, when the metric is unavailable.</p>{column_svg}<div class="legend"><span class="legend-row"><span class="swatch" style="background:#73c7ff"></span>Input (uncached)</span><span class="legend-row"><span class="swatch" style="background:#57d68b"></span>Output</span><span class="legend-row"><span class="swatch" style="background:#ffc857"></span>Cached input</span></div>{column_table}</article></section>
<section class="panel table-panel"><h2>Model summary</h2><div class="table-scroll">{_model_table(report)}</div></section>
{_diagnostic_sections(report)}
<section class="analytics-section"><div class="panel-head"><div><h2>Deployment details</h2><small>Expand a deployment for its evaluated findings, metric coverage, and bucket counts.</small></div></div><div class="details-list">{_analytics_deployment_sections(report)}</div></section>
<footer>tokenlens-for-azure · created by Tzahi Ariel</footer></section>
<section class="tab-panel cost" id="cost-panel" role="tabpanel" aria-labelledby="cost-tab">{cost_panel}</section>
<section class="tab-panel workloads" id="workloads-panel" role="tabpanel" aria-labelledby="workloads-tab">{workloads_panel}</section>
<section class="tab-panel ptu" id="ptu-panel" role="tabpanel" aria-labelledby="ptu-tab">{ptu_panel}</section>
</div></main>
<script>
document.documentElement.classList.add("js");
/* One router owns location.hash for the whole report: #tab=<name> plus an
   optional &deployment=<slug> for the PTU tab. The PTU panel reads and writes
   its parameter through this router only, so the URL, the visible panel, and
   aria-selected can never disagree. */
window.tokenlensRouter = (function() {{
  const names = ["overview", "cost", "workloads", "usage", "ptu"];
  const listeners = [];
  function parse() {{
    const raw = (location.hash || "").replace(/^#/, "");
    const state = {{tab: "overview", deployment: null}};
    if (!raw) return state;
    if (raw.indexOf("=") === -1) {{
      /* Legacy #cost / #usage / #ptu links stay valid. */
      state.tab = names.indexOf(raw.toLowerCase()) >= 0 ? raw.toLowerCase() : "overview";
      return state;
    }}
    raw.split("&").forEach(function(part) {{
      const pair = part.split("=");
      const key = decodeURIComponent(pair[0] || "").toLowerCase();
      const value = decodeURIComponent(pair.slice(1).join("=") || "");
      if (key === "tab" && names.indexOf(value.toLowerCase()) >= 0) state.tab = value.toLowerCase();
      else if (key === "deployment" && value) state.deployment = value;
      else if (key === "ptu" && value) {{ state.tab = "ptu"; state.deployment = value; }}
    }});
    return state;
  }}
  let current = parse();
  function serialize(state) {{
    let hash = "#tab=" + state.tab;
    if (state.tab === "ptu" && state.deployment) hash += "&deployment=" + encodeURIComponent(state.deployment);
    return hash;
  }}
  function write(state, push) {{
    current = {{tab: state.tab, deployment: state.deployment}};
    const hash = serialize(current);
    if (history.replaceState && location.hash !== hash) {{
      if (push) history.pushState(null, "", hash); else history.replaceState(null, "", hash);
    }}
  }}
  function notify(origin) {{ listeners.forEach(function(fn) {{ fn(current, origin); }}); }}
  window.addEventListener("hashchange", function() {{ current = parse(); notify("hash"); }});
  window.addEventListener("popstate", function() {{ current = parse(); notify("hash"); }});
  return {{
    state: function() {{ return current; }},
    setTab: function(tab, options) {{
      write({{tab: tab, deployment: current.deployment}}, (options || {{}}).push);
      notify("tab");
    }},
    setDeployment: function(slug) {{
      write({{tab: current.tab, deployment: slug}}, false);
    }},
    subscribe: function(fn) {{ listeners.push(fn); }}
  }};
}})();
(function() {{
  const tabs = Array.from(document.querySelectorAll('.tab-list [role="tab"]'));
  const panels = Array.from(document.querySelectorAll('[role="tabpanel"]'));
  const names = ["overview", "cost", "workloads", "usage", "ptu"];
  function render(name, moveFocus) {{
    const index = Math.max(0, names.indexOf(name));
    const selected = tabs[index];
    tabs.forEach(tab => {{ const active = tab === selected; tab.setAttribute("aria-selected", active); tab.tabIndex = active ? 0 : -1; }});
    panels.forEach(panel => panel.classList.toggle("active", panel.id === selected.getAttribute("aria-controls")));
    if (moveFocus) selected.focus();
  }}
  function activate(name, moveFocus) {{
    window.tokenlensRouter.setTab(name, {{push: true}});
    render(name, moveFocus);
  }}
  tabs.forEach((tab, index) => tab.addEventListener("click", () => activate(names[index], false)));
  tabs.forEach((tab, index) => tab.addEventListener("keydown", event => {{
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {{ event.preventDefault(); activate(names[(index + (event.key === "ArrowRight" ? 1 : names.length - 1)) % names.length], true); }}
    if (event.key === "Home" || event.key === "End") {{ event.preventDefault(); activate(event.key === "Home" ? "overview" : "ptu", true); }}
  }}));
  document.querySelectorAll("[data-tab-link]").forEach(link => link.addEventListener("click", event => {{ event.preventDefault(); activate(link.dataset.tabLink, false); }}));
  window.tokenlensRouter.subscribe(function(state, origin) {{ if (origin === "hash") render(state.tab, false); }});
  render(window.tokenlensRouter.state().tab, false);
}})();
(function() {{
  /* Cost view and workload scope toggles. Both degrade to all panels visible
     when scripting is unavailable, so print and no-JS readers lose nothing. */
  function bind(buttonAttr, panelAttr) {{
    const buttons = Array.from(document.querySelectorAll("[" + buttonAttr + "]"));
    if (!buttons.length) return;
    const panels = Array.from(document.querySelectorAll("[" + panelAttr + "]"));
    buttons.forEach(button => button.addEventListener("click", function() {{
      const value = button.getAttribute(buttonAttr);
      buttons.forEach(other => other.setAttribute("aria-pressed", other === button ? "true" : "false"));
      panels.forEach(panel => {{ panel.hidden = panel.getAttribute(panelAttr) !== value; }});
    }}));
  }}
  bind("data-cost-view", "data-cost-panel");
  bind("data-workload-scope", "data-workload-panel");
  document.querySelectorAll("[data-open-remediation]").forEach(link => link.addEventListener("click", function(event) {{
    const target = document.getElementById("resolve-pricing");
    if (!target) return;
    event.preventDefault();
    target.open = true;
    target.scrollIntoView({{block: "nearest"}});
  }}));
}})();
{PTU_DASHBOARD_JS}
</script></body></html>"""


def _model_table(report: AnalysisReport) -> str:
    rows = []
    for item in model_rollups(report):
        requests = f"{item.requests:,}" if item.requests is not None else "Unavailable"
        cached = f"{item.cached_tokens:,}" if item.cached_tokens_available else "Unavailable"
        status, reason = _PRICING_STATUS_COPY.get(item.pricing_status, ("Unresolved", "No exact model/mode price"))
        rows.append(
            f"<tr><th scope=\"row\">{_escape(_model_label(item))}<small>{_escape(item.deployment_mode.title())} · {_escape(requests)} requests</small></th>"
            f"<td>{item.total_tokens:,}<small>{item.input_tokens:,} / {_escape(cached)} / {item.output_tokens:,}</small></td>"
            f"<td>{item.token_share_percent:.1f}%</td>"
            f"<td>{item.active_buckets:,}<small>of {item.elapsed_buckets:,} elapsed</small></td>"
            f"<td>{_rate(item.input_price_per_million, currency=item.pricing_currency)} / {_rate(item.cached_input_price_per_million, currency=item.pricing_currency)} / {_rate(item.output_price_per_million, currency=item.pricing_currency)}</td>"
            f"<td>{_money(item.estimated_cost_usd, currency=item.pricing_currency, precision=2)}<small>{_escape(status)}: {_escape(reason)}</small></td></tr>"
        )
    return f"""<table class="data-table model-summary"><caption>Model summary · token mix below total is input / cached / output · rates are per 1M tokens · requests come from the request metric, never a bucket count</caption><thead><tr><th scope="col">Model and version</th><th scope="col">Tokens</th><th scope="col">Share</th><th scope="col">Active buckets</th><th scope="col">Price I / C / O</th><th scope="col">Est. cost</th></tr></thead><tbody>{''.join(rows) or '<tr><td colspan="6">No model usage observed.</td></tr>'}</tbody></table>"""


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
        overview_requests = (
            f"{overview_summary.requests_observed:,}"
            if overview_summary.requests_available and overview_summary.requests_observed is not None
            else "Unavailable"
        )
        overview_content = f"""<section class="metrics"><div class="kpi"><span>Requests</span><b>{_escape(overview_requests)}</b><p>{overview_summary.total_tokens:,} total tokens</p></div>
<div class="kpi"><span>Deployments</span><b>{len(overview_report.deployments):,}</b><p>Consumption routes</p></div>
<div class="kpi"><span>Optimisation scenarios</span><b>{len(overview_report.report_metadata.get("scenarios", [])):,}</b><p>Independent ranges · not additive</p></div>
<div class="kpi"><span>Findings</span><b>{len(overview_report.findings):,}</b><p>Usage diagnostics</p></div></section>
<section class="grid"><article class="panel overview-list"><h2>Portfolio usage</h2><p>Token share by deployment</p>{_deployment_rows(overview_report)}</article>
<article class="panel overview-list"><h2>Azure actions</h2><p>Prioritized for a first decision</p><ol>{_overview_actions(top_recommendations(overview_findings(overview_report)), overview_report)}</ol></article></section>
<section class="panel overview-list"><h2>Material findings</h2><p>Independent scenarios are retained; no aggregate range is summed.</p>{_finding_cards(overview_findings(overview_report), compact=True)}</section>"""
    else:
        overview_content = '<div class="panel fallback"><h2>Consumption Overview</h2><p>Legacy request/deployment overview remains available when request telemetry is supplied.</p></div>'
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TokenLens for Azure — Task economics</title>
<style>
:root{{--bg:#11213b;--surface:#172a49;--surface2:#1d3559;--border:#35527a;--text:#f3f7ff;--muted:#b5c5dc;--accent:#73c7ff;--green:#57d68b;--amber:#ffc857}}
*{{box-sizing:border-box}}html,body{{min-height:100%}}body{{margin:0;min-height:100vh;background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI",Arial,sans-serif}}main{{width:100%;min-height:100vh;margin:0;padding:clamp(16px,1.5vw,28px)}}header{{display:flex;justify-content:space-between;align-items:center;gap:18px;border-bottom:1px solid var(--border);padding-bottom:14px;margin-bottom:14px}}.logo{{width:min(300px,58vw);height:auto;display:block}}h1{{font-size:22px;margin:0}}h2{{font-size:17px;margin:0}}h3{{font-size:13px;margin:0}}p{{margin:0;color:var(--muted)}}.meta{{color:var(--muted);font-size:12px;text-align:right}}.tabs{{display:flex;gap:5px;border-bottom:1px solid var(--border);margin-bottom:14px}}button{{font:700 12px inherit;color:var(--muted);background:transparent;border:1px solid transparent;padding:9px 14px;border-radius:8px 8px 0 0;cursor:pointer}}button[aria-selected=true]{{color:var(--text);background:var(--surface);border-color:var(--border)}}button:focus-visible,svg circle:focus-visible{{outline:3px solid var(--amber);outline-offset:3px}}.panel,.kpi,.chart{{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:12px}}.kpi b{{display:block;font-size:22px;color:var(--accent);margin-top:4px}}.kpi span,.label{{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}}.chart-head{{display:flex;justify-content:space-between;gap:8px;margin-bottom:10px}}.bar-row{{display:grid;grid-template-columns:150px 1fr 100px;align-items:center;gap:8px;margin:9px 0;font-size:12px}}.bar-track{{height:12px;background:var(--surface2);border-radius:8px;overflow:hidden}}.bar-track i{{display:block;height:100%;background:var(--accent);border-radius:8px}}.overview-list{{padding:14px}}.overview-list h2{{margin-bottom:3px}}.overview-list > p{{margin-bottom:10px}}.overview-list .portfolio-row{{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:7px 0;border-bottom:1px solid var(--border);font-size:12px}}.overview-list .portfolio-row small{{display:block;color:var(--muted)}}.overview-list .portfolio-bar{{height:8px;background:var(--surface2);border-radius:8px;overflow:hidden}}.overview-list .portfolio-bar span{{display:block;height:100%;background:var(--accent)}}.overview-list .portfolio-value{{text-align:right}}.overview-list ol{{margin:0;padding-left:20px}}.overview-list li{{padding:5px 0;color:var(--muted)}}svg{{width:100%;height:auto;background:var(--surface2);border-radius:8px}}.axis{{stroke:var(--border);stroke-width:1}}svg text{{fill:var(--muted);font-size:11px}}svg circle{{fill:var(--green);stroke:var(--text);stroke-width:1}}table{{width:100%;border-collapse:collapse;font-size:13px;display:block;overflow-x:auto}}caption{{text-align:left;color:var(--muted);padding:0 0 6px}}th,td{{padding:7px;border-bottom:1px solid var(--border);white-space:nowrap;text-align:right}}th:first-child,td:first-child{{text-align:left}}thead th{{color:var(--muted);font-size:11px;text-transform:uppercase}}.table-panel{{margin-bottom:12px}}.scenario-list{{margin:0;padding-left:20px}}.scenario-list li{{padding:6px 0;border-bottom:1px solid var(--border)}}.scenario-list small{{display:block;color:var(--muted)}}.fallback{{padding:24px;text-align:center}}@media(max-width:900px){{.metrics,.grid{{grid-template-columns:1fr 1fr}}.bar-row{{grid-template-columns:110px 1fr 90px}}}}@media(max-width:600px){{main{{padding:14px}}header{{display:block}}.meta{{text-align:left;margin-top:7px}}.metrics,.grid{{grid-template-columns:1fr}}}}@media print{{body{{background:#fff;color:#11213b}}.panel,.kpi,.chart{{background:#fff;border-color:#9aa8ba}}.tabs{{display:none}}}}
</style></head><body><main>
<header><div><img class="logo" src="{_logo_data_uri()}" alt="TokenLens for Azure logo"></div>
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
