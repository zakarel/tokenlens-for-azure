# TokenLens for Azure: Report UX and Materiality Action Plan

## Objective

Turn the current long-form report into a concise executive one-pager with a separate, interactive usage-analytics view. The generated application and README example must remain identical because both must be produced by the same report renderer.

This release focuses on four outcomes:

1. visual model/deployment usage analytics;
2. customer-relevant materiality filtering;
3. a no-scroll desktop overview;
4. a realistic example portfolio using the requested model names.

## Repository state to preserve

- The working tree currently contains user-owned changes in `.gitignore` and an untracked `test.py`.
- Do not overwrite, stage, commit, or remove either file.
- Never commit `local-traces/` or any other real customer trace.
- Keep the analyzer offline and the generated HTML self-contained.

## Product decisions

### One self-contained report with two views

Use tabs inside one HTML report rather than separate files:

- **Overview**: the default executive one-pager.
- **Usage analytics**: charts, deployment/model tables, and detailed findings.

This preserves the current single-file sharing model while preventing the overview from becoming a long document. Tabs must work without network access or external JavaScript.

### Materiality of a 0.6% finding

A percentage alone is insufficient. In the current demo, `0.6%` represents only tens of thousands of tokens and is low-materiality relative to the portfolio. In a much larger workload, however, `0.6%` could represent millions of tokens and still deserve attention.

Therefore:

- do not delete low-percentage findings from the analysis;
- omit them from the executive overview when both percentage and absolute impact are immaterial;
- retain them in Usage analytics under **Additional opportunities**;
- always show both percentage and absolute token/call impact;
- explain materiality in customer language, not statistical jargon.

Default overview materiality rule:

```yaml
report:
  overview_min_impact_percent: 1.0
  overview_min_impact_tokens: 100000
  overview_max_findings: 3
```

A finding appears on Overview when either:

- `impact_max_percent >= 1.0`; or
- token-based `estimated_savings.max_tokens >= 100000`.

Findings without a quantified estimate, such as model-routing opportunities, may appear only when they are among the explicitly prioritised portfolio recommendations. The rule must be configurable and deterministic.

Impact labels:

| Maximum estimated impact | Customer label |
|---|---|
| `>= 10%` | Major |
| `>= 5%` and `< 10%` | High |
| `>= 1%` and `< 5%` | Moderate |
| `< 1%` | Low materiality |
| No quantified estimate | Evaluation opportunity |

Do not use confidence labels, data-quality scores, or "Not evaluated."

## Phase 1: Add a report presentation model

Do not embed filtering, ranking, and chart calculations directly inside HTML string construction.

Add a presentation layer, for example `src/tokenlens/presentation.py`, containing typed, testable helpers for:

- impact category;
- overview materiality;
- overview finding ranking;
- top recommendation selection;
- deployment and model aggregation;
- chart percentages;
- accessible chart labels;
- "additional opportunities" count.

### Aggregation requirements

Calculate both deployment-level and model-level rollups:

- deployment name;
- model name;
- request count;
- input tokens;
- output tokens;
- cached tokens;
- total tokens;
- percentage of portfolio tokens;
- average tokens per request;
- retry count;
- addressable token range.

Two deployments using the same model must remain separate in the deployment dataset and combine only in the model dataset.

All displayed percentages must reconcile to totals. Handle rounding so chart labels are honest; do not force rounded values to total exactly 100 if that would distort the underlying calculation.

### Tests

- `0.6%` and fewer than `100000` tokens is hidden from Overview and retained in details.
- `0.6%` and more than `100000` tokens remains visible on Overview.
- `1.0%` is visible at the boundary.
- findings with call-based impact use request counts rather than token thresholds.
- duplicate model names combine in model aggregation but not deployment aggregation.
- aggregate request and token totals reconcile exactly.
- empty, unknown, and single-deployment inputs remain valid.

## Phase 2: Build the one-page Overview

### Desktop target

The default Overview must fit without vertical scrolling at:

