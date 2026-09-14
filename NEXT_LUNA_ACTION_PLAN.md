# TokenLens for Azure: Next-Version Action Plan

## Purpose

Implement the next TokenLens version as a production-quality multi-deployment reporting workflow. The work must solve the real onboarding issues encountered during the first Foundry test and make the generated HTML report identical in design and content structure to the report shown in the README.

## Repository state to preserve

- The working tree currently contains user-created, uncommitted changes in `.gitignore` and an untracked `test.py`.
- Treat both as user work. Do not discard, overwrite, commit, or repurpose them without explicit confirmation.
- Real traces under `local-traces/` are private test data. Never commit them or use them as public fixtures.
- Keep TokenLens analysis offline by default. Any Foundry connectivity must be an explicit optional capture or inventory feature.

## Non-negotiable product requirements

1. Generated HTML must use the same navy design, large horizontal logo, deployment-aware content, impact percentages, watermark, and customer-facing language as the README preview.
2. Do not display confidence ratings, data-quality scores, or "Not evaluated" findings in customer reports.
3. Analyze every deployment represented in the input, not only one deployment.
4. Show both the deployment name and underlying model type where available. Do not treat these as interchangeable.
5. Show token usage and findings for the complete trace set and separately for each deployment.
6. Default HTML report filenames must contain the generation date and time.
7. Entra ID must be the primary Foundry authentication path. API keys may be documented only as an optional alternative.
8. Installation and first-run instructions must not require shell activation.
9. Automated reporting must be documented and usable without committing raw production traces.

## Product boundary for "all deployments"

TokenLens must not generate artificial calls to every Foundry deployment. "All deployments" means:

- the offline analyzer processes all deployment values found across one or more real trace files;
- the capture integration records the deployment name on every real application call;
- an optional Foundry inventory input may list deployments with zero observed traffic, but a zero-traffic row must never be presented as analyzed usage;
- historical prompt-level diagnostics require request traces. Deployment inventory or Azure Monitor aggregate metrics alone cannot support prompt, tool, retrieval, or conversation diagnostics.

## Phase 1: Make the generated report authoritative

### Problem

`docs/tokenlens-report-demo.html` is hand-built, while `src/tokenlens/reports.py` independently generates a smaller light-themed report. The README therefore advertises a report that the CLI does not produce.

### Implementation

1. Move the README report design into the real renderer.
   - Use the logo background colour `#11213b`.
   - Package and embed `docs/assets/tokenlens-logo-dark.png` as the large horizontal report logo.
   - Keep the small top-left identity only if it does not compete with the main logo.
   - Remove theme-switching controls.
   - Remove the "Estimates describe..." disclaimer block.
   - Keep `tokenlens-for-azure · created by Tzahi Ariel` as the small footer watermark.
   - Preserve responsive and print styles.
2. Replace the hand-maintained demo with generated output.
   - Add a rich synthetic multi-deployment fixture.
   - Add a deterministic script or test helper that runs the actual analyzer and writes `docs/tokenlens-report-demo.html`.
   - Regenerate the README PNG from that HTML.
   - Use a new preview filename when the screenshot changes to avoid GitHub image caching.
3. Keep all report formats aligned.
   - HTML, text, JSON, and SARIF must expose the same deployment identifiers and impact values.
   - Internal confidence fields may remain in the rule engine only if needed, but must not appear in customer output.
4. Escape every trace-derived value inserted into HTML.

### Acceptance criteria

- Running the documented CLI command produces a report visually equivalent to the README preview.
- The generated HTML contains the large horizontal logo and navy background.
- The report contains no `confidence`, `Data quality`, `Not evaluated`, or theme-switch text.
- The watermark is present.
- The demo HTML is generated from the same renderer used by the CLI.

## Phase 2: Add multi-deployment analysis

### Data model

Extend `TraceRecord` with explicit optional fields:

- `deployment_name`: the Azure/Foundry deployment identifier used by the request;
- `model_name`: the underlying model family/version returned by the service;
- `provider`: default to `azure_foundry` only when the source proves it;
- `resource_name` and `project_name`: optional grouping context;
- retain the existing `model` field temporarily for backward compatibility.

Normalize with documented precedence:

1. explicit top-level fields;
2. request fields;
3. metadata fields;
4. request `model` as a deployment-name fallback;
5. response `model` as the underlying model-name fallback;
6. `"unknown"` only when no source supplies the value.

Never merge deployments solely because they use the same model type.

### Analysis model

Add structures such as:

- `DeploymentSummary`;
- `DeploymentAnalysis`;
- `AnalysisReport.deployments`.

Each deployment summary must include:

- deployment name;
- model name/type;
- request count and share of total requests;
- input, output, cached, and total tokens;
- share of total tokens;
- average tokens per request;
- retry count;
- average or percentile latency when available;
- addressable token range and percentage;
- findings generated from only that deployment's records.

The overall analysis must still run once across the complete trace set. Per-deployment analysis must use the same rule engine without copying rule logic. Prevent recursive report construction by extracting a reusable summary/finding analysis function.

### Report UX

Add:

1. an "All deployments" portfolio summary;
2. a deployment comparison table sorted by total tokens descending;
3. a token-distribution visual using accessible HTML/CSS bars;
4. per-deployment sections with usage, impact, and Azure actions;
5. an explicit "Unknown deployment" bucket when necessary;
6. clear distinction between deployment name and model type.

Do not hide small deployments. Allow future filtering, but include every observed deployment in this version.

### Capture support

Add a supported Foundry capture example rather than relying on an ad hoc `test.py`:

- `examples/capture_foundry.py` or a small importable helper;
- Entra ID authentication with `DefaultAzureCredential`;
- one JSON object appended per completed call;
- deployment name taken from the request argument;
- underlying model name taken from the response;
- usage, timestamp, request ID, latency, status, workload, and tenant fields;
- optional tools, retrieval chunks, and retry linkage;
- no credentials or bearer tokens written to traces;
- clear success output showing the trace path and number of records.

Support multiple deployments through repeated real application calls or a configurable deployment list. Do not call every deployment merely to populate the report.

### Tests

- Normalize Azure request deployment and response model separately.
- Analyze at least three deployments, including two deployments of the same model.
- Verify totals reconcile exactly between deployment rows and the overall summary.
- Verify findings are isolated to the relevant deployment.
- Verify unknown deployments remain visible.
- Verify JSON, SARIF, text, and HTML contain deployment identifiers.
- Verify HTML escaping for deployment and model names.

## Phase 3: Simplify installation and first run

### Packaging

1. Keep the core offline dependencies minimal.
2. Add an optional dependency group:

   ```toml
   foundry = ["openai>=<supported-version>", "azure-identity>=<supported-version>"]
   ```

3. Confirm installation works with Python 3.11, 3.12, and 3.13 before expanding support.
4. Prefer `pipx install "tokenlens-azure[foundry] @ git+https://..."` for users.

### Activation-free quick start

The primary README path must avoid `source` and shell-specific activation:

```bash
git clone https://github.com/zakarel/tokenlens-for-azure.git
cd tokenlens-for-azure
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,foundry]"
.venv/bin/tokenlens-azure --help
```

Provide separate Windows PowerShell commands. Mention `. .venv/bin/activate` only as optional convenience for POSIX shells.

### Guided commands

Add:

- `tokenlens-azure doctor` to check Python support, write permissions, optional Foundry dependencies, relevant environment variables, and Azure credential availability without exposing secrets;
- `tokenlens-azure init-foundry` to create a safe trace directory, a `.gitignore` entry or instructions, and a copyable capture example;
- `--open` for HTML reports using Python's cross-platform `webbrowser` module;
- explicit terminal output after generation: absolute report path, trace count, deployment count, and whether the browser-open request succeeded.

Do not silently create an authenticated client and exit. The capture example must perform a request only when the user explicitly supplies a prompt/deployment and must print a clear outcome.

### Documentation troubleshooting

Cover these exact first-run issues:

- repository location and paths containing spaces;
- `python` versus `python3`;
- no virtual-environment activation required;
- zsh activation alternative;
- API-key authentication disabled;
- `az login` and required role for Entra ID;
- endpoint format and deployment name;
- client creation produces no output unless a request is sent;
- `open` may be silent on macOS;
- report path verification and `--open`;
- too few traces may legitimately produce few findings.

### Acceptance criteria

- A new macOS/zsh user can install, capture one request through Entra ID, generate a report, and open it by following one README path.
- The same core flow has Windows PowerShell instructions.
- No step requires storing an API key.
- No step requires activating a virtual environment.

## Phase 4: Timestamp report filenames

Create one filename helper with an injectable clock.

Default HTML output:

```text
tokenlens-report-YYYYMMDD-HHMMSSZ.html
```

Default comparison output:

```text
tokenlens-comparison-YYYYMMDD-HHMMSSZ.html
```

