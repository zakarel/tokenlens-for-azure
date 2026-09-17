# Pricing resolution, catalogs, and the deferred public sync

TokenLens never guesses a rate. This document states exactly where a rate can
come from, what is deliberately not implemented, and why.

## Resolution order

1. **Observed per-call cost** — a cost the provider itself returned.
2. **Customer catalog** — an exact contracted rate you recorded.
3. **Synchronized verified public catalog** — *not yet available, see below*.
4. **Packaged verified catalog** — the dated snapshot shipped with TokenLens.
5. **Unresolved** — the token volume stays visible; the cost is withheld.

There is no nearest-model fallback, no model-family fallback, no currency
conversion, and no rate inferred from Azure quota or capacity. A versioned model
is distinct from its unversioned name, and a deployment mode is part of the
match: a Global Standard rate is never applied to a Data Zone deployment.

## Commands

| Command | Network | Purpose |
|---|---|---|
| `tokenlens-azure pricing status` | none | Which catalogs exist, how many entries, and when they were retrieved |
| `tokenlens-azure pricing verify` | none | Currency consistency, expiry, and confidence labelling |
| `tokenlens-azure pricing set-rate` | none | Record one exact contracted rate locally |
| `tokenlens-azure pricing sync` | **none — deferred** | Would synchronize verified public pricing |
| `tokenlens-azure foundry pricing` | none | Per-deployment readiness for the current selection |

## Why `pricing sync` is deferred

`tokenlens-azure pricing sync` prints its status, makes **no network request**,
and exits with code `2`.

A synchronized entry is only publishable when every one of these is true:

- the source is documented and machine-readable;
- a deterministic parser exists, with committed fixtures, rather than an
  HTML scrape driven by visual labels;
- the entry carries an exact canonical model, its aliases and version, the
  publisher, the deployment mode, the service tier, the currency, the billing
  basis, input/cached/output rates, an effective date, a retrieval timestamp,
  a source URL, and a content hash;
- the confidence can honestly be labelled `verified`;
- any conversion (for example a CCU-derived dollar equivalent) is explained and
  is not presented as an Azure token meter.

Shipping a synchronizer that cannot meet that bar would produce numbers
indistinguishable from guesses, which is the precise failure mode TokenLens
exists to prevent. The command is therefore deferred rather than approximated.

When it ships, the snapshot will be cached user-locally — not in the repository
— at `~/.config/tokenlens/pricing/public-snapshot.yml` (or the platform
application-data equivalent, or `TOKENLENS_CONFIG_DIR`), and analysis will keep
reading it offline.

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
credential, and every entry is labelled `customer_override` so the report can
say plainly that the rate is customer-provided.

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
| `Exact public rate` | A verified catalog entry matched exactly | none |
| `Customer override` | Your contracted rate matched exactly | none |
| `Model identified, rate unavailable` | Identity is exact; no rate exists | `pricing set-rate`, or continue without cost |
| `Identity unresolved` | Collection could not resolve the model | recollect or enrich; **never** a pricing override |
| `Currency mismatch` | The catalog currency differs from the report currency | choose one reporting currency |

`unknown` is never offered as a pricing override key.

## What the report shows

Cost analysis renders one decision state, its impact, and one action. The
affected model, the exact reason, the catalogs consulted, and the copyable
command live in a single collapsible remediation panel rather than being
repeated in every card. When no catalog entry matched, no source URL, retrieval
date, publisher, currency, or billing basis is displayed — showing them would
imply the catalog priced the workload.
