# Release policy

## Support matrix

The GA-track package targets Python 3.11, 3.12, and 3.13 on supported Linux,
macOS, and Windows environments. Analysis is local and offline after explicit
collection or synchronization.

## Release sequence

`0.2.0a1` is the truth-model foundation prerelease. The planned sequence is
`0.5.0b1` pricing, `0.8.0b1` insights/instrumentation, `1.0.0rc1`, then
`1.0.0` GA. Every release requires the full test suite, wheel/package-data
checks, privacy scans, deterministic pricing/insight fixtures, and CI matrix.

## Rollback criteria

Do not release when a report can convert unavailable values to zero, a pricing
source is incomplete but treated as complete, a PTU recommendation is emitted
without eligibility/evidence, a privacy fixture contains content, or installed
wheel analysis differs from the source checkout. A release is rolled back when
any of these regressions is found after publication.

## Security and support

Security reports should include the product version, sanitized command, and
reproduction fixture without prompts, responses, credentials, identifiers, or
customer pricing. See `SECURITY.md`.
