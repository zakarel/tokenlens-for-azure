"""Short and long context meters must be distinguishable at resolution time.

A synchronized context-scoped rate carries the documented prompt-token boundary
in ``context_length_min``/``context_length_max``. Resolution then picks by the
record's own context length, falls back to the configured default only when the
telemetry cannot state one, and says so when it does.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tokenlens.analyzer import analyze
from tokenlens.ingest import iter_records
from tokenlens.pricing import PriceEntry, PricingCatalog, resolve_trace_cost
from tokenlens.pricing_sources.azure_retail import (
    MODEL_CROSSWALK,
    crosswalk_for,
    crosswalk_rows,
    rows_from_payload,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
AS_OF = date(2026, 9, 22)
WHEN = datetime(2026, 9, 15, tzinfo=UTC)
LUNA_THRESHOLD = 128_000


def _rows(*names: str):
    rows = []
    for name in names:
        rows.extend(rows_from_payload(json.loads((FIXTURES / name).read_text(encoding="utf-8"))))
    return rows


def _entry(entries, model: str, context: str):
    return next(item for item in entries if item.model == model and item.context_window == context)


def both_contexts() -> PricingCatalog:
    """A synchronized catalog holding the short *and* long Luna meters."""
    result = crosswalk_rows(
        _rows("azure_retail_page1.json"), contexts=["short", "long"], as_of=AS_OF
    )
    return PricingCatalog(
        catalog_name="public-pricing-sync",
        retrieved_at=AS_OF,
        pricing_basis="analysis_date_snapshot",
        prices=result.entries,
    )


def record(*, input_tokens: int, aggregate: bool = False, model: str = "gpt-5.6-luna"):
    raw = {
        "timestamp": WHEN.isoformat(),
        "deployment_name": "prod",
        "model_name": model,
        "deployment_mode": "global",
        "messages": [{"role": "user", "content": "synthetic"}],
        "usage": {"input_tokens": input_tokens, "output_tokens": 100},
        "status_code": 200,
    }
    if aggregate:
        raw["metadata"] = {"record_type": "foundry_metric_bucket"}
    return next(iter_records(io.StringIO(json.dumps(raw) + "\n")))


# --- The documented boundary ------------------------------------------------


def test_the_boundary_is_declared_per_model_not_inferred():
    assert crosswalk_for("gpt-5.6-luna").long_context_threshold_tokens == LUNA_THRESHOLD
    # Mistral publishes no context split for this model.
    assert crosswalk_for("ministral-3b").long_context_threshold_tokens is None
    assert all(
        entry.long_context_threshold_tokens is None or entry.long_context_threshold_tokens > 0
        for entry in MODEL_CROSSWALK
    )


def test_synchronized_context_entries_carry_the_boundary():
    entries = crosswalk_rows(
        _rows("azure_retail_page1.json"), contexts=["short", "long"], as_of=AS_OF
    ).entries
    short = _entry(entries, "gpt-5.6-luna", "short")
    long_context = _entry(entries, "gpt-5.6-luna", "long")
    assert (short.context_length_min, short.context_length_max) == (0, LUNA_THRESHOLD - 1)
    assert (long_context.context_length_min, long_context.context_length_max) == (
        LUNA_THRESHOLD,
        None,
    )
    # The bands are adjacent and never overlap.
    assert short.context_length_max + 1 == long_context.context_length_min
    assert "0–127,999 prompt tokens" in short.note
    assert "from 128,000 prompt tokens" in long_context.note


def test_a_model_without_a_documented_boundary_never_publishes_a_long_rate():
    # Ministral's synthetic feed has no long meter, so synthesize the situation
    # a model with an undocumented boundary would produce.
    rows = _rows("azure_retail_page2.json")
    for row in rows:
        object.__setattr__(row, "meter_name", row.meter_name.replace("Model In", "Model LongCo In"))
        object.__setattr__(
            row, "meter_name", row.meter_name.replace("Model Out", "Model LongCo Out")
        )
    result = crosswalk_rows(rows, contexts=["long"], as_of=AS_OF)
    assert result.entries == []
    assert "context-boundary-undocumented" in {item.reason for item in result.quarantined}
    assert any("no documented prompt-token boundary" in note for note in result.notes)


def test_a_multi_context_sync_publishes_both_bands_or_neither():
    result = crosswalk_rows(
        _rows("azure_retail_page1.json"), contexts=["short", "long"], as_of=AS_OF
    )
    luna = [item for item in result.entries if item.model == "gpt-5.6-luna"]
    assert {item.context_window for item in luna} == {"short", "long"}
    # Every published context-scoped entry is bounded, so none can be selected
    # by accident.
    assert all(item.context_length_min is not None for item in luna)


# --- Resolution -------------------------------------------------------------


def _resolve(catalog: PricingCatalog, **kwargs):
    return resolve_trace_cost(record(**kwargs), when=WHEN, reference_catalog=catalog)


def test_a_small_prompt_resolves_to_the_short_context_rate():
    resolution = _resolve(both_contexts(), input_tokens=1000)
    assert resolution.resolved
    assert resolution.input_per_million == pytest.approx(0.20)
    # The band was matched exactly, so nothing was assumed about context.
    assert "context=short" not in resolution.assumed_dimensions


def test_a_prompt_at_the_boundary_resolves_to_the_long_context_rate():
    resolution = _resolve(both_contexts(), input_tokens=LUNA_THRESHOLD)
    assert resolution.resolved
    assert resolution.input_per_million == pytest.approx(0.40)
    assert "context=long" not in resolution.assumed_dimensions


def test_a_prompt_just_below_the_boundary_stays_short():
    resolution = _resolve(both_contexts(), input_tokens=LUNA_THRESHOLD - 1)
    assert resolution.input_per_million == pytest.approx(0.20)


def test_a_prompt_far_above_the_boundary_stays_long():
    resolution = _resolve(both_contexts(), input_tokens=400_000)
    assert resolution.input_per_million == pytest.approx(0.40)


def test_an_unavailable_context_length_falls_back_to_short_and_says_so():
    # An aggregate bucket's token count is an interval total, not one prompt.
    resolution = _resolve(both_contexts(), input_tokens=900_000, aggregate=True)
    assert resolution.resolved
    assert resolution.input_per_million == pytest.approx(0.20)
    assert "context=short" in resolution.assumed_dimensions


def test_an_aggregate_bucket_is_never_read_as_one_long_prompt():
    catalog = both_contexts()
    # The same bucket total would select the long meter if it were mistaken for
    # a prompt size. It must not.
    assert _resolve(catalog, input_tokens=900_000, aggregate=True).input_per_million == pytest.approx(0.20)
    assert _resolve(catalog, input_tokens=900_000, aggregate=False).input_per_million == pytest.approx(0.40)


def test_a_short_only_sync_withholds_cost_above_the_boundary():
    short_only = crosswalk_rows(_rows("azure_retail_page1.json"), as_of=AS_OF)
    catalog = PricingCatalog(
        catalog_name="short-only",
        retrieved_at=AS_OF,
        pricing_basis="analysis_date_snapshot",
        prices=short_only.entries,
    )
    assert _resolve(catalog, input_tokens=1000).resolved is True
    over = _resolve(catalog, input_tokens=400_000)
    assert over.resolved is False
    assert over.unresolved_reason == "no-exact-model-mode-price"


def test_an_unbounded_entry_never_claims_a_context_assumption():
    catalog = PricingCatalog(
        catalog_name="packaged-style",
        retrieved_at=AS_OF,
        pricing_basis="analysis_date_snapshot",
        prices=[
            PriceEntry(
                provider="azure_foundry",
                publisher="microsoft",
                model="gpt-5.6-luna",
                region="global",
                effective_from=date(2026, 1, 1),
                input_per_million=0.20,
                output_per_million=1.20,
            )
        ],
    )
    resolution = _resolve(catalog, input_tokens=900_000, aggregate=True)
    assert resolution.resolved
    assert resolution.assumed_dimensions == []


# --- Reported through the analysis ------------------------------------------


def test_the_context_default_is_reported_as_an_applied_assumption():
    report = analyze(
        [record(input_tokens=500, aggregate=True) for _ in range(3)],
        "synthetic telemetry",
        generated_at="2026-09-22T08:00:00Z",
        data_classification="synthetic",
        reference_catalog=both_contexts(),
    )
    applied = report.report_metadata["pricing"]["assumed_dimensions_applied"]
    assert "context=short" in applied


def test_an_exact_context_length_reports_no_context_assumption():
    records = [
        next(
            iter_records(
                io.StringIO(
                    json.dumps(
                        {
                            "timestamp": (WHEN + timedelta(minutes=index)).isoformat(),
                            "deployment_name": "prod",
                            "model_name": "gpt-5.6-luna",
                            "deployment_mode": "global",
                            "messages": [{"role": "user", "content": "synthetic"}],
                            "usage": {"input_tokens": 1000, "output_tokens": 100},
                            "status_code": 200,
                        }
                    )
                    + "\n"
                )
            )
        )
        for index in range(3)
    ]
    report = analyze(
        records,
        "synthetic telemetry",
        generated_at="2026-09-22T08:00:00Z",
        data_classification="synthetic",
        reference_catalog=both_contexts(),
    )
    applied = report.report_metadata["pricing"]["assumed_dimensions_applied"]
    assert "context=short" not in applied
