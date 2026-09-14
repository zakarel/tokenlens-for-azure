from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import webbrowser
from importlib.resources import files
from pathlib import Path

import typer

from .analyzer import analyze
from .ingest import InputError, load_records_many
from .output import choose_output
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
        f"Analyzed {summary.requests_analyzed:,} requests · {summary.total_tokens:,} total tokens · {len(report.deployments):,} deployments",
        "",
    ]
    for deployment in report.deployments:
        item = deployment.summary
        lines.append(
            f"Deployment: {item.deployment_name} · model type: {item.model_name} · "
            f"{item.requests_analyzed:,} requests · {item.total_tokens:,} tokens"
        )
    if report.deployments:
        lines.append("")
    for finding in report.findings:
        estimate = finding.estimated_savings
        if estimate.unit == "calls":
            impact = f"{estimate.min_tokens or 0:,} calls"
        elif estimate.min_tokens is None:
            impact = "Opportunity"
        else:
            impact = f"{estimate.min_tokens:,}–{estimate.max_tokens or estimate.min_tokens:,} tokens"
        if finding.impact_min_percent is None:
            impact_percent = "Opportunity"
        elif finding.impact_min_percent == finding.impact_max_percent:
            impact_percent = f"{finding.impact_min_percent:.1f}%"
        else:
            impact_percent = f"{finding.impact_min_percent:.1f}–{finding.impact_max_percent:.1f}%"
        lines.extend(
            [
                f"{finding.severity.upper():<6} {finding.rule_id}  {finding.title}",
                f"       {finding.detail}",
                f"       Impact: {impact_percent} ({impact})",
                f"       Azure action: {finding.azure_recommendation.action}",
                "",
            ]
        )
    lines.append(f"{len(report.rules)} rules processed · {summary.findings} findings · advisory result")
    return "\n".join(lines) + "\n"


def _open_report(path: Path) -> bool:
    try:
        return bool(webbrowser.open(path.resolve().as_uri()))
    except (OSError, webbrowser.Error):
        return False


def _validate_format(output_format: str) -> None:
    if output_format not in {"text", "json", "sarif", "html"}:
        raise typer.BadParameter("must be text, json, sarif, or html")


def _write_report(content: str, output: str | None, *, output_format: str, output_dir: str | None, comparison: bool, open_report: bool, quiet: bool, report) -> Path | None:
    if open_report and output_format != "html":
        raise typer.BadParameter("--open requires --format html")
    destination = choose_output(output, report_format=output_format, output_dir=output_dir or ".", comparison=comparison)
    write_output(content, str(destination) if destination else None)
    if destination:
        destination = destination.resolve()
        if not quiet:
            typer.echo(f"report-path={destination}")
            typer.echo(
                f"traces={report.summary.requests_analyzed} deployments={len(report.deployments)}"
            )
        if open_report:
            opened = _open_report(destination)
            if not quiet:
                typer.echo(f"browser-open={'succeeded' if opened else 'failed'}")
    return destination


