from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .analyzer import analyze
from .ingest import InputError, load_records
from .reports import report_html, report_json, report_sarif, write_output

app = typer.Typer(help="Offline LLM token-efficiency diagnostics with Azure-first guidance.")


def _load_config(path: str | None) -> dict:
    if not path:
        candidate = Path(".tokenlens.yml")
        if not candidate.is_file():
            return {}
        path = str(candidate)
    import yaml

    with open(path, encoding="utf-8") as source:
        return yaml.safe_load(source) or {}


def _render(report, output_format: str) -> str:
    if output_format == "json":
        return report_json(report)
    if output_format == "sarif":
        return report_sarif(report)
    if output_format == "html":
        return report_html(report)
    summary = report.summary
    lines = [
        "TokenLens for Azure",
        "─" * 68,
        f"Analyzed {summary.requests_analyzed:,} requests · {summary.input_tokens:,} input tokens · {summary.output_tokens:,} output tokens",
        "",
    ]
    for finding in report.findings:
        estimate = finding.estimated_savings
        if estimate.unit == "calls":
            impact = f"{estimate.min_tokens:,} calls"
        elif estimate.min_tokens is None:
            impact = "No estimate"
        else:
            impact = f"{estimate.min_tokens:,}–{estimate.max_tokens:,} tokens"
        lines.extend(
            [
                f"{finding.severity.upper():<6} {finding.rule_id}  {finding.title}",
                f"       {finding.detail}",
                f"       Impact: {impact} · Confidence: {finding.confidence}",
                f"       Azure action: {finding.azure_recommendation.action}",
                "",
            ]
        )
    lines.append(
        f"{len(report.rules)} rules processed · {summary.findings} findings · {summary.not_evaluated} not evaluated · advisory result"
    )
    return "\n".join(lines) + "\n"


@app.command("analyze")
def analyze_command(
    input_path: str = typer.Argument(..., metavar="INPUT", help="JSONL path or - for stdin."),
    output_format: str = typer.Option("text", "--format", case_sensitive=False, help="text, json, sarif, or html."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write the report to a file."),
    config: Optional[str] = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
) -> None:
    """Analyze an OpenAI-compatible JSONL trace."""
    try:
        _load_config(config)
        records, source = load_records(input_path)
        report = analyze(records, source)
        if output_format not in {"text", "json", "sarif", "html"}:
            raise typer.BadParameter("must be text, json, sarif, or html")
        write_output(_render(report, output_format), output)
    except InputError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def compare(
    baseline: str = typer.Argument(..., help="Baseline JSONL path."),
    candidate: str = typer.Argument(..., help="Candidate JSONL path."),
    output_format: str = typer.Option("text", "--format", case_sensitive=False, help="text, json, sarif, or html."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write the candidate report to a file."),
    fail_on_regression: Optional[float] = typer.Option(None, "--fail-on-regression", help="Fail when addressable percentage increases by this amount."),
) -> None:
    """Compare candidate traces against a baseline."""
    try:
        baseline_records, baseline_source = load_records(baseline)
        candidate_records, candidate_source = load_records(candidate)
        baseline_report = analyze(baseline_records, baseline_source)
        candidate_report = analyze(candidate_records, candidate_source)
    except InputError as exc:
        raise typer.BadParameter(str(exc)) from exc
    delta = candidate_report.summary.addressable_max_percent - baseline_report.summary.addressable_max_percent
    text = _render(candidate_report, output_format)
    text += f"\nBaseline addressable range: {baseline_report.summary.addressable_max_percent:.1f}%\n"
    text += f"Candidate addressable range: {candidate_report.summary.addressable_max_percent:.1f}%\n"
    text += f"Change: {delta:+.1f} percentage points\n"
    write_output(text, output)
    if fail_on_regression is not None and delta > fail_on_regression:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
