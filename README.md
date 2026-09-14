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

<a href="docs/assets/tokenlens-report-ptu-dashboard-20260914-v7.png">
  <img src="docs/assets/tokenlens-report-ptu-dashboard-20260914-v7.png" alt="TokenLens for Azure synthetic PTU Advisor dashboard showing the recommendation banner with confidence, six At a Glance metrics, and the token volume and request outcome evidence charts">
</a>

<a href="docs/assets/tokenlens-report-ptu-advisor-20260914-v6.png">
  <img src="docs/assets/tokenlens-report-ptu-advisor-20260914-v6.png" alt="TokenLens for Azure synthetic PTU advisor report showing workload dimensions, sizing, and PAYG versus hybrid economics">
</a>

<p align="center"><em>Deterministic synthetic data · Click any image to open the enlarged PNG.</em></p>

The report has four self-contained views:

- **Overview** combines consumption, estimated analyzed cost, pricing coverage, and priority actions.
- **Cost analysis** shows exact model/mode prices, cost composition, unresolved pricing, and model/deployment spend.
- **Usage &amp; diagnostics** keeps the corrected multicolor model mix, visible model summary, deployment usage, and all findings.
- **PTU advisor** is a per-deployment decision dashboard: recommendation banner with evidence confidence, six At a Glance metrics, four evidence charts with synchronized zoom and expansion, capacity sizing, PAYG versus PTU + spillover economics, rationale, and a print/JSON export.

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

TokenLens analysis is offline. Collection is an explicit, separate step using
the Entra ID credential chain—no API key is required.

#### Which collection path do I need?

| Goal | Recommended path |
|---|---|
| Verify one deployment works | `tokenlens-azure smoke-test-foundry` |
| Analyze prompt/token efficiency | SDK wrapper (`tokenlens.integrations`) or OpenTelemetry |
| Decide whether PTU is worthwhile | `tokenlens-azure collect-foundry-metrics` |
| Analyze OpenTelemetry telemetry you already have | `tokenlens-azure import-otel` |
| Analyze an Application Insights/Log Analytics export you already have | `tokenlens-azure import-app-insights` |
| Query Application Insights directly for GenAI traces | `tokenlens-azure collect-app-insights` |
| Analyze existing logs in another JSON shape | `tokenlens-azure import --mapping` |
| Centralize usage metadata at the gateway instead of the client | APIM AI Gateway policy (see below) |

You never run a TokenLens command per prompt. Instrument the client once, or
collect Azure Monitor aggregates once per window.

#### 1. Smoke test one deployment

```bash
az login
.venv/bin/tokenlens-azure doctor
export AZURE_OPENAI_ENDPOINT="https://YOUR-RESOURCE.openai.azure.com/"
.venv/bin/tokenlens-azure smoke-test-foundry --deployment YOUR_DEPLOYMENT
```

This makes **exactly one billable request** and says so before running. It
proves connectivity and normalization. It cannot support PTU analysis: one call
per deployment is not a representative workload. Claude deployments use
`--api anthropic` and the Messages API; they are never routed through OpenAI
chat completions. For Claude, set the endpoint to the Anthropic Foundry base
URL and rely on the Entra credential chain:

```bash
export FOUNDRY_ENDPOINT="https://YOUR-RESOURCE.services.ai.azure.com/anthropic"
.venv/bin/tokenlens-azure smoke-test-foundry \
  --deployment YOUR_CLAUDE_DEPLOYMENT \
  --api anthropic
```

The Claude smoke-test path requests an Entra token for
`https://ai.azure.com/.default`; no Anthropic API key is required.

#### 2. Collect application request telemetry

Instrument the model client once at application startup:

```python
from tokenlens.integrations.openai import instrument_openai

client = instrument_openai(
    existing_client,
    deployment_mode="global",        # Global or Regional; never inferred
    workload="support-assistant",
)

response = client.chat.completions.create(model="support-prod", messages=messages)
```

```python
from tokenlens.integrations.anthropic_foundry import instrument_anthropic_foundry

client = instrument_anthropic_foundry(existing_client, deployment_mode="global", workload="coding-agent")
```

The wrapper adds no extra inference request, returns the SDK's own response
object, supports streaming, and writes contentless records to
`./tokenlens-traces/tokenlens-YYYY-MM-DD.jsonl`. See
`examples/instrument_azure_openai.py` and `examples/instrument_claude_foundry.py`.

