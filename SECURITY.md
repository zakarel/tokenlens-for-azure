# Security and privacy

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/zakarel/tokenlens-for-azure/security/advisories/new).
Do not open a public issue or include credentials, prompts, responses, traces,
tenant or subscription identifiers, endpoints, customer pricing, or reports.
Include the affected version, a sanitized reproduction, impact, and proposed
mitigation where known.

## Threat model and data boundary

TokenLens analyses locally and does not send prompts, responses, credentials, or
authentication tokens to a TokenLens service. Azure collection and explicit
pricing synchronization are separate operations that contact only the documented
allow-listed sources.

Default telemetry is contentless: it stores counts, timing, status, deployment
and model identity, and optional keyed local fingerprints. Prompt text, response
text, tool arguments/results, retrieved documents, headers, IP addresses, and
credentials are not stored. Fingerprint keys remain local and are never exported.

Imported telemetry and pricing files are untrusted input. Parsers are bounded,
allow-listed, fail closed on malformed schemas, and write caches atomically with
user-only permissions where supported. Contract, Marketplace, override, and
actual-cost data remains customer-local and is never merged into public pricing
snapshots.

Generated reports are private by default. Do not upload reports derived from
real telemetry to this public repository, public issue trackers, or public CI
artifacts. Use synthetic fixtures for public bug reports and documentation.
