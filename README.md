<div align="center">

<img src="docs/assets/tokenlens-logo-dark.png" alt="TokenLens for Azure logo" width="720">

# TokenLens for Azure

### Lint LLM traces for avoidable token usage

TokenLens is an offline Python CLI and GitHub Action that analyzes OpenAI-compatible JSONL traces, identifies token waste, and maps findings to practical Azure optimizations.

[![Status](https://img.shields.io/badge/status-pre--alpha-7A5AF8?style=flat-square)](#what-is-tokenlens)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](#prerequisites)
[![Offline](https://img.shields.io/badge/analysis-100%25_offline-107C10?style=flat-square)](#privacy-and-security)
[![Azure-first](https://img.shields.io/badge/Azure-first-0078D4?style=flat-square&logo=microsoftazure&logoColor=white)](#azure-first-portable-core)

**No API key · No Azure account · No network calls · No prompts leaving your machine**

</div>

> [!IMPORTANT]
> TokenLens for Azure is currently **pre-alpha**. The project is usable as an MVP, but command interfaces, rule thresholds, and package names may change before v0.1.

## What is TokenLens?

TokenLens is a developer tool for understanding the hidden cost of LLM applications. It works like a linter for token usage: point it at representative request traces and it reports where context, retries, tools, outputs, caching, or model selection may be wasting tokens.

It is **Azure-first but provider-portable**. The detection engine understands OpenAI-compatible traces, while a separate recommendation layer maps findings to Azure OpenAI, Azure API Management, Microsoft Foundry, Azure AI Search, Azure Monitor, and Application Insights.

## What does it do?

TokenLens:

- analyzes JSONL locally without calling an LLM;
- evaluates eight token-efficiency diagnostics;
- reports evidence, estimated savings ranges, and impact percentages;
- distinguishes measured findings from heuristic opportunities;
- omits rules when the trace data cannot support a finding;
- recommends concrete Azure actions;
- estimates analyzed cost with exact model and deployment-mode pricing matches;
- evaluates PTU suitability, sizing, break-even, and hybrid spillover economics;
- emits terminal, JSON, SARIF, and self-contained HTML reports;
- compares a candidate trace set with a baseline;
- runs advisory by default, with opt-in CI regression thresholds.

The tool does not rewrite prompts, proxy production traffic, or claim guaranteed financial savings.

## How does it work?

```text
OpenAI-compatible JSONL
          │
          ▼
Normalize and redact
          │
          ▼
Run diagnostics, pricing, and PTU analysis
          │
          ▼
Estimate impact percentages
          │
          ▼
Map findings to Azure actions
          │
          ▼
Text · JSON · SARIF · HTML
```

Every finding follows the same explainable contract:

```json
{
  "rule_id": "TL001",
  "severity": "high",
  "title": "Repeated system prefix",
  "evidence": {
    "affected_requests": 8103,
    "repeated_tokens": 9400000
  },
  "estimated_savings": {
    "min_tokens": 6900000,
    "max_tokens": 9400000
  },
  "impact_min_percent": 18.1,
  "impact_max_percent": 24.7,
  "azure_recommendation": {
    "service": "Azure OpenAI",
    "capability": "Prompt caching"
  }
}
```

Optimisation scenarios are reported independently because caching, trajectory,
routing, reliability, and cleanup levers can overlap. TokenLens never adds
independent estimates into a misleading aggregate (for example, `100%–100%`).

## Example output

**Overview**

<a href="docs/assets/tokenlens-report-overview-20260914-v6.png">
  <img src="docs/assets/tokenlens-report-overview-20260914-v6.png" alt="TokenLens for Azure synthetic report Overview showing consumption, estimated analyzed cost, and pricing coverage">
</a>

**Cost analysis**

<a href="docs/assets/tokenlens-report-cost-analysis-20260914-v6.png">
  <img src="docs/assets/tokenlens-report-cost-analysis-20260914-v6.png" alt="TokenLens for Azure synthetic Cost analysis report showing model prices, cost composition, and deployment cost">
</a>

**Usage &amp; diagnostics**

<a href="docs/assets/tokenlens-report-usage-20260914-v6.png">
  <img src="docs/assets/tokenlens-report-usage-20260914-v6.png" alt="TokenLens for Azure synthetic Usage and diagnostics report showing multicolor model share, deployment usage, and the complete model pricing summary">
</a>

**PTU advisor**

<a href="docs/assets/tokenlens-report-ptu-advisor-20260914-v6.png">
  <img src="docs/assets/tokenlens-report-ptu-advisor-20260914-v6.png" alt="TokenLens for Azure synthetic PTU advisor report showing workload dimensions, sizing, and PAYG versus hybrid economics">
</a>

<p align="center"><em>Deterministic synthetic data · Click any image to open the enlarged PNG.</em></p>

The report has four self-contained views:

- **Overview** combines consumption, estimated analyzed cost, pricing coverage, and priority actions.
- **Cost analysis** shows exact model/mode prices, cost composition, unresolved pricing, and model/deployment spend.
- **Usage &amp; diagnostics** keeps the corrected multicolor model mix, visible model summary, deployment usage, and all findings.
- **PTU advisor** evaluates workload fit, capacity, sizing, break-even, and PTU plus PAYG spillover per deployment.

Charts are inline SVG with adjacent accessible tables, keyboard tabs, URL fragments,
mobile layout, and print styles. The checked-in demo is generated by
`scripts/generate-demo-report.py` from fictional task types and models.

The checked-in example is synthetic and contains exactly **7,548 requests** across four deployments using
`MAI-Thinking-1`, `Phi-4-mini`, `gpt-4.1`, and `MAI-Cyber-1 Flash`.
Overview materiality uses both percentage and absolute token impact; Usage &
diagnostics retains lower-impact findings rather than deleting them.

## Prerequisites

Before installing TokenLens, make sure you have:

- **Python 3.11 or newer**;
- **pip** or **pipx**;
- an OpenAI-compatible JSONL trace file;
- representative request data with `model`, `messages`, and `usage`.

You do **not** need:

- an Azure subscription;
- an Azure OpenAI or OpenAI API key;
- network access at analysis time;
- a database or hosted service.

## Installation

### Install from GitHub with pipx

```bash
pipx install git+https://github.com/zakarel/tokenlens-for-azure.git
```

### Install from GitHub with pip

```bash
python -m pip install git+https://github.com/zakarel/tokenlens-for-azure.git
```

### Install for local development

```bash
git clone https://github.com/zakarel/tokenlens-for-azure.git
cd tokenlens-for-azure
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/tokenlens-azure --help
```

No shell activation is required. On Windows PowerShell, use:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\tokenlens-azure.exe --help
```

For Foundry capture, install the optional dependencies with
`.venv/bin/python -m pip install -e ".[foundry]"`. Activating with
`. .venv/bin/activate` (POSIX) or `.venv\Scripts\Activate.ps1` (PowerShell) is
an optional convenience only.

The package will be published to PyPI after the v0.1 interface stabilizes.

## How to use it

### Analyze a trace file

```bash
.venv/bin/tokenlens-azure analyze requests.jsonl
```

The command is advisory and exits successfully while reporting findings.
Pass multiple JSONL files, a directory, or a glob to analyze every observed
deployment: `.venv/bin/tokenlens-azure analyze "traces/*.jsonl"`.

### Generate a JSON report

```bash
.venv/bin/tokenlens-azure analyze requests.jsonl \
  --format json \
  --output tokenlens.json
```

### Generate a GitHub SARIF report

```bash
tokenlens-azure analyze requests.jsonl \
  --format sarif \
  --output tokenlens.sarif
```

### Generate a shareable HTML report

```bash
tokenlens-azure analyze requests.jsonl \
  --format html \
  --output-dir reports \
  --open
```

When `--output` is omitted, HTML, JSON, and SARIF reports use a UTC filename
such as `tokenlens-report-20260914-082311Z.html`; collisions receive `-2`,
`-3`, and so on. Text remains on stdout. Use `--output` when a script needs a
stable path.

### First-run Foundry capture (Entra ID)

TokenLens analysis is offline. Capture is an explicit, separate step using the
Entra ID credential chain—no API key is required:

```bash
az login
.venv/bin/tokenlens-azure doctor
.venv/bin/tokenlens-azure init-foundry
export AZURE_OPENAI_ENDPOINT="https://YOUR-RESOURCE.openai.azure.com/"
.venv/bin/python examples/capture_foundry.py \
  --deployment YOUR_DEPLOYMENT \
  --prompt "Classify this support request as billing or support."
.venv/bin/tokenlens-azure analyze foundry-traces --format html --open
```

The capture example performs one real request only when a deployment and
prompt are supplied, records the deployment and returned model separately, and
never writes credentials or bearer tokens. Your Entra identity needs an Azure
OpenAI/Foundry inference role on the resource. API-key authentication is not
the primary onboarding path and is intentionally not shown here.

`doctor` checks Python, write permissions, optional packages, endpoint
configuration, and the `DefaultAzureCredential` chain without exposing
secrets. `init-foundry` creates a private `foundry-traces/` directory and a
copy of the capture example.

### Compare a candidate with a baseline

```bash
tokenlens-azure compare \
  baseline.jsonl \
  candidate.jsonl \
  --fail-on-regression 10
```

`--fail-on-regression` is optional. It enables an explicit CI gate when the candidate’s addressable-waste percentage increases beyond the supplied number of percentage points.

### Read from standard input

```bash
cat requests.jsonl | tokenlens-azure analyze -
```

## Input format

TokenLens accepts one JSON object per line. `model`, `messages`, and `usage` are
the minimum request-analysis fields. Accurate pricing additionally needs the
underlying `model_name` and `deployment_mode`; PTU analysis needs valid
timestamps and benefits from latency and status-code evidence.

```json
{
  "timestamp": "2026-09-03T12:00:00Z",
  "request_id": "req-123",
  "model": "support-prod",
  "deployment_name": "support-prod",
  "model_name": "MAI-Thinking-1",
  "provider": "azure_foundry",
  "deployment_mode": "global",
  "messages": [
    {"role": "system", "content": "You are a support assistant."},
    {"role": "user", "content": "Where is my order?"}
  ],
  "tools": [],
  "max_output_tokens": 1000,
  "usage": {
    "input_tokens": 4200,
    "output_tokens": 380,
    "cached_tokens": 1800
  },
  "latency_ms": 1450,
  "status_code": 200,
  "retry_of": null,
  "retrieved_chunks": [],
  "metadata": {
    "workload": "support-assistant",
    "tenant": "hashed-value"
  }
}
```

Standard OpenAI and Azure OpenAI request/response envelopes are normalized by the importer, so applications do not need to redesign their logging schema.

## Account cost and PTU advisor

The packaged reference catalog is a dated offline snapshot of
[Microsoft Foundry model pricing](https://azure.microsoft.com/en-us/pricing/details/ai-foundry-models/microsoft/).
TokenLens matches provider, canonical model, service tier, deployment mode, and
explicit aliases. Dated model-version suffixes are preserved, and missing
deployment modes remain unresolved rather than defaulting to Global. The
analysis-date snapshot is applied as a current estimate even when traces are
older; it is not presented as historical billing data. TokenLens never
substitutes the nearest model: unmatched traffic stays visible as
`Pricing unavailable` and is excluded from monetary totals. A report uses one
currency and performs no implicit conversion, so prices in other currencies
remain unresolved. Reference prices are estimates, not invoices; negotiated
agreements, offers, regions, currencies, and later price changes may differ.

The PTU view adapts the workload dimensions, model-capacity data, sizing,
break-even, and hybrid spillover formulas from Microsoft's MIT-licensed
[PTU Advisor](https://github.com/msftse-org/ptu-advisor), revision
`eb0558cd4c6d3794be76d9caa2e87129d1f8221c`. It aggregates request traces into
five-minute buckets and evaluates each deployment independently. Unknown models
never fall back to GPT-4o capacity. If exact PAYG pricing or supported capacity
is unavailable, TokenLens shows the workload evidence without inventing a
monetary comparison. Hybrid economics sweep valid PTU increments and select the
lowest-cost reserved baseline plus PAYG spillover rather than assuming a
peak-sized commitment. See [Third-Party Notices](THIRD_PARTY_NOTICES.md).

## Task economics

Token cost is not task cost: one business task can contain retries, tools,
multiple models, and late human review. Task economics therefore requires
explicit metadata and never infers identity from prompts, routes, or models.
The append-only v2 JSONL stream supports `model_call`, `tool_step`,
`task_result`, and `human_review` events:

```json
{"schema_version":2,"event_id":"synthetic-event","event_type":"model_call","timestamp":"2026-09-14T10:00:00Z","task_id":"synthetic-task","task_type":"ticket-classification","attempt_id":"attempt-1","step_index":1,"execution_strategy":"default","strategy_version":"v1","provider":"synthetic","deployment_name":"synthetic-deployment","model_name":"synthetic-model","service_tier":"standard","usage":{"input_tokens":900,"cached_input_tokens":150,"cache_write_tokens":0,"output_tokens":120,"reasoning_tokens":30},"observed_cost_usd":0.01}
```

Measurement is progressive per task type:

1. **Task cost measured** — explicit task fields and billable steps;
2. **Solved-task economics measured** — explicit closed outcomes and success denominators;
3. **Correct-task economics measured** — human reviews and observed cleanup dollars.

Open tasks remain visible but never enter success denominators. Monetary averages
omit partially unresolved trajectories. Pricing precedence is observed event
cost, customer catalog, dated offline reference catalog, then unresolved
(tokens remain available and dollars do not). Cleanup dollars are used only
when emitted by a review event. Strategies are compared only for equivalent
task types and strategy versions, with provisional and ranked sample thresholds
shown in the report.

Task economics remains available for explicit v2 task-event streams as a
dedicated advanced report. The account-facing HTML uses Cost analysis instead,
because task IDs, outcomes, and strategy versions are not normally available
from account request traces.

Run a first local report with no arguments in an interactive terminal:

```bash
tokenlens-azure
tokenlens-azure configure
tokenlens-azure instrument --language python
```

Non-interactive and CI invocations print help and never prompt. The optional
Python helper writes only aggregate usage and explicit metadata:

```python
from tokenlens.instrumentation import task

with task(task_id=correlation_id, task_type="ticket-classification",
          execution_strategy="default", strategy_version="v1") as run:
    result = run.record_model_call(
        deployment="general-prod", model="example-model", call=call_model
    )
    run.complete(outcome="solved", automated_check="passed")
```

Rotate the JSONL file at the host boundary (for example, daily or at a size
limit), then analyze all retained rotations together. Scheduled jobs should
join late result and review events by opaque event ID, retain open tasks across
lookback windows, and publish only private HTML/JSON artifacts. Raw streams are
never uploaded by TokenLens.

## Eight diagnostics

| Rule | Detects |
|---|---|
| `TL001` | Repeated system prefixes and policy text |
| `TL002` | Unbounded conversation-history growth |
| `TL003` | Oversized or unused tool schemas |
| `TL004` | Duplicate or overlapping retrieval context |
| `TL005` | Retry amplification |
| `TL006` | Excessive output allocation |
| `TL007` | Semantic-cache opportunities |
| `TL008` | Possible model over-sizing |

`TL008` is intentionally advisory: it recommends evaluation with a quality baseline rather than blindly downgrading a model.

## Azure-first recommendations

| Finding | Azure remediation |
|---|---|
| Repeated stable prompt prefix | Azure OpenAI prompt caching |
| Repeated equivalent requests | APIM semantic caching with Azure Managed Redis |
| Token spikes or uncontrolled consumers | APIM token rate limits and quotas |
| Likely model over-sizing | Microsoft Foundry Model Router evaluation |
| Noisy retrieval context | Azure AI Search chunking, hybrid retrieval, and semantic reranking |
| Prompt/context compression opportunity | Custom compression workload hosted on Azure |
| Cross-workload visibility | Application Insights and Azure Monitor instrumentation |

The Azure mapping addresses the same optimization problem; it does not imply feature-for-feature parity with every open-source project.

## GitHub Actions

The checked-in composite action returns a `report-path` output and uses a
timestamped report filename:

```yaml
name: Token efficiency

on:
  pull_request:

jobs:
  tokenlens:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: ./
        id: tokenlens
        with:
          input: test-data/requests.jsonl
          format: sarif
          fail-on-regression: "10"
      - uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: ${{ steps.tokenlens.outputs.report-path }}
```

Existing inefficiencies stay in the baseline; pull requests surface newly introduced waste.

For scheduled reporting, see `.github/workflows/tokenlens-scheduled.yml`. It is
designed for a private/self-hosted runner where `TOKENLENS_TRACE_INPUT` points
to an explicit secure source; only short-retention HTML/JSON artifacts are
uploaded, never raw traces. Locally, use `scripts/run-scheduled-report.sh`
with `TOKENLENS_TRACE_INPUT` and `TOKENLENS_REPORT_DIR`. macOS `launchd`,
Linux `cron`, and Windows Task Scheduler can invoke that wrapper without
activating a virtual environment. The wrapper never uploads or emails reports.
For task economics, point the input at a private lookback directory containing
rotated v2 event streams. Re-run the same window when late `task_result` or
`human_review` events arrive; event IDs make the join idempotent. Keep open
tasks visible across report periods, update customer catalogs with effective
dates, retain private artifacts only as long as required, and do not upload raw
streams.

## Configuration

Create `.tokenlens.yml` in the repository root:

```yaml
version: 1

analysis:
  tokenizer: auto
  redact_content: true
  tenant_key: metadata.tenant
  workload_key: metadata.workload

rules:
  TL001:
    enabled: true
    minimum_repeated_tokens: 10000
  TL004:
    enabled: true
    similarity_threshold: 0.85
  TL008:
    enabled: true
    severity: info

ci:
  advisory: true
  fail_on_regression_percent: null
```
## License

TokenLens for Azure is released under the [MIT License](LICENSE).

---

<div align="center">


</div>
