# Pricing: assumptions, official sources, and provenance

TokenLens never guesses a rate. This document states exactly which pricing
dimensions are assumed, where a rate may come from, and what is deliberately
refused.

## Default assumptions

Azure Foundry publishes a separate meter for every purchasing dimension.
Telemetry and deployment metadata usually resolve only some of them. Where a
dimension is not stated, TokenLens applies one documented default set:

> **Pricing assumptions: Retail · Global · Standard · Short context · Normal inference**

| Dimension | Default | Meaning |
|---|---|---|
| Purchase model | `retail` | List pricing, not an enterprise agreement or private offer |
| Deployment | `global` | Global Standard, not data zone and not regional |
| Service tier | `standard` | Not batch, priority (PP), flex, or provisioned |
| Context window | `short` | The short-context meter, not the long-context meter |
| Inference mode | `normal` | Not fine-tuned, hosted, media, tool, or session metering |

Every value above is normalized once, by a single canonicalizer, and both the
Azure and the Claude source consume the canonical result. `Data Zone`,
`data-zone`, and `DZ` are the same dimension; `medium` is not a context window
and is rejected before any request is made. An *unstated* dimension (`unknown`,
empty) is a fact about the telemetry and falls back to the default; an
*unrecognized* one is a caller error and fails loudly.

These defaults are **never hidden**. They are rendered in bold at the top of
the report's Cost analysis tab, repeated in the pricing provenance disclosure,
carried in `report_metadata.pricing.assumptions`, echoed by
`tokenlens-azure pricing status` and `tokenlens-azure pricing sync`, and shown
in the guided workflow before collection starts.

An exact observed or configured dimension always wins over a default. A
deployment discovered as Data Zone Standard is priced from data-zone meters or
not at all — a Global Standard rate is never applied to it.

Two different facts are reported separately:

| Field | Meaning |
|---|---|
| `assumed_dimensions_applied` | Defaults that were applied by a rate which **actually priced a record in this report** |
| `catalog_assumed_dimensions_available` | Defaults present anywhere in the consulted catalogs, whether or not they were used |

An observed per-call cost, or a customer rate that states every dimension,
applies **no** assumption, and the report says so rather than inheriting a
catalog-wide list.

### The context window is resolved, not assumed away

A synchronized context-scoped rate carries the **documented** prompt-token
boundary for its model in `context_length_min` / `context_length_max`:

| Meter | Band | Applies when |
|---|---|---|
| GPT-5.6 Luna, short context | `0`–`127,999` | the prompt is below 128K tokens |
| GPT-5.6 Luna, long context | `128,000`+ | the prompt is 128K tokens or larger |

Resolution picks the band from the record's own context length, so a
multi-context synchronization can never *silently* select the long-context
rate. The boundary is declared per model on the crosswalk entry. A model with
no documented boundary never publishes a long-context rate at all: the meter is
quarantined as `context-boundary-undocumented`, because a long rate that cannot
be separated from the short one is indistinguishable from a guess.

When the telemetry cannot state a per-request prompt size — an Azure Monitor
bucket is an interval total, not one prompt — the configured default (`short`)
applies and `context=short` is recorded in the resolution's applied
assumptions. A prompt size that *is* known and falls inside a published band is
exact, and adds no assumption.

If only the short band was synchronized and a prompt exceeds it, cost is
withheld for those requests rather than billed at the wrong meter. Synchronize
both bands with `--context short --context long`.

## Resolution order

1. **Observed per-call cost** — a cost the provider itself returned.
2. **Customer catalog** — an exact contracted rate you recorded.
3. **Cached public sync** — a snapshot synchronized from an official source.
4. **Packaged catalog** — the dated snapshot shipped with TokenLens.
5. **Unresolved** — the token volume stays visible; the cost is withheld.

There is no nearest-model fallback, no model-family fallback, no currency
conversion, and no rate inferred from Azure quota or capacity. A versioned model
is distinct from its unversioned name, and the deployment mode is part of the
match.

Entries synchronized from an official source carry `source_rank = 10` and
therefore outrank the packaged catalog's `source_rank = 100` for the same model
and mode. A customer override outranks both.

