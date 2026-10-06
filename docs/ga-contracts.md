# TokenLens GA contracts

## Assessment modes

- **Operational** uses Foundry inventory, Azure Monitor, pricing, workloads,
  aggregate insights, and PTU readiness.
- **Optimization** uses SDK wrappers, OpenTelemetry, Application Insights, or
  contentless JSONL request telemetry for TL001–TL008.
- **Full** runs both tracks.

Aggregate telemetry cannot evaluate request-level prompt, retrieval, caching, or
model-sizing rules. TokenLens reports those rules as not evaluated rather than
guessing.

## Pricing sources

The Azure Retail Prices API is the primary machine-readable public source.
Official Microsoft Foundry provider pages are a fail-closed secondary source
when a published rate is absent from the API. Contract, Marketplace, override,
and Actual Cost Details sources remain separate tracks. Unknown or ambiguous
rates never become a numeric estimate.

## Insights

Aggregate insights include evidence, impact, confidence, applicability,
recommendation, caveats, and a verification step. Request-level findings are
reported separately. Savings scenarios are not added together because they may
overlap.

## Privacy and offline boundary

Analysis and report generation are offline after explicit collection or pricing
synchronization. Default telemetry is contentless. Prompt text, responses,
tool arguments/results, retrieved documents, credentials, headers, identities,
and IP addresses are not collected.