- `1440 x 900` CSS pixels;
- `1366 x 768` CSS pixels, allowing only minimal browser chrome variance.

It must also print to one landscape page.

### Overview content

Keep only information needed for a first decision:

1. compact TokenLens logo/header and report metadata;
2. five compact KPIs:
   - requests;
   - total tokens;
   - deployments;
   - addressable range;
   - material findings;
3. portfolio usage summary with four compact deployment rows;
4. top three material findings;
5. top three Azure actions;
6. navigation to Usage analytics;
7. small `tokenlens-for-azure · created by Tzahi Ariel` watermark.

Remove from Overview:

- repeated per-deployment finding sections;
- all low-materiality findings;
- verbose explanations;
- long source paths;
- technical details already available in Usage analytics.

If findings are hidden by materiality or the top-three cap, show one compact line:

> `N additional opportunities are available in Usage analytics.`

### Responsive behaviour

- Desktop: no-scroll one-pager.
- Tablet/mobile: content may stack and scroll; do not shrink text below readable sizes.
- Keyboard focus must be visible.
- Tabs need `role="tablist"`, `role="tab"`, `role="tabpanel"`, `aria-selected`, and keyboard navigation.
- With JavaScript disabled, both sections must remain readable rather than disappearing.

### Print behaviour

- Overview prints as one landscape page.
- Usage analytics begins on a separate printed page.
- Hide interactive tab controls in print.

## Phase 3: Add Usage analytics charts

### Chart approach

Use inline SVG and minimal inline JavaScript. Do not add D3, Chart.js, CDN dependencies, canvas, or network calls.

Add two charts:

1. **Donut chart: token share by model**
   - segments use the model aggregation;
   - legend shows model, total tokens, and percentage;
   - centre label shows portfolio total tokens;
   - tooltips/focus labels provide exact values;
   - include an adjacent accessible data table.

2. **Column chart: token usage by deployment**
   - one column per deployment;
   - stacked input/output/cached tokens when values exist;
   - deployment label and model name;
   - exact values available by hover, focus, and accessible table;
   - sort by total tokens descending.

Use a colour-safe palette with sufficient contrast on `#11213b`. Do not rely on colour alone: use labels, patterns, borders, or direct values.

### Usage analytics content

The tab contains:

- the donut chart;
- the stacked deployment column chart;
- model summary table;
- deployment summary table;
- all material findings;
- **Additional opportunities** for low-materiality findings;
- per-deployment findings in collapsible native `<details>` elements;
- a short explanation:
  - percentage is relative to the relevant deployment or portfolio;
  - absolute token/call volume is shown alongside it;
  - low-materiality does not mean invalid, only lower priority.

### Acceptance criteria

- Charts render with no external resources.
- Chart values match JSON output exactly.
- Two deployments sharing one model produce two columns but one donut segment.
- Every chart has a readable accessible table.
- Analytics remains usable when there are more than four deployments.
- Long model and deployment names do not overlap or overflow.

## Phase 4: Replace the example portfolio models

Keep exactly `7,548` synthetic requests across four distinct deployments.

Use these requested model display names:

1. `Claude-opus-5`
2. `gpt-5.6-luna`
3. `claude-opus-5`
4. `claude-fable-5.1`

The repeated `Claude-opus-5` / `claude-opus-5` entries should demonstrate separate deployments using the same model family. Preserve the requested display spelling in the report, but add a canonical model key for aggregation so case differences do not create misleading duplicate donut slices.

Recommended fixture mapping:

| Deployment | Display model | Requests |
|---|---|---:|
| `reasoning-prod` | `Claude-opus-5` | 3,220 |
| `general-prod` | `gpt-5.6-luna` | 2,160 |
| `reasoning-batch` | `claude-opus-5` | 1,680 |
| `creative-prod` | `claude-fable-5.1` | 488 |
| **Total** |  | **7,548** |

Implementation rules:

- use deterministic generation rather than manually maintaining 7,548 JSON lines;
- prefer a small generator script plus a generated fixture only if tests require the full file;
- vary prompts, usage, output limits, retries, and optional cached tokens enough to avoid every request appearing identical;
- keep all data clearly synthetic;
- ensure two Opus deployments remain distinct while their canonical model rollup combines them;
- do not imply these model names are actual Microsoft Foundry catalogue availability or measured production results.

## Phase 5: Keep README and application synchronized

### Single source of truth

The actual `report_html()` output remains authoritative.

Update the demo pipeline so it:

1. generates the 7,548-request synthetic portfolio;
2. analyzes it with the production analyzer;
3. writes `docs/tokenlens-report-demo.html`;
4. opens the Overview tab at `1440 x 900`;
5. captures a new PNG using a cache-busting filename;
6. updates README to the new PNG.

Do not hand-edit the demo HTML after generation.

### README changes

- Show only the one-page Overview image.
- Clicking it opens the enlarged image.
- Add a second smaller image or direct link labelled **View usage analytics**.
- Explain the two report views in two concise bullets.
- Explain materiality:
  - Overview prioritises material findings;
  - Usage analytics retains lower-impact opportunities.
- State that the example is synthetic and contains 7,548 requests.
- Use the four requested model display names.
- Keep installation, capture, automation, and privacy instructions intact.

### Generated report navigation

- Default active tab is Overview.
- Support URL fragments such as `#overview` and `#usage`.
- The README analytics link should open `docs/tokenlens-report-demo.html#usage`.
- Preserve the selected tab when printing only if it does not compromise complete print output.

## Phase 6: Configuration and output consistency

Extend `.tokenlens.yml` support for:

```yaml
report:
  overview_min_impact_percent: 1.0
  overview_min_impact_tokens: 100000
  overview_max_findings: 3
```

The current CLI loads configuration but may not apply it. Wire the report configuration through analysis/presentation/rendering rather than documenting inactive settings.

Apply materiality only to presentation:

- JSON and SARIF retain all real findings;
- text output identifies low-materiality findings but may group them after material ones;
- Overview filters and caps findings;
- Usage analytics includes all findings;
- analysis totals and addressable ranges remain unchanged.

Add report metadata describing the applied materiality thresholds to JSON without exposing internal confidence values.

## Expected files to change

- `src/tokenlens/models.py`
- `src/tokenlens/analyzer.py`
- `src/tokenlens/reports.py`
- `src/tokenlens/cli.py`
- `src/tokenlens/presentation.py` or an equivalent focused module
- `examples/multi-deployment-portfolio.jsonl` or its generator
- `scripts/generate-demo-report.py`
- `tests/test_deployments.py`
- new report/presentation tests
- `README.md`
- `.tokenlens.yml` example
- generated demo HTML and PNG assets

Avoid unrelated refactoring.

## Required validation

1. Run the full test suite.
2. Verify exactly 7,548 fixture requests.
3. Verify deployment request totals are `3220 + 2160 + 1680 + 488`.
4. Verify the two Opus deployment columns remain separate.
5. Verify the Opus model donut aggregation combines case variants.
6. Verify chart totals equal report totals.
7. Verify a `0.6%` low-volume finding is absent from Overview and present in Usage analytics.
8. Verify generated HTML contains no external scripts, stylesheets, fonts, or images.
9. Verify the Overview has no vertical scroll at `1440 x 900` and `1366 x 768`.
10. Verify keyboard tab navigation and URL fragments.
11. Verify Overview prints on one landscape page.
12. Verify README image and actual generated Overview match.
13. Verify no `confidence`, `Data quality`, or `Not evaluated` customer-facing text appears.
14. Verify user-owned `.gitignore`, `test.py`, and private traces remain untouched.

## Definition of done

- The HTML opens on a one-page Overview.
- Usage analytics provides a donut chart and stacked deployment column chart.
- Low-materiality findings do not distract from Overview but remain discoverable.
- The example shows 7,548 requests and the four requested model labels.
- README and generated application are based on the same renderer.
- All tests and visual checks pass.
- Only task-related files are committed and pushed.

