<div align="center">

# TokenLens for Azure

### Lint LLM traces for avoidable token usage

TokenLens is an offline Python CLI and GitHub Action that analyzes OpenAI-compatible JSONL traces, identifies token waste, and maps findings to practical Azure optimizations.

[![Status](https://img.shields.io/badge/status-pre--alpha-7A5AF8?style=flat-square)](#project-status)
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
- reports evidence, estimated savings ranges, and confidence;
- distinguishes measured findings from heuristic opportunities;
- explicitly reports rules that could not be evaluated because telemetry is missing;
- recommends concrete Azure actions;
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
Run eight offline diagnostics
          │
          ▼
Estimate impact and confidence
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
  "confidence": "high",
  "azure_recommendation": {
    "service": "Azure OpenAI",
    "capability": "Prompt caching"
  }
}
```

Overlapping opportunities are bounded before the aggregate range is reported. Missing data produces **not evaluated**, never a silent pass.

## Example output

<a href="docs/tokenlens-report-demo.html">
  <img src="docs/tokenlens-report-preview.png" alt="TokenLens for Azure sample report showing metrics, data quality, findings, savings ranges, confidence, and Azure actions">
</a>

<p align="center">
  <em>Sample data · Click the image to open the complete self-contained report mock-up.</em>
</p>

The report puts the most actionable information first: total trace volume, addressable token range, data quality, severity-ranked findings, confidence, and prioritized Azure actions.

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
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
```

The package will be published to PyPI after the v0.1 interface stabilizes.

## How to use it

### Analyze a trace file

```bash
tokenlens-azure analyze requests.jsonl
```

The command is advisory and exits successfully while reporting findings.

### Generate a JSON report

```bash
tokenlens-azure analyze requests.jsonl \
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
  --output tokenlens-report.html
```

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

TokenLens accepts one JSON object per line. Only `model`, `messages`, and `usage` are required; the other fields improve diagnostic coverage.

```json
{
  "timestamp": "2026-09-03T12:00:00Z",
  "request_id": "req-123",
  "model": "gpt-5-mini",
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

## Eight diagnostics

| Rule | Detects | Confidence |
|---|---|:---:|
| `TL001` | Repeated system prefixes and policy text | High |
| `TL002` | Unbounded conversation-history growth | High |
| `TL003` | Oversized or unused tool schemas | High |
| `TL004` | Duplicate or overlapping retrieval context | Medium |
| `TL005` | Retry amplification | High |
| `TL006` | Excessive output allocation | High |
| `TL007` | Semantic-cache opportunities | Medium |
| `TL008` | Possible model over-sizing | Low–medium |

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
      - run: python -m pip install -e .
      - run: |
          tokenlens-azure compare \
            test-data/baseline.jsonl \
            test-data/requests.jsonl \
            --format sarif \
            --output tokenlens.sarif \
            --fail-on-regression 10
      - uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: tokenlens.sarif
```

Existing inefficiencies stay in the baseline; pull requests surface newly introduced waste.

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

## Privacy and security

- Analysis runs locally or on the CI runner.
- Version 0.1 makes no network calls and requires no API key.
- No model inspects, rewrites, or summarizes prompts.
- Raw prompt content is omitted from reports by default.
- Tenant and workload boundaries are respected during cache analysis.
- Public tests use synthetic, privacy-safe traces.

TokenLens is safe to bring to the data—not another service that asks developers to upload it.

## Architecture

```text
src/tokenlens/
├── cli.py
├── config.py
├── ingest/
├── models/
├── rules/
├── estimation/
├── recommendations/
└── reports/
```

The core is a streaming JSONL importer, normalized trace model, plugin-style rule engine, impact estimator, recommendation pack, and multiple report renderers.

## Roadmap

### v0.1

- [x] Offline JSONL importer
- [x] Eight diagnostic contracts
- [x] Terminal, JSON, SARIF, and HTML report formats
- [x] Advisory baseline comparison
- [x] Azure remediation pack
- [x] GitHub Action scaffold

### Later

- [ ] APIM and Application Insights import adapters
- [ ] Provider recommendation packs
- [ ] User-supplied pricing catalogs
- [ ] Custom diagnostic plugins
- [ ] Trend reports across releases
- [ ] Optional Azure OpenAI-assisted analysis, disabled by default

## Project status

TokenLens for Azure is an early community project and is **not an official Microsoft product**. APIs, rule thresholds, and package names may change before v0.1.

## License

TokenLens for Azure is released under the [MIT License](LICENSE).

---

<div align="center">

**Spend tokens on answers—not repetition.**

</div>