Rules:

- Use UTC and include `Z`.
- Replace characters that are invalid on Windows.
- Never overwrite an existing file; add `-2`, `-3`, and so on when necessary.
- An explicit full `--output` path remains an override for scripts needing a stable filename.
- If `--output` is omitted for HTML, write the timestamped file instead of dumping HTML to the terminal.
- Text output without `--output` remains on stdout.
- Print the absolute generated path.
- `--open` opens the generated file.

Test the filename helper with a fixed clock, collisions, explicit output, paths with spaces, and Windows-safe characters.

## Phase 5: Automated report generation

### CLI support

Automation must be a thin wrapper around the same CLI, not a separate analysis path.

Add machine-friendly behaviour:

- stable exit codes;
- absolute generated report path;
- optional `--quiet`;
- optional `--output-dir`;
- action output containing `report-path`;
- no interactive prompts in scheduled runs.

### GitHub Actions

Update the README with two workflows:

1. pull-request regression analysis;
2. scheduled HTML generation using `schedule` and `workflow_dispatch`.

The scheduled example must:

- obtain traces from an explicit secure source or run on a self-hosted runner where traces already exist;
- never commit raw traces;
- upload only the HTML/JSON report as a private workflow artifact;
- set a short `retention-days`;
- use least-privilege permissions;
- avoid printing prompts or tokens to logs;
- expose the generated timestamped report path from the composite action.

Update `action.yml` so the output filename is timestamped and returned as an action output. Remove shell command-substitution patterns that make argument handling brittle; construct arguments safely.

### Local scheduling

Document:

- macOS `launchd` as the preferred local option;
- Linux `cron`;
- Windows Task Scheduler.

Provide a checked-in activation-free wrapper script that:

1. resolves the repository directory;
2. invokes `.venv/bin/tokenlens-azure`;
3. analyzes a configured trace glob or directory;
4. writes to a reports directory;
5. logs only status and generated path;
6. returns a non-zero exit code on input or analysis failure.

Do not automatically upload or email reports.

### Trace retention

Document that users should:

- keep raw traces outside Git;
- redact or hash tenant identifiers;
- define trace and report retention;
- restrict file permissions;
- avoid collecting secrets, access tokens, or unnecessary prompt content;
- use a private runner when traces cannot leave the application environment.

## Recommended implementation order

1. Add tests that demonstrate the report-design mismatch and multi-deployment requirements.
2. Refactor analysis into reusable overall and per-deployment aggregation.
3. Extend ingestion and models with deployment identity.
4. Replace the generated HTML renderer with the authoritative navy design.
5. Generate the demo HTML and README preview from the real renderer.
6. Add timestamped output handling and `--open`.
7. Add `doctor`, optional Foundry dependencies, and the supported capture example.
8. Update the composite GitHub Action and add scheduled automation examples.
9. Rewrite installation, real-data capture, troubleshooting, and automation sections in the README.
10. Run the complete test suite and a real activation-free smoke test.

## Expected files to add or change

- `src/tokenlens/models.py`
- `src/tokenlens/ingest.py`
- `src/tokenlens/analyzer.py`
- `src/tokenlens/reports.py`
- `src/tokenlens/cli.py`
- `src/tokenlens/output.py` for timestamped paths
- `src/tokenlens/foundry.py` only if a reusable capture helper is justified
- `examples/capture_foundry.py`
- `examples/multi-deployment-requests.jsonl`
- `scripts/generate-demo-report.py`
- `scripts/run-scheduled-report.sh`
- `tests/test_ingest.py`
- `tests/test_deployments.py`
- `tests/test_reports.py`
- `tests/test_cli.py`
- `pyproject.toml`
- `action.yml`
- `.github/workflows/ci.yml`
- `README.md`
- generated files under `docs/`

Do not force this exact module split when a smaller, clearer structure fits the codebase, but keep ingestion, analysis, output naming, rendering, and optional Foundry integration separable.

## Final validation checklist

- Full test suite passes.
- Package installs from a clean Python environment.
- Activation-free CLI works.
- Entra-authenticated capture example writes a valid trace without secrets.
- A fixture containing multiple deployments produces reconciled overall and per-deployment totals.
- Generated HTML matches the README preview.
- Generated HTML uses a timestamped filename.
- `--open` opens the generated report or reports a clear error.
- Scheduled workflow syntax is valid.
- Raw traces and local credentials are not committed.
- User-owned `.gitignore` and `test.py` changes remain untouched unless explicitly approved.

