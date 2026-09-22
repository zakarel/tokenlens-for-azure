"""Official Claude pricing: deterministic parsing, CCU conversion, and drift.

Foundry bills Claude in Anthropic consumption units. TokenLens reads the
official published US-dollar rates, converts them at a fixed 0.01 USD per CCU,
and labels the billing basis ``claude_ccu_equivalent``. Nothing here is
inferred from a related model, and the parser fails loudly on schema drift.
"""

from __future__ import annotations

import re
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokenlens.pricing_sources.cache import CLAUDE_SNAPSHOT, load_snapshot, snapshot_path, write_snapshot
from tokenlens.pricing_sources.claude_docs import (
    CLAUDE_ALLOW_LIST,
    CLAUDE_CURRENCY,
    CLAUDE_PRICING_URL,
    ClaudeCurrencyError,
    USD_PER_CCU,
    PricingSourceDrift,
    build_price_entry,
    claude_snapshot,
    deployment_multiplier,
    parse_claude_pricing,
    usd_to_ccu,
)
from tokenlens.pricing_sources.fetch import AllowListError, FetchedPage

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)

OPUS_5 = "Claude Opus 5"


def _html(name: str = "claude_pricing.html") -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _transport(name: str = "claude_pricing.html"):
    body = (FIXTURES / name).read_bytes()

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        assert url == CLAUDE_PRICING_URL
        return FetchedPage(url=url, status=200, body=body)

    return transport


# --- Allow-list -------------------------------------------------------------


def test_only_the_exact_official_claude_page_is_accepted():
    assert CLAUDE_ALLOW_LIST.validate(CLAUDE_PRICING_URL) == CLAUDE_PRICING_URL


@pytest.mark.parametrize(
    "url",
    [
        "http://platform.claude.com/docs/en/about-claude/pricing",
        "https://platform.claude.com/docs/en/about-claude/pricing/extra",
        "https://platform.claude.com/docs/en/about-claude",
        "https://docs.claude.com/docs/en/about-claude/pricing",
        "https://platform.claude.com.evil.test/docs/en/about-claude/pricing",
    ],
)
def test_any_other_host_or_path_is_refused(url):
    with pytest.raises(AllowListError):
        CLAUDE_ALLOW_LIST.validate(url)


# --- Parsing ----------------------------------------------------------------


def test_the_published_opus_5_rates_are_parsed_exactly():
    table = parse_claude_pricing(_html())
    opus = table.find(OPUS_5)
    assert opus is not None
    assert opus.base_input_per_mtok == pytest.approx(5.0)
    assert opus.cache_write_5m_per_mtok == pytest.approx(6.25)
    assert opus.cache_write_1h_per_mtok == pytest.approx(10.0)
    assert opus.cache_hit_per_mtok == pytest.approx(0.50)
    assert opus.output_per_mtok == pytest.approx(25.0)


def test_the_navigation_table_is_skipped_and_every_model_row_is_read():
    table = parse_claude_pricing(_html())
    assert [item.model for item in table.models] == [
        "Claude Opus 5",
        "Claude Sonnet 5",
        "Claude Haiku 4.5",
    ]


def test_a_renamed_or_merged_column_fails_loudly_instead_of_mis_mapping():
    with pytest.raises(PricingSourceDrift, match="expected columns"):
        parse_claude_pricing(_html("claude_pricing_drift.html"))


def test_a_page_without_any_table_fails_loudly():
    with pytest.raises(PricingSourceDrift):
        parse_claude_pricing("<html><body><p>Pricing moved.</p></body></html>")


def test_a_non_price_cell_fails_loudly():
    html = _html().replace("$6.25 / MTok", "contact sales")
    with pytest.raises(PricingSourceDrift, match="5m cache write"):
        parse_claude_pricing(html)


# --- CCU conversion ---------------------------------------------------------


def test_dollar_rates_convert_to_consumption_units_at_one_cent_each():
    assert USD_PER_CCU == 0.01
    assert usd_to_ccu(1.0) == pytest.approx(100.0)
    opus = parse_claude_pricing(_html()).find(OPUS_5)
    assert opus.ccu() == {
        "base_input": pytest.approx(500.0),
        "cache_write_5m": pytest.approx(625.0),
        "cache_write_1h": pytest.approx(1000.0),
        "cache_hit": pytest.approx(50.0),
        "output": pytest.approx(2500.0),
    }


