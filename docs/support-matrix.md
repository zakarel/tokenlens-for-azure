# Support matrix

## Supported runtime

| Area | Supported |
|---|---|
| Python | 3.11, 3.12, 3.13 |
| Operating systems | Ubuntu, macOS, Windows |
| Analysis | Offline after explicit collection or pricing synchronization |
| Reports | HTML, JSON, SARIF, and terminal text |
| Azure scope | Microsoft Foundry/Azure OpenAI collection where the installed monitor extras and caller permissions support the resource |

## Assessment modes

- **Operational:** Foundry inventory, Azure Monitor, pricing, workloads,
  aggregate insights, and PTU evidence.
- **Optimization:** contentless SDK, OpenTelemetry, Application Insights, or
  JSONL request telemetry for request-level rules.
- **Full:** both tracks.

Aggregate Azure Monitor telemetry cannot evaluate prompt, retrieval, tool,
retry, or per-request cache behaviour. TokenLens reports those diagnostics as
not evaluated rather than guessing.

## Data and pricing boundaries

TokenLens does not guarantee numeric pricing for every future model or
commercial arrangement. Exact verified rates, customer catalogs, Marketplace
terms, and actual-cost imports remain separate evidence states. Unknown prices,
unsupported modalities, sparse PTU evidence, and missing metrics remain visible
as limitations rather than zeroes or invented estimates.

## Release support

The current supported release is `1.0.1`. Security reports and sanitized
support diagnostics must follow `SECURITY.md`; real telemetry must not be
attached to public issues, pull requests, or public CI artifacts.