## Official sources

| Source | URL | Scope |
|---|---|---|
| Azure Retail Prices API | `https://prices.azure.com/api/retail/prices` | `serviceName eq 'Foundry Models'` token meters |
| Anthropic published pricing | `https://platform.claude.com/docs/en/about-claude/pricing` | Claude token rates, converted to consumption units |

Both are allow-listed by **exact host and exact path** — on the first request,
on **every redirect hop**, and on every pagination continuation the API
returns. Prefix matching is not used, a non-`https` scheme is refused (so an
`https` → `http` downgrade redirect cannot be followed), a non-443 port is
refused, embedded credentials are refused, and a repeated URL terminates the
walk rather than looping. The URL a response actually came from is re-validated
and stored on the snapshot as its `source_url`.

Every run is bounded: wall-clock timeout, maximum pages, maximum items,
maximum response bytes, and a bounded retry count with short backoff. An
allow-list refusal and a budget refusal are never retried — repeating them
would only repeat the refusal.

The Azure query is additionally bounded **server-side** by the product families
the crosswalk can price, and optionally by one ARM region:

```text
serviceName eq 'Foundry Models'
  and armRegionName eq 'eastus2'
  and (productName eq 'Azure Mistral Models' or productName eq 'Azure OpenAI GPT5')
```

so an ordinary synchronization reads a single page and stays far inside the
budget instead of walking the entire Foundry Models price list.

### A truncated feed is a failure, not a result

If a page, item, or byte ceiling is reached while the source still has more to
give, the walk raises rather than returning what it happened to read. The
synchronization then reports `status=failed` with `feed=truncated`, **nothing
is written to the cache**, the previous complete snapshot stays in effect, and
nothing is labelled `verified`.

This distinction matters: "we stopped reading" is not evidence that a meter
does not exist. A model that could not be resolved from a truncated feed is
quarantined as `feed-truncated-model-unresolved`, never as
`no-normal-inference-meter-published`.

Synchronization happens only in `tokenlens-azure pricing sync` and, when the
cache is absent or stale, once inside the guided `tokenlens-azure foundry`
workflow. **Analysis is always offline** — it reads the cached snapshot and
never opens a connection. Set `TOKENLENS_NO_PRICING_SYNC=1` to disable the
guided attempt entirely (air-gapped environments and CI).

A synchronization failure is never fatal. The previous snapshot stays in place,
the packaged catalog and any customer rates still apply, and the run continues.

## The local snapshot cache

Snapshots live outside the repository, in the user-local application-data
directory (`~/.config/tokenlens/pricing`, the platform equivalent, or
`TOKENLENS_CONFIG_DIR`):

```
pricing/azure-retail-foundry.json
pricing/claude-pricing-docs.json
pricing/pricing-sources.json
```

They are written atomically (temporary file, `fsync`, `chmod 0600`, atomic
replace) into a `0700` directory, contain no credential, and record the final
validated source URL, API version, retrieval timestamp, SHA-256 content hash,
pages read, rows read, the billing currency, the feed-complete flag, the
assumptions in force, the resulting catalog, and every quarantined row. A
corrupt or foreign-schema file is ignored rather than trusted, and an
incomplete snapshot is refused by the writer itself.

`pricing-sources.json` records which sources the last synchronization actually
requested. Freshness is judged against **that** set, so a user who syncs with
`--no-claude` is not told forever that a synchronization is overdue, and the
guided workflow only requires the Claude source when a Claude deployment is
actually selected.

The freshness budget is seven days. A stale snapshot is still applied — it is
only a reason to refresh, never a reason to discard a verified rate.

### Currency

A catalog carries exactly one currency, and TokenLens never converts. Snapshots
in different currencies are therefore **never merged**: only those matching the
reporting currency are used, and any that are excluded are reported with the
reason through `public_pricing_provenance` and `pricing status`. The packaged
catalog is never dropped to make room for a foreign-currency snapshot.

Anthropic publishes Claude pricing in **USD only**. A non-USD synchronization
excludes Claude with a stated reason rather than converting it:

```text
source=claude_pricing_docs status=skipped
  reason=Anthropic publishes Claude pricing in USD only, and TokenLens never converts
  currencies, so it is excluded from this EUR synchronization.
```

## The Azure Retail Prices crosswalk

The parser is deterministic and closed. A row is priced only when **all** of
this holds:

- `serviceName` is `Foundry Models`, `type` is `Consumption`, there is no
  reservation term, and `tierMinimumUnits` is zero;
- `meterName` ends in `Tokens`;
- an explicit entry in the model crosswalk matches the meter label — for
  example the deployment model `gpt-5.6-luna` matches the published labels
  `5.6 luna`, `56 luna`, and `56luna`, and `Ministral-3B` matches
  `Ministral 3B` and `Mnstrl 3b`;
- **every remaining token in the label is a token TokenLens recognizes**. An
  unknown token quarantines the row instead of producing a rate;
- the priced dimension (input, output, cached input, cache write), the
  deployment, the context window, the service tier, and the inference mode each
  resolve unambiguously.

Excluded by construction, from `meterName`, `skuName`, **and** `armSkuName`:
fine-tuning (`-FT`, so `Ministral 3B Model In-FT` never prices normal
inference), batch, priority (`PP`), flex, provisioned and PTU, hosted
deployment units (`1/Hour`), media, tool, and session meters, plus data-zone and
regional meters while the global default is in force.

`unitOfMeasure` is converted exactly: `1K` is multiplied to a per-million rate,
`1M` is used directly. A composite unit such as `1/Hour` is refused outright
rather than read as "one token".

Where the same global meter is republished per region, the duplicates are
collapsed **only after verifying the rates are identical**. If they differ, the
account's own region decides; without one, the dimension is quarantined rather
than averaged. Provenance keeps the contributing regions.

Each entry records the `meterId` of every dimension it used, the content hash,
the retrieval timestamp, the source URL, and the effective date. A future
effective date is not applied early. An input rate without a matching output
rate is quarantined as an incomplete meter set — never half-priced.

### When a model has no normal-inference meter

Some models are published in the retail feed **only** with fine-tuning meters.
`Ministral-3B` is currently one of them: every `Ministral 3B` / `Mnstrl 3b`
token meter carries an `-FT` marker. TokenLens refuses to price it from a
fine-tuning rate and instead records:

```text
quarantined  no-normal-inference-meter-published  ministral-3b
             published-only-as=fine-tuning; requested=global/short-context
```

Cost stays unresolved for that deployment, usage and PTU analysis continue, and
the remediation is a contracted rate you supply.

## Claude on Foundry

Foundry bills Claude in Anthropic consumption units (CCU). TokenLens parses the
official published US-dollar token rates and converts them at the fixed Azure
rate of **0.01 USD per CCU (100 CCU = $1)**:

| Claude Opus 5 | USD / MTok | CCU / MTok |
|---|---|---|
| Base input | $5.00 | 500 |
| 5-minute cache write | $6.25 | 625 |
| 1-hour cache write | $10.00 | 1000 |
| Cache hits & refreshes | $0.50 | 50 |
| Output | $25.00 | 2500 |

The billing basis is labelled `claude_ccu_equivalent`, never `token_rate`, so
the report can say plainly that this is a CCU-derived dollar-equivalent
estimate and not an Azure token meter. The report shows the estimated USD token
cost and can report the CCU equivalent alongside it.

Deployment multipliers: **Global standard is 1.0x**. The **US data zone 1.1x**
premium is applied **only** when the deployment mode is exactly data-zone — it
is never applied under the global default assumption.

A mode that was never stated stays unstated: the global baseline is applied and
`deployment=global` is recorded as an assumption. A mode that was explicitly
configured — `global` or `data_zone` — is exact and adds no assumption.

Deployment modes are handled **per mode, never all-or-nothing**. Claude is
offered on Foundry for `global` and `data_zone` only. Requesting
`--deployment-mode global --deployment-mode regional` publishes the global
rates and quarantines `regional` as `deployment-mode-not-published`.
Requesting `regional` alone produces a *completed* snapshot with zero entries,
a quarantine record, and a stated reason:

```text
source=claude_pricing_docs status=ok entries=0 quarantined=1
  published=none; the read completed and nothing matched the request
  reason=No Claude rate was published: the requested deployment mode(s) regional
  are not offered for Claude on Foundry. Supported modes: data_zone, global.
```

The guided workflow derives Claude's modes from **Claude deployments only**, so
a data-zone GPT deployment elsewhere in the portfolio can never add a data-zone
premium to Claude.

The provenance note quotes CCU figures derived from the rate **actually stored
on the entry**, after the multiplier, alongside the published base rates, so
the explanation can never disagree with the arithmetic. For Claude Opus 5 in a
US data zone the note reads `input 550 CCU/MTok` (5 USD × 1.1 = 5.5 USD), not
the unmultiplied 500.

**Customer-specific private discounts are not included.** Record them with
`pricing set-rate` if you have them.

The Claude parser is strict. If the published table's columns are renamed,
merged, or stop being `$N / MTok` values, it raises a drift error, no snapshot
is written, the previous cached snapshot stays in use, and no rate is
fabricated.

## Commands

| Command | Network | Purpose |
|---|---|---|
| `tokenlens-azure pricing status` | none | Catalogs, snapshots, hashes, freshness, assumptions |
| `tokenlens-azure pricing verify` | none | Currency consistency, expiry, confidence labelling |
| `tokenlens-azure pricing set-rate` | none | Record one exact contracted rate locally |
| `tokenlens-azure pricing sync` | allow-listed, bounded | Synchronize official public pricing into the local cache |
| `tokenlens-azure foundry pricing` | none | Per-deployment readiness for the current selection |

`pricing sync` options: `--currency`, `--region`, `--deployment-mode`
(repeatable: `global`, `data_zone`, `regional`), `--context` (repeatable:
`short` or `long`), and `--claude/--no-claude`. An unsupported dimension value
exits `1` before anything is contacted or written. The command exits `3` when a
source failed or was truncated, and the report names which one and what is
still in effect. A source that was deliberately skipped is reported as
`status=skipped` and does not make the run a failure.

## Recording a contracted rate

```bash
tokenlens-azure pricing set-rate \
  --model YOUR_MODEL \
  --input-per-million 1.25 \
  --cached-input-per-million 0.31 \
  --output-per-million 5.00 \
  --deployment-mode global \
  --effective-from 2026-01-01 \
  --billing-basis token_rate \
  --note "Negotiated enterprise agreement"
```

The values are echoed and confirmed before anything is written. The catalog is
stored with user-only permissions outside the repository, contains no
credential, and every entry is labelled `customer_override`.

A rate in a second currency is **rejected**, not converted:

```text
error=The customer catalog is denominated in USD. TokenLens never converts
currencies, so a EUR rate cannot be added to it.
```

## Readiness states

`tokenlens-azure foundry pricing` reports identity and price coverage as
separate states, because they are separate problems:

| State | Meaning | Remediation |
|---|---|---|
| `Exact public rate` | An official or packaged entry matched exactly | none |
| `Customer override` | Your contracted rate matched exactly | none |
| `Model identified, rate unavailable` | Identity is exact; no rate exists | `pricing set-rate`, or continue without cost |
| `Identity unresolved` | Collection could not resolve the model | recollect or enrich; **never** a pricing override |
| `Currency mismatch` | The catalog currency differs from the report currency | choose one reporting currency |

`unknown` is never offered as a pricing override key.

The guided workflow never asks what to do about an unresolved rate. There is
only one safe answer, so it states the reason, continues the full assessment
with cost withheld for those deployments only, and prints one remediation
command.

## What the report shows

Cost analysis opens with the bold assumptions line and its override warning,
then one decision state, its impact, and one action. The affected model, the
exact reason, the catalogs consulted, the synchronized sources with their
retrieval timestamps and content hashes, and the copyable command live in
collapsible panels rather than being repeated in every card.

When no catalog entry matched, no source URL, retrieval date, publisher,
currency, or billing basis is displayed — showing them would imply the catalog
priced the workload. The assumptions banner is still shown, because it
describes the policy that was applied, not a result.

Estimates are advisory. Agreements, private offers, regional availability, and
later price changes may differ from any published rate.