Already emitting OpenTelemetry GenAI spans? Mirror them instead:

```python
provider.add_span_processor(TokenLensSpanProcessor(output_dir="tokenlens-traces"))
```

or import an export offline:

```bash
.venv/bin/tokenlens-azure import-otel spans.json --output-dir tokenlens-traces
```

Already have telemetry somewhere else? Three more offline-safe paths cover
existing data without a new SDK integration:

**Application Insights / Log Analytics.** If GenAI OpenTelemetry attributes
already reached Application Insights (for example via an OpenTelemetry
exporter), import an export you already have — either the
`{"tables": [...]}` shape from `az monitor app-insights query`/the Logs REST
API, or a flat continuous-export JSON/JSONL file:

```bash
.venv/bin/tokenlens-azure import-app-insights app-insights-export.json --output-dir tokenlens-traces
```

This reuses the OpenTelemetry importer's attribute allow list and content
rejection directly, so the two commands behave identically for equivalent
data. To query Application Insights directly instead of exporting first, use
the explicit collection command (install `tokenlens-azure[foundry-monitor]`
for `azure-monitor-query`):

```bash
az login
.venv/bin/tokenlens-azure collect-app-insights --workspace-id LOG_ANALYTICS_WORKSPACE_ID --days 14
```

`collect-app-insights` is the only Application Insights path that contacts
Azure, and it says so before running. It sends one fixed, reviewable KQL
query that selects GenAI usage dimensions (`customDimensions`, duration,
success, result code) — never a request/response body column — and maps the
result the same way `import-app-insights` does.

**Existing logs in another JSON shape.** If your logs already carry
usage/latency data but not in TokenLens's or OpenTelemetry's shape, describe
the mapping declaratively instead of writing a converter:

```bash
.venv/bin/tokenlens-azure import app-logs.jsonl --mapping tokenlens-mapping.yml
```

See `examples/generic-log-mapping-example.yml` for a documented template. The
mapping file is parsed with `yaml.safe_load` only — it can never execute code
— and it may only target TokenLens's contentless schema fields, so it has no
destination for prompt, response, tool, or credential content even if a
source path pointed at one. A source path whose final segment looks like
content or a credential is rejected before any row is read.

**API Management AI Gateway.** If you front Azure OpenAI/Foundry deployments
with APIM, you can centralize usage metadata at the gateway instead of (or in
addition to) instrumenting every client. `examples/apim-ai-gateway-policy.xml`
is a documented, metadata-only policy example: it emits deployment/model,
token usage, latency, and HTTP status via APIM's built-in
`azure-openai-emit-token-metric`/`emit-metric` policies, and it never reads a
request/response body or logs a header value. TokenLens does not apply this
policy for you — copy the fragment you need into your own API/operation
policy and adjust the metric destination. **APIM's AI Gateway policy support
differs by model API and APIM SKU/version**; verify current policy names and
availability in the Azure API Management documentation before relying on it,
and use the file's generic `emit-metric` fallback for backends (for example
Claude on Foundry) that have no dedicated token-metric policy.

#### 3. Collect Azure Monitor metrics for PTU

```bash
az login
.venv/bin/tokenlens-azure connect-foundry
.venv/bin/tokenlens-azure list-foundry-resources --subscription SUBSCRIPTION_ID
.venv/bin/tokenlens-azure collect-foundry-metrics \
  --resource-group RESOURCE_GROUP \
  --account ACCOUNT \
  --days 14 \
  --deployment YOUR_DEPLOYMENT \
  --deployment-mode global
.venv/bin/tokenlens-azure analyze local-traces/foundry-metrics --format html --open
```

`collect-foundry-metrics` contacts Azure Monitor and collects five-minute
aggregate counters only: token volume, request counts, HTTP 429 counts, and
average latency. It never collects request bodies, prompts, responses, user
IDs, IP addresses, or headers, and it never writes a subscription, tenant, or
resource identifier into telemetry. Re-running the same window is idempotent:
bucket identifiers are deterministic and are checked against the records already
in the output directory, so an overlapping re-collection appends only new
buckets. `--deployment-mode` applies to every collected deployment, including an
unrestricted collection where deployment names are discovered from the returned
metric dimensions. Metrics the resource does not expose are reported as missing,
never substituted with zero.