@app.command("analyze")
def analyze_command(
    input_paths: list[str] = typer.Argument(..., metavar="INPUT", help="JSONL path, directory, glob, or - for stdin."),
    output_format: str = typer.Option("text", "--format", case_sensitive=False, help="text, json, sarif, or html."),
    output: str | None = typer.Option(None, "--output", "-o", help="Write to this exact file."),
    output_dir: str | None = typer.Option(None, "--output-dir", help="Directory for generated timestamped reports."),
    config: str | None = typer.Option(None, "--config", help="Path to .tokenlens.yml."),
    open_report: bool = typer.Option(False, "--open", help="Open a generated HTML report."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Analyze one or more OpenAI-compatible JSONL traces."""
    _validate_format(output_format)
    try:
        _load_config(config)
        records, source = load_records_many(input_paths)
        report = analyze(records, source)
        _write_report(
            _render(report, output_format),
            output,
            output_format=output_format,
            output_dir=output_dir,
            comparison=False,
            open_report=open_report,
            quiet=quiet,
            report=report,
        )
    except InputError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command()
def compare(
    baseline: str = typer.Argument(..., help="Baseline JSONL path."),
    candidate: str = typer.Argument(..., help="Candidate JSONL path."),
    output_format: str = typer.Option("text", "--format", case_sensitive=False, help="text, json, sarif, or html."),
    output: str | None = typer.Option(None, "--output", "-o", help="Write to this exact file."),
    output_dir: str | None = typer.Option(None, "--output-dir", help="Directory for generated timestamped reports."),
    fail_on_regression: float | None = typer.Option(None, "--fail-on-regression", help="Fail when addressable percentage increases by this amount."),
    open_report: bool = typer.Option(False, "--open", help="Open a generated HTML report."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress status output."),
) -> None:
    """Compare candidate traces against a baseline."""
    _validate_format(output_format)
    try:
        baseline_records, baseline_source = load_records_many([baseline])
        candidate_records, candidate_source = load_records_many([candidate])
        baseline_report = analyze(baseline_records, baseline_source)
        candidate_report = analyze(candidate_records, candidate_source)
    except InputError as exc:
        raise typer.BadParameter(str(exc)) from exc
    delta = candidate_report.summary.addressable_max_percent - baseline_report.summary.addressable_max_percent
    text = _render(candidate_report, output_format)
    if output_format != "html":
        text += (
            f"\nBaseline addressable range: {baseline_report.summary.addressable_max_percent:.1f}%\n"
            f"Candidate addressable range: {candidate_report.summary.addressable_max_percent:.1f}%\n"
            f"Change: {delta:+.1f} percentage points\n"
        )
    _write_report(
        text,
        output,
        output_format=output_format,
        output_dir=output_dir,
        comparison=True,
        open_report=open_report,
        quiet=quiet,
        report=candidate_report,
    )
    if fail_on_regression is not None and delta > fail_on_regression:
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Check local Python, permissions, optional Foundry packages, and Entra configuration."""
    typer.echo(f"python={sys.version.split()[0]}")
    typer.echo(f"python-supported={'yes' if sys.version_info >= (3, 11) else 'no'}")
    typer.echo(f"cwd-writable={'yes' if os.access('.', os.W_OK) else 'no'}")
    for package in ("openai", "azure.identity"):
        typer.echo(f"{package}={'installed' if importlib.util.find_spec(package) else 'missing (optional)'}")
    env_present = any(os.getenv(name) for name in ("AZURE_OPENAI_ENDPOINT", "FOUNDRY_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT"))
    typer.echo(f"foundry-endpoint={'configured' if env_present else 'not configured'}")
    if importlib.util.find_spec("azure.identity"):
        try:
            from azure.identity import DefaultAzureCredential

            DefaultAzureCredential(exclude_interactive_browser_credential=True)
            typer.echo("entra-credential=available via DefaultAzureCredential chain")
        except Exception:
            typer.echo("entra-credential=not available; run az login and verify your role")
    else:
        typer.echo("entra-credential=install tokenlens-azure[foundry]")


@app.command("init-foundry")
def init_foundry(
    directory: str = typer.Option("foundry-traces", "--directory", help="Private directory for captured JSONL traces."),
) -> None:
    """Create a private trace directory and a copyable Entra-authenticated capture example."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    (target / ".gitignore").write_text("*.jsonl\n", encoding="utf-8")
    source = Path(__file__).resolve().parents[2] / "examples" / "capture_foundry.py"
    destination = target / "capture_foundry.py"
    if not source.is_file():
        packaged = files("tokenlens").joinpath("assets/capture_foundry.py")
        destination_content = packaged.read_text(encoding="utf-8")
        if not destination.exists():
            destination.write_text(destination_content, encoding="utf-8")
    elif not destination.exists():
        shutil.copyfile(source, destination)
    typer.echo(f"trace-directory={target.resolve()}")
    typer.echo(f"capture-example={destination.resolve()}")
    typer.echo("Run the capture example only after supplying an explicit deployment and prompt.")


if __name__ == "__main__":
    app()