def test_global_standard_is_the_baseline_and_data_zone_is_the_only_premium():
    assert deployment_multiplier("global") == (1.0, True)
    assert deployment_multiplier("data_zone") == (1.1, True)
    assert deployment_multiplier("data-zone") == (1.1, True)
    # An unknown mode falls back to the global baseline and says so.
    assert deployment_multiplier("unknown") == (1.0, False)
    assert deployment_multiplier(None) == (1.0, False)


def test_a_global_entry_uses_the_published_rates_unmultiplied():
    opus = parse_claude_pricing(_html()).find(OPUS_5)
    entry = build_price_entry(opus, deployment_mode="global", retrieved_at=NOW)
    assert entry.model == "claude-opus-5"
    assert entry.publisher == "anthropic"
    assert entry.billing_basis == "claude_ccu_equivalent"
    assert entry.region == "global"
    assert entry.input_per_million == pytest.approx(5.0)
    assert entry.cached_input_per_million == pytest.approx(0.50)
    assert entry.cache_write_per_million == pytest.approx(6.25)
    assert entry.output_per_million == pytest.approx(25.0)
    assert entry.source_url == CLAUDE_PRICING_URL
    # The CCU equivalent is reported alongside the dollar estimate.
    assert "500 CCU/MTok" in entry.note
    assert "100 CCU = $1" in entry.note
    assert "private discounts are not included" in entry.note


def test_the_one_hour_cache_tier_can_be_selected_explicitly():
    opus = parse_claude_pricing(_html()).find(OPUS_5)
    entry = build_price_entry(opus, deployment_mode="global", cache_tier="1h", retrieved_at=NOW)
    assert entry.cache_write_per_million == pytest.approx(10.0)
    assert "1h cache write 1000 CCU/MTok" in entry.note


def test_the_data_zone_premium_applies_only_to_an_exact_data_zone_mode():
    opus = parse_claude_pricing(_html()).find(OPUS_5)
    dz = build_price_entry(opus, deployment_mode="data_zone", retrieved_at=NOW)
    assert dz.region == "data_zone"
    assert dz.input_per_million == pytest.approx(5.5)
    assert dz.cached_input_per_million == pytest.approx(0.55)
    assert dz.cache_write_per_million == pytest.approx(6.875)
    assert dz.output_per_million == pytest.approx(27.5)
    assert "1.1x" in dz.note

    # Under the global default assumption the premium is never applied.
    assumed = build_price_entry(opus, deployment_mode="unknown", retrieved_at=NOW)
    assert assumed.region == "global"
    assert assumed.input_per_million == pytest.approx(5.0)
    assert "deployment=global" in assumed.assumed_dimensions


def test_the_global_default_is_labelled_and_an_exact_mode_is_not():
    opus = parse_claude_pricing(_html()).find(OPUS_5)
    exact = build_price_entry(opus, deployment_mode="global", retrieved_at=NOW)
    assert "deployment=global" not in exact.assumed_dimensions
    assert "purchase_model=retail" in exact.assumed_dimensions
    assert "service_tier=standard" in exact.assumed_dimensions


# --- Snapshot cache ---------------------------------------------------------


def test_a_claude_snapshot_is_written_and_read_back_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = claude_snapshot(models=[OPUS_5], transport=_transport(), now=NOW)
    assert snapshot.source == "claude_pricing_docs"
    assert snapshot.source_url == CLAUDE_PRICING_URL
    assert snapshot.content_hash.startswith("sha256:")
    assert [entry.model for entry in snapshot.catalog.prices] == ["claude-opus-5"]
    assert any("100 CCU = $1" in note for note in snapshot.notes)
    assert any("private discounts are not included" in note for note in snapshot.notes)

    path = write_snapshot(snapshot, name=CLAUDE_SNAPSHOT)
    assert path == snapshot_path(CLAUDE_SNAPSHOT)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reloaded = load_snapshot(CLAUDE_SNAPSHOT)
    assert reloaded is not None
    assert reloaded.catalog.prices[0].input_per_million == pytest.approx(5.0)
    assert reloaded.catalog.prices[0].billing_basis == "claude_ccu_equivalent"


