#!/usr/bin/env python3
"""Generate the checked-in demo from the production analyzer and renderer."""

from __future__ import annotations

import json
from pathlib import Path

from tokenlens.analyzer import analyze
from tokenlens.ingest import load_records_many
from tokenlens.reports import report_html


ROOT = Path(__file__).resolve().parents[1]
fixture = ROOT / "examples" / "multi-deployment-portfolio.jsonl"

DEMO_DEPLOYMENTS = (
    ("reasoning-prod", "Claude-opus-5", 3220, 640, 112, "reasoning"),
    ("general-prod", "gpt-5.6-luna", 2160, 420, 72, "general"),
    ("reasoning-batch", "claude-opus-5", 1680, 520, 88, "batch"),
    ("creative-prod", "claude-fable-5.1", 488, 300, 180, "creative"),
)


def write_fixture() -> None:
    """Create deterministic, clearly synthetic records without storing customer text."""
    with fixture.open("w", encoding="utf-8") as output:
        sequence = 0
        for deployment, model, count, input_tokens, output_tokens, workload in DEMO_DEPLOYMENTS:
            for index in range(count):
                sequence += 1
                messages = [{"role": "user", "content": f"Process synthetic {workload} task {index % 97:02d}."}]
                if sequence <= 6000:
                    messages.insert(0, {"role": "system", "content": "Synthetic task."})
                payload = {
                    "timestamp": f"2026-09-03T12:{sequence // 60:02d}:{sequence % 60:02d}Z",
                    "request_id": f"demo-{sequence:05d}",
                    "deployment_name": deployment,
                    "model_name": model,
                    "model": deployment,
                    "provider": "azure_foundry",
                    "messages": messages,
                    "tools": [],
                    "max_output_tokens": 2048 if workload == "creative" else 4096,
                    "usage": {
                        "input_tokens": input_tokens + (index % 7),
                        "output_tokens": output_tokens + (index % 5),
                        "cached_tokens": (input_tokens // 4) if index % 3 == 0 else 0,
                    },
                    "metadata": {"tenant": "tenant-demo-001", "workload": f"workload-{workload}"},
                }
                output.write(json.dumps(payload, separators=(",", ":")) + "\n")


write_fixture()
records, _ = load_records_many([str(fixture)])
report = analyze(
    records,
    "examples/multi-deployment-portfolio.jsonl",
    generated_at="2026-09-03T12:10:00+00:00",
    report_config={
        "overview_min_impact_percent": 1.0,
        "overview_min_impact_tokens": 100000,
        "overview_max_findings": 3,
    },
)
(ROOT / "docs" / "tokenlens-report-demo.html").write_text(report_html(report), encoding="utf-8")
print(f"Generated {ROOT / 'docs' / 'tokenlens-report-demo.html'} from {len(records):,} synthetic requests")
