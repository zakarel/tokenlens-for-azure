#!/usr/bin/env python3
"""Generate the checked-in demo from the production analyzer and renderer."""

from __future__ import annotations

from pathlib import Path

from tokenlens.analyzer import analyze
from tokenlens.ingest import load_records_many
from tokenlens.reports import report_html


ROOT = Path(__file__).resolve().parents[1]
fixture = ROOT / "examples" / "multi-deployment-portfolio.jsonl"
records, _ = load_records_many([str(fixture)])
report = analyze(records, "examples/multi-deployment-portfolio.jsonl", generated_at="2026-09-03T12:10:00+00:00")
(ROOT / "docs" / "tokenlens-report-demo.html").write_text(report_html(report), encoding="utf-8")
print(f"Generated {ROOT / 'docs' / 'tokenlens-report-demo.html'}")
