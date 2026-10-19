# Release policy

## Support matrix

The GA-track package targets Python 3.11, 3.12, and 3.13 on supported Linux,
macOS, and Windows environments. Analysis is local and offline after explicit
collection or synchronization.

## Release sequence

`1.0.1` is the current GA release. It includes the `1.0.0` GA baseline plus
the aggregate-insight UX patch. The baseline was published after the RC soak,
approved live-canary evidence, artifact-level privacy review, and the full
supported-platform matrix. Every release requires the full test suite,
wheel/package-data checks, privacy scans, deterministic pricing/insight
fixtures, and CI matrix.

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

## Release-candidate exit gate

Before tagging `v1.0.0`, record:

- all supported CI checks green on the RC commit;
- a fresh wheel and source distribution install and run successfully;
- synthetic privacy and repository-hygiene scans pass;
- representative private live canaries complete without publishing telemetry;
- no unresolved P0/P1 correctness or security issue;
- rollback owner, support contact, and known limitations are confirmed.