Discovery is scoped to one explicitly selected subscription — TokenLens never
scans every accessible subscription.

`connect-foundry` asks at most four questions, writes a credential-free
`.tokenlens.yml`, and creates private output directories. `doctor` reports
offline-analyzer readiness, each optional extra, credential availability,
configuration validity, and output-directory permissions without ever making an
inference call.

#### Privacy model

Default telemetry **never** contains prompts, responses, system messages, tool
arguments, retrieved text, headers, API keys, bearer tokens, connection
strings, endpoints, or tenant/subscription/request identifiers. There is no
content-capture mode in this release; `telemetry.content_capture: true` is
rejected.

Repeated-prefix diagnostics use keyed HMAC-SHA256 fingerprints of the system
prompt and tool schema, never the text itself:

```bash
export TOKENLENS_FINGERPRINT_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
```

If no key is configured, fingerprints are omitted rather than downgraded to an
unsalted hash. The key lives in your environment and is never written into
telemetry.

#### Rotation, retention, and backpressure

| Setting | Environment variable | Default |
|---|---|---|
| Output directory | `TOKENLENS_TELEMETRY_DIR` | `tokenlens-traces` |
| Size rotation | `TOKENLENS_TELEMETRY_MAX_MB` | `50` |
| Retention | `TOKENLENS_TELEMETRY_RETENTION_DAYS` | `30` |
| Sampling | `TOKENLENS_TELEMETRY_SAMPLE_RATE` | `1.0` |
| Strict mode | `TOKENLENS_TELEMETRY_STRICT` | `false` |

Files rotate daily and by size, are created with owner-only permissions, and
retention only ever deletes TokenLens's own dated files inside the configured
directory. Retention is measured by **local file age**, not by the timestamps
inside a file, so a backfilled window — an imported OpenTelemetry export or a
14-day Azure Monitor collection — is never deleted by the same run that wrote
it. Telemetry failures never fail a model call unless `strict` is enabled, but
they are never silent either: an error callback fires, warnings are
rate-limited, and `writer.dropped_events` exposes the count. PTU aggregate
metrics are never sampled.

Collection and import are **idempotent**. Canonical records carry a
deterministic `event_id`, so re-running `import-otel`, `import-app-insights`,
`import` (generic mapping), `collect-app-insights`, or
`collect-foundry-metrics` over an overlapping window appends only what is new
and reports `records-already-present`. Analysis deduplicates on the same
identifier, so a rotated file copied beside its original is not counted twice.
Legacy schema v1/v2 traces have no canonical identifier and are never
deduplicated.

#### Legacy capture example

The earlier `examples/capture_foundry.py --prompt ...` workflow still exists as
a manual smoke test and is superseded by `smoke-test-foundry`. `init-foundry`
creates a private `foundry-traces/` directory and a copy of that example.

### Compare a candidate with a baseline

```bash
tokenlens-azure compare \
  baseline.jsonl \
  candidate.jsonl \
  --fail-on-regression 10
```

`--fail-on-regression` is optional. It enables an explicit CI gate when the candidate’s addressable-waste percentage increases beyond the supplied number of percentage points.

### Audit pricing coverage without generating a report

```bash
tokenlens-azure pricing-audit requests.jsonl
```

`pricing-audit` never makes a network call. For each unique returned model
ID it prints the deployment mode, service tier, request/token coverage, the
selected catalog and billing basis, the unresolved reason when a model does
not price, and a suggested local override key — without ever printing
endpoints, resource IDs, tenant values, request IDs, or prompt/response
content. Use it to find exactly which models need a customer catalog entry
before generating a full report.

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

### Coverage-aware, multi-source pricing

Each price entry carries a `billing_basis` alongside its rate — `token_rate`
(a standard Azure per-token meter), `claude_ccu_equivalent` (a
dollar-equivalent estimate derived from Anthropic/Claude's Foundry Claude
Consumption Unit rate card — not an Azure token meter), or
`marketplace_partner_token_rate` (a partner model billed through its own
Azure Marketplace offer). Every rate also tracks its `confidence`
(`verified`, `customer_override`, or the runtime `observed` state used for
observed per-call cost) so a report never presents an estimate with the same
weight as a bundled, dated snapshot.