def test_a_requested_model_that_is_not_published_is_quarantined_not_invented(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = claude_snapshot(models=["Claude Opus 9"], transport=_transport(), now=NOW)
    assert snapshot.catalog.prices == []
    assert [item.reason for item in snapshot.quarantined] == ["model-not-published"]


def test_drift_never_writes_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    with pytest.raises(PricingSourceDrift):
        claude_snapshot(transport=_transport("claude_pricing_drift.html"), now=NOW)
    assert load_snapshot(CLAUDE_SNAPSHOT) is None


def test_drift_falls_back_to_the_previous_cached_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    from tokenlens.pricing_sources.sync import sync_public_pricing

    good = claude_snapshot(models=[OPUS_5], transport=_transport(), now=NOW)
    write_snapshot(good, name=CLAUDE_SNAPSHOT)

    report = sync_public_pricing(
        include_claude=True,
        transport=_transport("claude_pricing_drift.html"),
        now=NOW,
    )
    claude = next(item for item in report.outcomes if item.source == "claude_pricing_docs")
    assert claude.ok is False
    assert "PricingSourceDrift" in (claude.error or "")
    assert claude.used_cache is True
    # The previously verified snapshot is intact and still readable.
    cached = load_snapshot(CLAUDE_SNAPSHOT)
    assert cached is not None
    assert cached.catalog.prices[0].input_per_million == pytest.approx(5.0)


# --- Redirects --------------------------------------------------------------


def test_a_claude_redirect_off_the_allow_listed_page_is_refused():
    from tokenlens.pricing_sources.fetch import redirect_probe

    for target in (
        "https://docs.claude.com/docs/en/about-claude/pricing",
        "http://platform.claude.com/docs/en/about-claude/pricing",
        "https://platform.claude.com/docs/en/about-claude/pricing-v2",
    ):
        with pytest.raises(AllowListError):
            redirect_probe(CLAUDE_ALLOW_LIST, from_url=CLAUDE_PRICING_URL, to_url=target)


def test_an_allow_listed_claude_redirect_is_permitted():
    from tokenlens.pricing_sources.fetch import redirect_probe

    request = redirect_probe(
        CLAUDE_ALLOW_LIST,
        from_url=CLAUDE_PRICING_URL,
        to_url="https://platform.claude.com:443/docs/en/about-claude/pricing",
    )
    assert request is not None


# --- Currency ---------------------------------------------------------------


def test_a_non_usd_claude_snapshot_is_refused_rather_than_converted(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    with pytest.raises(ClaudeCurrencyError, match="never"):
        claude_snapshot(currency="EUR", transport=_transport(), now=NOW)
    assert load_snapshot(CLAUDE_SNAPSHOT) is None


def test_a_claude_snapshot_records_its_currency(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = claude_snapshot(models=[OPUS_5], transport=_transport(), now=NOW)
    assert snapshot.currency == CLAUDE_CURRENCY == "USD"
    assert snapshot.catalog.currency == "USD"
    assert snapshot.feed_complete is True


# --- Provenance arithmetic --------------------------------------------------


@pytest.mark.parametrize("mode", ["global", "data_zone"])
def test_the_note_ccu_figures_match_the_stored_usd_rates(mode):
    opus = parse_claude_pricing(_html()).find(OPUS_5)
    entry = build_price_entry(opus, deployment_mode=mode, retrieved_at=NOW)
    quoted = {
        key: float(value)
        for key, value in re.findall(r"(input|cache write|cache hit|output) ([0-9.]+) CCU/MTok", entry.note)
    }
    assert quoted["input"] == pytest.approx(usd_to_ccu(entry.input_per_million))
    assert quoted["cache write"] == pytest.approx(usd_to_ccu(entry.cache_write_per_million))
    assert quoted["cache hit"] == pytest.approx(usd_to_ccu(entry.cached_input_per_million))
    assert quoted["output"] == pytest.approx(usd_to_ccu(entry.output_per_million))


def test_the_data_zone_note_states_the_multiplier_and_the_base_rates():
    opus = parse_claude_pricing(_html()).find(OPUS_5)
    entry = build_price_entry(opus, deployment_mode="data_zone", retrieved_at=NOW)
    assert "1.1x data_zone deployment multiplier" in entry.note
    # 5 USD * 1.1 = 5.5 USD = 550 CCU, not the unmultiplied 500.
    assert "input 550 CCU/MTok" in entry.note
    assert "input 500 CCU/MTok" not in entry.note
    assert "Published base rates before the multiplier: input 5 USD" in entry.note


def test_the_global_note_states_the_baseline_multiplier():
    opus = parse_claude_pricing(_html()).find(OPUS_5)
    entry = build_price_entry(opus, deployment_mode="global", retrieved_at=NOW)
    assert "1x global deployment multiplier" in entry.note
    assert "input 500 CCU/MTok" in entry.note


# --- Deployment modes are handled per mode, never all-or-nothing ------------


def test_an_unstated_mode_is_preserved_and_labelled_as_the_global_default(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = claude_snapshot(models=[OPUS_5], transport=_transport(), now=NOW)
    entry = snapshot.catalog.prices[0]
    assert entry.region == "global"
    assert entry.input_per_million == pytest.approx(5.0)
    # The mode was never stated, so the default is applied *and named*.
    assert "deployment=global" in entry.assumed_dimensions
    assert "deployment=global" in snapshot.assumed_dimensions


def test_an_exactly_configured_global_mode_is_not_an_assumption(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = claude_snapshot(
        models=[OPUS_5], deployment_modes=["global"], transport=_transport(), now=NOW
    )
    entry = snapshot.catalog.prices[0]
    assert entry.region == "global"
    assert "deployment=global" not in entry.assumed_dimensions
    assert entry.input_per_million == pytest.approx(5.0)


def test_an_exactly_configured_data_zone_mode_is_not_an_assumption(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = claude_snapshot(
        models=[OPUS_5], deployment_modes=["data_zone"], transport=_transport(), now=NOW
    )
    entry = snapshot.catalog.prices[0]
    assert entry.region == "data_zone"
    assert entry.input_per_million == pytest.approx(5.5)
    assert "deployment=global" not in entry.assumed_dimensions


def test_an_unsupported_mode_is_quarantined_without_losing_the_supported_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = claude_snapshot(
        models=[OPUS_5],
        deployment_modes=["global", "regional", "data_zone"],
        transport=_transport(),
        now=NOW,
    )
    assert {entry.region for entry in snapshot.catalog.prices} == {"global", "data_zone"}
    unsupported = [
        item for item in snapshot.quarantined if item.reason == "deployment-mode-not-published"
    ]
    assert len(unsupported) == 1
    assert "requested=regional" in (unsupported[0].detail or "")
    assert "data_zone, global" in (unsupported[0].detail or "")
    # A single unsupported mode never fails the whole source.
    assert snapshot.feed_complete is True
    assert snapshot.empty_reason is None


def test_a_regional_only_request_completes_with_zero_entries_and_a_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = claude_snapshot(
        models=[OPUS_5], deployment_modes=["regional"], transport=_transport(), now=NOW
    )
    assert snapshot.catalog.prices == []
    assert snapshot.feed_complete is True
    assert snapshot.quarantined and snapshot.quarantined[0].reason == "deployment-mode-not-published"
    assert "not offered for Claude" in (snapshot.empty_reason or "")
    assert any("not offered for Claude" in note for note in snapshot.notes)
    # A complete-but-empty snapshot is still cacheable and readable.
    write_snapshot(snapshot, name=CLAUDE_SNAPSHOT)
    assert load_snapshot(CLAUDE_SNAPSHOT).empty_reason == snapshot.empty_reason


def test_a_regional_only_request_is_reported_as_ok_not_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    from tokenlens.pricing_sources.sync import sync_public_pricing

    report = sync_public_pricing(
        claude_deployment_modes=["regional"],
        claude_models=[OPUS_5],
        deployments=["regional"],
        transport=_transport(),
        now=NOW,
    )
    claude = next(item for item in report.outcomes if item.source == "claude_pricing_docs")
    assert claude.status == "ok"
    assert claude.entries == 0
    assert claude.quarantined >= 1
    assert "not offered for Claude" in (claude.reason or "")
