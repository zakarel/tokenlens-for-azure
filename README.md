<div align="center">

<img src="docs/assets/tokenlens-logo-dark.png" alt="TokenLens for Azure" width="720">

# TokenLens for Azure

### Understand usage, cost, workloads, and PTU readiness across Microsoft Foundry

[![Status](https://img.shields.io/badge/status-pre--alpha-7A5AF8?style=flat-square)](#status)
[![Python](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](#quick-start)
[![Offline analysis](https://img.shields.io/badge/analysis-offline-107C10?style=flat-square)](#privacy)
[![Azure](https://img.shields.io/badge/Microsoft-Foundry-0078D4?style=flat-square&logo=microsoftazure&logoColor=white)](#what-you-get)

</div>

TokenLens discovers your Microsoft Foundry deployments, collects aggregate Azure
Monitor metrics, and creates a self-contained report with:

- usage by deployment, model, and workload;
- estimated cost when an exact verified rate is available;
- pricing coverage and clear remediation when it is not;
- PTU suitability and evidence quality;
- request outcomes, throughput, and rate-limit activity;
- token-efficiency diagnostics supported by the available telemetry.

TokenLens does not send prompts or responses to another service and never guesses
pricing or PTU capacity.

> [!IMPORTANT]
> TokenLens is currently **pre-alpha**. Interfaces and calculation rules may
> change before v0.1.

## Quick start

### 1. Install

```bash
git clone https://github.com/zakarel/tokenlens-for-azure.git
cd tokenlens-for-azure
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[foundry,foundry-claude,foundry-monitor]"
```

### 2. Sign in

```bash
az login
```

### 3. Run TokenLens

```bash
.venv/bin/tokenlens-azure foundry
```

That is the only TokenLens command most users need.

The wizard:

1. checks local readiness;
2. asks whether to inspect all accessible subscriptions or one subscription;
3. discovers Foundry accounts and every deployment in scope;
4. asks for the analysis window;
5. checks pricing readiness;
6. collects each deployment with visible progress;
7. creates technical workloads automatically;
8. generates and opens the report.

## What the wizard looks like

```text
╭──────────────────── TokenLens Foundry ────────────────────╮
│ Full assessment · Usage · Cost · Workloads · PTU          │
╰────────────────────────────────────────────────────────────╯

Step 1/4 · Subscription scope
  1. All accessible subscriptions
  2. One specific subscription

Step 2/4 · Foundry accounts
  ✓ example-foundry-account · East US 2

Step 3/4 · Deployments
  ✓ reasoning-prod · GPT-5.6 Luna · Global Standard
  ✓ coding-prod    · Claude Opus 5 · Global Standard
  ✓ compact-prod   · Ministral 3B   · Global Standard

All 3 deployments will be collected.

Step 4/4 · Analysis window
  1. 7 days
  2. 14 days — recommended
  3. 30 days
  4. 90 days

Pricing readiness
  ✓ reasoning-prod · verified retail rate
  ⚠ coding-prod    · Claude CCU rate unavailable
  ⚠ compact-prod   · exact rate unavailable

Collecting deployments
  reasoning-prod  ━━━━━━━━━━━━━━━━━━━  complete
  coding-prod     ━━━━━━━━━━━━━━━━━━━  complete
  compact-prod    ━━━━━━━━━━━━━━━━━━━  complete

Collection complete
  3 / 3 deployments succeeded
  Report opened
```

The exact presentation depends on terminal capabilities. `NO_COLOR` and
non-interactive terminals receive a plain-text equivalent.

## What you get

| View | Answers |
|---|---|
| **Overview** | What happened, what is measurable, and what needs attention? |
| **Cost analysis** | What is priced, what remains unpriced, and why? |
| **Workloads** | What does each deployment, application, agent, or process cost? |
| **Usage & diagnostics** | Which models and deployments consume tokens, and which diagnostics were evaluated? |
| **PTU advisor** | Is PTU applicable, is there enough evidence, and what capacity or pricing data is missing? |

### Overview

<a href="docs/assets/tokenlens-report-overview-20260917-v8.png">
  <img src="docs/assets/tokenlens-report-overview-20260917-v8.png" alt="Synthetic TokenLens Overview report">
</a>

### Cost analysis

<a href="docs/assets/tokenlens-report-cost-analysis-20260917-v8.png">
  <img src="docs/assets/tokenlens-report-cost-analysis-20260917-v8.png" alt="Synthetic TokenLens Cost analysis report">
</a>

### Workloads

<a href="docs/assets/tokenlens-report-workloads-20260917-v8.png">
  <img src="docs/assets/tokenlens-report-workloads-20260917-v8.png" alt="Synthetic TokenLens Workloads report">
</a>

<p align="center"><em>All screenshots use deterministic synthetic data.</em></p>

## Workload cost

Every deployment automatically appears as a **technical workload**, so workload
cost is visible even when no business metadata is configured.

You can later enrich a dedicated deployment with a business workload such as an
application, agent, API, or business process.

For shared deployments, business allocation requires an explicit workload tag
from SDK or OpenTelemetry instrumentation. TokenLens does not divide shared cost
arbitrarily. Untagged traffic remains visible as **Unassigned**.

## Pricing

Cost estimation requires an exact match for model, deployment mode, service
tier, currency, and billing basis.

TokenLens uses verified catalogs and customer-provided contracted rates. It does
not substitute a related model or silently convert currencies.

If pricing is unresolved, usage and operational analysis still work. The report
shows the affected deployment, excluded tokens, reason, and next safe action.

Claude is a special case: Foundry bills it through Anthropic consumption units,
and an equivalent Azure token rate might not be publicly available. TokenLens
keeps that cost unresolved rather than inventing a conversion.

See [Pricing methodology](docs/pricing.md).

## PTU states

| State | Meaning |
|---|---|
| **PTU recommended** | Evidence and economics support a commitment. |
| **Borderline** | Validate with more representative traffic before committing. |
| **PAYG recommended** | PAYG better fits the observed workload. |
| **Insufficient evidence** | Too few active five-minute buckets support a decision. |
| **Capacity data required** | Exact PTU capacity for this model/version is not verified. |
| **PTU not applicable** | The partner/Marketplace model has no Azure PTU purchase model. |

Claude and Mistral deployments can legitimately show **PTU not applicable**.
That is different from an error.

## Privacy

Collection uses Azure Resource Manager and Azure Monitor. Analysis runs locally.

By default TokenLens does **not** collect prompts, responses, system messages,
tool content, retrieved documents, credentials, user identities, or IP addresses.

Local configuration, telemetry, reports, pricing overrides, and run state are
excluded from Git. Reports omit local source paths and Azure subscription or
tenant identifiers.

## Documentation

- [Pricing methodology and provenance](docs/pricing.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- Built-in help is available from the guided command

## Status

TokenLens is advisory. Estimated costs are not invoices, and PTU results are not
contractual capacity commitments. Validate recommendations against your
agreement, regional availability, quality requirements, and representative
production traffic.

## License

[MIT](LICENSE)