Resolution precedence is unchanged and strict: observed per-call cost, then
an exact customer-catalog match, then the packaged reference catalog, then
unresolved. TokenLens never derives a rate from a related model, family, or
version. When a model has no authoritative published rate — including a
preview/unannounced model ID — it stays `Pricing unavailable` with an exact
unresolved reason and a suggested override key rather than being silently
estimated. Copy [`examples/customer-pricing-overrides-example.yml`](examples/customer-pricing-overrides-example.yml)
into your own pricing configuration and fill in only the models you have a
confirmed rate for; as shipped, every entry is commented out so it loads as
an empty catalog instead of a misleading `$0.00`.

Cost analysis stays useful at every coverage level. At 0% coverage the tab
still shows token volume, request counts, the unresolved reason per model,
and a remediation link to `pricing-audit` — never three zero-width bars that
could be mistaken for a resolved $0 cost. At partial coverage, the composition
chart adds a hatched "unresolved (excluded)" segment sized by excluded token
share, and the model/deployment tables gain **Billing basis** and
**Source/status** columns. Overview's Estimated cost KPI uses adaptive
precision (as many decimals as needed) so a genuinely nonzero micro-cost is
never rounded down to `$0.00`.

### PTU Advisor dashboard

The PTU Advisor tab is a per-deployment decision dashboard. It renders one
selected deployment at a time (with a deployment selector and URL fragment
state when a report contains several), so unrelated model or deployment slices
are never merged into a single recommendation.

Each selected deployment shows:

- a full-width **recommendation banner** with a status badge, an evidence
  confidence score, a one-sentence recommendation, an interpretation, and an
  export control;
- **At a Glance**: Scope, Typical Throughput (average weighted TPM), Busy-Hour
  Throughput (P95 weighted TPM), Total Tokens, Daily Average Tokens, and
  Rate-Limit Events — three columns by two rows on desktop, two on medium
  screens, one on mobile;
- four **evidence charts** — Token Volume Over Time, Request Outcomes, Daily
  Deployment Cost, and Rate-Limit Events (429) — sharing one observed window,
  with legends, focusable SVG points, accessible summaries, table fallbacks,
  an expand control, and a keyboard range slider whose zoom is synchronized
  across all four;
- the **capacity and cost decision** graphs: weighted TPM with average, P95,
  and selected PTU capacity; and the PAYG vs PTU + spillover cost explorer with
  break-even markers, current cost points, and the monthly difference;
- **why this recommendation**, **what would change it**, **assumptions**,
  **confidence and data quality**, and **recommended next steps**, all from
  deterministic templates grounded in typed metrics — never generated text.

Banner states are explicit:

| State | Meaning |
|---|---|
| PTU Recommended | Operational evidence and modeled economics support commitment |
| Borderline — validate before committing | PTU and PAYG are close, or signals disagree |
| PAYG Recommended | PAYG is the safer or cheaper strategy |
| Insufficient Evidence | The window or required metrics cannot support a recommendation |
| Pricing Required | Workload evidence exists but economics cannot be calculated |
| PTU Not Applicable | The model has no applicable Azure PTU offer |
| Capacity Data Required | Exact model/version PTU capacity is unavailable |

The confidence score measures confidence in the **evidence and
classification**, not the probability that PTU saves money — a high-confidence
Borderline result is valid. It is a documented, unit-tested weighted sum of
active-bucket count, lookback duration, bucket continuity, request-outcome
coverage, latency coverage, pricing coverage, model-capacity availability, and
deployment-mode certainty. Zero-filled missing buckets never earn confidence,
and no score is shown when the evidence gate fails: the missing evidence is
shown instead.

Everything the dashboard renders comes from one typed payload
(`PtuDashboardData`). The renderer never reaches back into raw records and
never recomputes a rate: Daily Deployment Cost is produced by the same pricing
engine and provenance shown in Cost analysis, and reconciles with it exactly.
At partial pricing coverage the chart adds a **Partial estimate** badge and
discloses the excluded token share; at 0% coverage the card is kept with an
explicit pricing-required state and the observed daily token totals, and no
zero-dollar bars are drawn.

Missing metrics are always explicit. A source that never reported request
outcomes shows `Metric unavailable`, not a zero line; a genuine zero 429 count
is shown as `0.00%`. Cached-token volume that Azure Monitor does not expose for
a model omits that series and says so.

