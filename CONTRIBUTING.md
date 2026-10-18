# Contributing to TokenLens

Thank you for contributing. All changes to `main` go through a pull request;
do not push directly to the protected branch.

## Before opening an issue or pull request

- Use a supported Python version listed in `RELEASE.md`.
- Reproduce problems with synthetic data whenever possible.
- Never commit prompts, responses, traces, credentials, tenant or subscription
  identifiers, endpoints, customer pricing, reports, screenshots, or local
  configuration.
- Report suspected vulnerabilities privately using `SECURITY.md`, not a public
  issue.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Analysis is expected to work offline. Tests must use injected transports and
synthetic fixtures; they must not contact Azure or public pricing endpoints.

## Pull requests

Describe the user-visible change, linked issue, compatibility impact, privacy
impact, and negative cases tested. Update documentation and changelog entries
when behaviour or public contracts change. Screenshots must use synthetic
data. Keep pull requests focused and do not include generated reports or
private files.

Maintainers may request changes that improve correctness, privacy, accessibility,
or supportability before merging.