`Export report` produces a print-optimized, selected-deployment view suitable
for Save as PDF (legends, axes, colours, and text alternatives preserved), and
`Download JSON` writes the embedded typed aggregate payload. Neither contacts a
server, and neither contains prompts, responses, credentials, endpoints,
subscription IDs, tenant IDs, or request IDs.

Large series are downsampled for display only, using deterministic LTTB; exact
values are retained for tooltips, tables, calculations, and exports, and the
range readout states when display downsampling is active.

### PTU eligibility states and cost/throughput graphs

Every deployment gets one of six explicit eligibility states — eligible with
sufficient evidence, eligible but insufficient evidence, model capacity
unavailable, PTU not applicable, pricing unavailable, or deployment mode
unavailable — shown as a badge on its PTU card. Partner/marketplace models
(for example Anthropic or Mistral models served through Foundry) report
**PTU not applicable** rather than "Model not supported": Azure PTU capacity
purchasing is a Microsoft first-party model feature, so a partner model
correctly never gets a numeric PTU recommendation.

For eligible deployments with at least 100 active five-minute buckets,
TokenLens renders two native inline SVG graphs sourced entirely from typed
report data — no chart runtime is embedded in the self-contained HTML:

- **Throughput over time** — five-minute weighted TPM, an average-TPM line, a
  P95 reference line, and an optional PTU-capacity line, with a compact
  accessible table fallback and keyboard-focusable sample points.
- **Cost explorer** — a dashed PAYG line and a solid PTU + spillover line
  across sustained TPM, break-even marker(s), the selected PTU-capacity
  marker, the observed-average-TPM marker, the current PAYG/hybrid cost
  points, and a shaded interval where PTU is cheaper.

The renderer never recalculates economics: every curve point, break-even
crossing, and "current cost" marker is emitted by the same engine functions
used to pick the PTU baseline. A three-call smoke trace — enough to confirm
connectivity and trace normalization, not enough to size dedicated capacity —
stays **eligible but insufficient evidence**: TokenLens explains that at
least 100 active five-minute buckets are required and shows no graph and no
invented recommendation. For a real workload assessment, capture a
representative multi-day window per deployment (not a one-request-per-deployment
smoke test) so enough five-minute buckets are observed for a confident
recommendation and cost curve.

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

### Which diagnostics work without raw content?

| Diagnostic | Azure Monitor aggregates | Contentless request telemetry | Raw content |
|---|---|---|---|
| Token volume | Yes | Yes | Not required |
| Output-token waste (`TL006`) | Limited | Yes | Not required |
| Retry waste (`TL005`) | Limited | Yes | Not required |
| Cached-prefix utilisation | Limited | Yes | Not required |
| Repeated system prefix (`TL001`) | No | Yes — HMAC fingerprint + token count | Not required |
| Repeated tool schema (`TL003`) | No | Yes — fingerprint + token count | Not required |
| Retrieval overfetch (`TL004`) | No | Yes — retrieval token count | Not required |
| Semantic prompt quality (`TL007`) | No | No | Would require content |

When the telemetry a diagnostic needs is absent, the report says so explicitly:
the rule is reported as **not evaluated**, with the missing field named.
TokenLens never treats missing content as evidence that a workload is
efficient, and never infers prompt identity from content.

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

Collection settings are separate and credential-free. `tokenlens-azure
connect-foundry` writes them for you:

```yaml
telemetry:
  output_dir: tokenlens-traces
  content_capture: false          # raw content capture is not supported
  sample_rate: 1.0                # deterministic, stable per request
  rotation:
    max_mb: 50
    retention_days: 30
  fingerprints:
    enabled: true
    key_env: TOKENLENS_FINGERPRINT_KEY

foundry:
  subscription_id_env: AZURE_SUBSCRIPTION_ID
  resource_group: example-resource-group
  account: example-foundry-account
  deployments:
    - example-deployment

monitor:
  lookback_days: 14
  granularity_minutes: 5
```

Credentials, access tokens, tenant secrets, API keys, and bearer tokens are
never stored. Subscription identity is referenced by environment-variable name,
never written into the file.

## License

TokenLens for Azure is released under the [MIT License](LICENSE).

---

<div align="center">


</div>
