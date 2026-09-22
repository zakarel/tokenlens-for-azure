"""Guided auto-sync, deployment matching, and pricing readiness precedence.

The wizard may refresh the cached official snapshot; analysis never does. Both
paths are exercised here with injected transports and synthetic fixtures, and
the suite-wide guard makes a real socket impossible.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tokenlens.foundry_workflow.models import DeploymentRecord, FoundryWorkflowConfig, FoundryTarget
from tokenlens.foundry_workflow.pricing import (
    ASSUMPTIONS_BANNER,
    CustomerRate,
    catalog_status,
    load_customer_catalog,
    pricing_readiness,
    public_snapshot_paths,
    write_customer_rate,
)
from tokenlens.foundry_workflow.prompts import ScriptedPrompter
from tokenlens.foundry_workflow.wizard import sync_public_pricing_if_needed
from tokenlens.pricing_sources.azure_retail import retail_snapshot
from tokenlens.pricing_sources.cache import (
    AZURE_RETAIL_SNAPSHOT,
    CLAUDE_SNAPSHOT,
    load_snapshot,
    write_snapshot,
)
from tokenlens.pricing_sources.claude_docs import claude_snapshot
from tokenlens.pricing_sources.fetch import FetchedPage
from tokenlens.pricing_sources.sync import (
    public_pricing_catalog,
    public_pricing_provenance,
    public_sync_needed,
    requested_sources,
    sync_public_pricing,
)
from tokenlens.reference import load_bundled_reference_catalog, load_effective_reference_catalog

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)

#: The three deployments in the user's own .tokenlens.yml, reproduced exactly.
DEPLOYMENTS = [
    DeploymentRecord(
        name="claude-opus-5",
        model="claude-opus-5",
        model_version="2",
        sku="GlobalStandard",
        deployment_mode="global",
        provider_family="claude_foundry",
        inference_api="anthropic",
        publisher="anthropic",
    ),
    DeploymentRecord(
        name="gpt-5.6-luna",
        model="gpt-5.6-luna",
        model_version="2026-07-09",
        sku="GlobalStandard",
        deployment_mode="global",
        provider_family="azure_openai",
        inference_api="openai",
        publisher="microsoft",
    ),
    DeploymentRecord(
        name="Ministral-3B",
        model="Ministral-3B",
        model_version="1",
        sku="GlobalStandard",
        deployment_mode="global",
        provider_family="partner_model",
        inference_api="openai",
        publisher="mistral",
    ),
]


def _retail(url: str, timeout: float, max_bytes: int) -> FetchedPage:
    name = "azure_retail_page2.json" if "skip" in url else "azure_retail_page1.json"
    return FetchedPage(url=url, status=200, body=(FIXTURES / name).read_bytes())


def _eur_retail(url: str, timeout: float, max_bytes: int) -> FetchedPage:
    """The same synthetic feed, redenominated, for currency-safety tests."""
    name = "azure_retail_page2.json" if "skip" in url else "azure_retail_page1.json"
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    for item in payload["Items"]:
        item["currencyCode"] = "EUR"
    payload["BillingCurrency"] = "EUR"
    return FetchedPage(url=url, status=200, body=json.dumps(payload).encode())


def _claude(url: str, timeout: float, max_bytes: int) -> FetchedPage:
    return FetchedPage(url=url, status=200, body=(FIXTURES / "claude_pricing.html").read_bytes())


def _transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
    return _retail(url, timeout, max_bytes) if "prices.azure.com" in url else _claude(url, timeout, max_bytes)


def _config(*, public_cache: bool = True, deployments=None) -> FoundryWorkflowConfig:
    config = FoundryWorkflowConfig(
        foundry=FoundryTarget(
            resource_group="rg-cu-samples",
            account="cu-samples-resource",
            region="eastus2",
            deployments=list(DEPLOYMENTS if deployments is None else deployments),
        )
    )
    config.pricing.public_cache = public_cache
    return config


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path


@pytest.fixture()
def synced(cache, monkeypatch):
    write_snapshot(retail_snapshot(transport=_retail, now=NOW), name=AZURE_RETAIL_SNAPSHOT)
    write_snapshot(
        claude_snapshot(models=["Claude Opus 5"], transport=_claude, now=NOW), name=CLAUDE_SNAPSHOT
    )
    return cache


# --- Guided auto-sync -------------------------------------------------------


def test_the_wizard_syncs_when_the_cache_is_absent(cache, monkeypatch):
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)
    import tokenlens.pricing_sources.sync as sync_module

    real = sync_module.sync_public_pricing
    monkeypatch.setattr(
        sync_module,
        "sync_public_pricing",
        lambda **kwargs: real(**{**kwargs, "transport": _transport}),
    )
    lines = sync_public_pricing_if_needed(ScriptedPrompter([]), _config())
    assert any("azure_retail_prices: synchronized" in line for line in lines)
    assert any("claude_pricing_docs: synchronized" in line for line in lines)
    for path in public_snapshot_paths().values():
        assert path.is_file()


def test_the_wizard_skips_the_sync_when_the_cache_is_current(synced, monkeypatch):
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)

    def forbidden(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("a current cache must not trigger a network request")

    monkeypatch.setattr("tokenlens.pricing_sources.sync.sync_public_pricing", forbidden)
    lines = sync_public_pricing_if_needed(ScriptedPrompter([]), _config())
    assert lines == ["Cached official pricing snapshot is current; no network request was made."]


def test_a_sync_failure_never_stops_the_assessment(cache, monkeypatch):
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)
    import tokenlens.pricing_sources.sync as sync_module

    real = sync_module.sync_public_pricing

    def broken(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        raise OSError("synthetic outage")

    monkeypatch.setattr(
        sync_module,
        "sync_public_pricing",
        lambda **kwargs: real(**{**kwargs, "transport": broken}),
    )
    lines = sync_public_pricing_if_needed(ScriptedPrompter([]), _config())
    assert any("not synchronized" in line for line in lines)
    assert any("packaged catalog and customer rates still apply" in line for line in lines)
    # Readiness still works offline against the packaged catalog.
    assert pricing_readiness(DEPLOYMENTS)


def test_an_unexpected_sync_error_is_absorbed(cache, monkeypatch):
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)
    import tokenlens.pricing_sources.sync as sync_module

    def explode(**kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sync_module, "sync_public_pricing", explode)
    lines = sync_public_pricing_if_needed(ScriptedPrompter([]), _config())
    assert lines == [
        "Official pricing synchronization failed (RuntimeError). The assessment continues "
        "with the packaged catalog and any customer rates."
    ]


def test_the_sync_can_be_disabled_by_configuration_and_by_environment(cache, monkeypatch):
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)
    lines = sync_public_pricing_if_needed(ScriptedPrompter([]), _config(public_cache=False))
    assert lines == ["Public pricing synchronization is disabled in .tokenlens.yml (pricing.public_cache)."]

    monkeypatch.setenv("TOKENLENS_NO_PRICING_SYNC", "1")
    lines = sync_public_pricing_if_needed(ScriptedPrompter([]), _config())
    assert lines == ["Public pricing synchronization is disabled (TOKENLENS_NO_PRICING_SYNC)."]


# --- Matching the user's own deployments ------------------------------------


def test_every_current_deployment_resolves_to_an_exact_rate(synced):
    states = {item.deployment: item for item in pricing_readiness(DEPLOYMENTS)}
    assert states["gpt-5.6-luna"].state == "exact_public_rate"
    assert states["Ministral-3B"].state == "exact_public_rate"
    assert states["claude-opus-5"].state == "exact_public_rate"
    assert states["claude-opus-5"].billing_basis == "claude_ccu_equivalent"
    assert states["gpt-5.6-luna"].billing_basis == "token_rate"


def test_readiness_reports_which_defaults_were_assumed(synced):
    states = {item.deployment: item for item in pricing_readiness(DEPLOYMENTS)}
    # Luna's meters state the mode and the context window explicitly.
    assert states["gpt-5.6-luna"].assumed_dimensions == ["purchase_model=retail"]
    # Ministral's do not, so both defaults are named.
    assert "deployment=global" in states["Ministral-3B"].assumed_dimensions
    assert "context=short" in states["Ministral-3B"].assumed_dimensions


def test_ministral_is_priced_from_normal_inference_not_fine_tuning(synced):
    entry = next(
        item
        for item in load_effective_reference_catalog().prices
        if item.model == "ministral-3b"
    )
    assert entry.input_per_million == pytest.approx(0.04)
    assert entry.output_per_million == pytest.approx(0.04)
    assert entry.meter_ids["input"].startswith("44444444")


def test_a_data_zone_deployment_never_borrows_the_global_rate(synced):
    data_zone = DeploymentRecord(
        name="luna-dz", model="gpt-5.6-luna", deployment_mode="data_zone", publisher="microsoft"
    )
    state = pricing_readiness([data_zone])[0]
    # The synchronized snapshot holds global meters only under the global
    # default, so a data-zone deployment is honestly reported as unpriced.
    assert state.state == "rate_unavailable"
    assert state.suggested_override_key == "gpt-5.6-luna"


def test_an_unknown_model_is_never_matched_to_a_similar_one(synced):
    unknown = DeploymentRecord(name="mystery", model="gpt-5.7-sol", deployment_mode="global")
    assert pricing_readiness([unknown])[0].state == "rate_unavailable"


def test_an_unresolved_identity_is_not_a_pricing_problem(synced):
    blank = DeploymentRecord(name="unnamed", model="unknown", deployment_mode="global")
    state = pricing_readiness([blank])[0]
    assert state.state == "identity_unresolved"
    assert state.suggested_override_key is None


# --- Precedence -------------------------------------------------------------


def test_a_customer_override_outranks_the_synchronized_rate(synced):
    write_customer_rate(
        CustomerRate(
            model="gpt-5.6-luna",
            input_per_million=0.10,
            output_per_million=0.60,
            deployment_mode="global",
            effective_from=date(2026, 1, 1),
            note="Negotiated enterprise agreement",
        )
    )
    customer = load_customer_catalog()
    state = next(
        item
        for item in pricing_readiness(DEPLOYMENTS, customer_catalog=customer)
        if item.deployment == "gpt-5.6-luna"
    )
    assert state.state == "customer_override"
    assert state.catalog_name == "customer-overrides"


def test_catalog_status_names_every_source_and_the_assumptions(synced):
    status = catalog_status()
    assert status["pricing-assumptions"] == ASSUMPTIONS_BANNER
    assert status["public-sync"] == "cached"
    assert status["azure-retail-prices-entries"] == 2
    assert status["claude-pricing-docs-entries"] == 1
    assert str(status["azure-retail-prices-content-hash"]).startswith("sha256:")
    assert status["resolution-order"] == "observed > customer > public sync > packaged > unresolved"


def test_a_stale_snapshot_is_still_read_offline(cache):
    # Stale by the freshness budget, but every entry is still applied: a cached
    # snapshot is never discarded just because it is due for a refresh.
    stale_moment = datetime(2026, 8, 20, tzinfo=UTC)
    write_snapshot(retail_snapshot(transport=_retail, now=stale_moment), name=AZURE_RETAIL_SNAPSHOT)
    catalog = load_effective_reference_catalog()
    assert any(item.model == "gpt-5.6-luna" for item in catalog.prices)
    assert catalog_status()["public-sync-stale"] == "yes"


def test_disabling_the_public_cache_falls_back_to_the_packaged_catalog(synced):
    catalog = load_effective_reference_catalog(use_public_cache=False)
    assert not any(item.model == "gpt-5.6-luna" for item in catalog.prices)
    assert catalog.catalog_name == "microsoft-foundry-reference-2026-09-14"


# --- Source selection drives freshness --------------------------------------


def test_a_portfolio_without_claude_is_not_told_a_sync_is_overdue(cache):
    write_snapshot(retail_snapshot(transport=_retail, now=NOW), name=AZURE_RETAIL_SNAPSHOT)
    # Claude was never requested, so its absent snapshot is not a staleness signal.
    assert public_sync_needed(include_claude=False, now=NOW) is False
    assert public_sync_needed(include_claude=True, now=NOW) is True


def test_the_requested_source_set_is_persisted_and_reused(cache):
    sync_public_pricing(include_claude=False, transport=_transport, now=NOW)
    assert requested_sources() == ["azure_retail_prices"]
    # With nothing explicit, freshness follows what was actually synchronized.
    assert public_sync_needed(now=NOW) is False
    assert catalog_status()["public-sync-stale"] == "no"
    assert catalog_status()["claude-pricing-docs-requested"] == "no"

    sync_public_pricing(include_claude=True, transport=_transport, now=NOW)
    assert requested_sources() == ["azure_retail_prices", "claude_pricing_docs"]
    assert catalog_status()["claude-pricing-docs-requested"] == "yes"


def test_with_no_recorded_selection_both_sources_are_assumed(cache):
    assert requested_sources() == ["azure_retail_prices", "claude_pricing_docs"]
    assert public_sync_needed(now=NOW) is True


def test_the_wizard_only_requires_claude_when_a_claude_deployment_is_selected(cache, monkeypatch):
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)
    write_snapshot(retail_snapshot(transport=_retail, now=NOW), name=AZURE_RETAIL_SNAPSHOT)
    no_claude = [item for item in DEPLOYMENTS if item.publisher != "anthropic"]

    def forbidden(**kwargs):  # pragma: no cover - must never run
        raise AssertionError("a current cache must not trigger a network request")

    monkeypatch.setattr("tokenlens.pricing_sources.sync.sync_public_pricing", forbidden)
    lines = sync_public_pricing_if_needed(
        ScriptedPrompter([]), _config(deployments=no_claude)
    )
    assert lines == ["Cached official pricing snapshot is current; no network request was made."]


# --- Currency safety --------------------------------------------------------


def test_a_non_usd_sync_excludes_claude_with_a_stated_reason(cache):
    report = sync_public_pricing(currency="EUR", transport=_transport, now=NOW)
    claude = next(item for item in report.outcomes if item.source == "claude_pricing_docs")
    assert claude.status == "skipped"
    assert claude.ok is True
    assert "USD only" in (claude.reason or "")
    assert "never converts currencies" in (claude.reason or "")
    # A skipped source does not make the whole run a failure.
    assert report.ok is True
    assert load_snapshot(CLAUDE_SNAPSHOT) is None
    # ...and it is not recorded as a requested source.
    assert requested_sources() == ["azure_retail_prices"]


def test_snapshots_in_different_currencies_are_never_merged(cache):
    write_snapshot(
        retail_snapshot(currency="EUR", transport=_eur_retail, now=NOW), name=AZURE_RETAIL_SNAPSHOT
    )
    write_snapshot(
        claude_snapshot(models=["Claude Opus 5"], transport=_claude, now=NOW), name=CLAUDE_SNAPSHOT
    )
    usd = public_pricing_catalog(currency="USD")
    assert usd is not None
    assert {entry.publisher for entry in usd.prices} == {"anthropic"}
    assert usd.currency == "USD"

    eur = public_pricing_catalog(currency="EUR")
    assert eur is not None
    assert eur.currency == "EUR"
    assert eur.prices and all(entry.publisher != "anthropic" for entry in eur.prices)


def test_an_excluded_currency_is_reported_not_hidden(cache):
    write_snapshot(
        retail_snapshot(currency="EUR", transport=_eur_retail, now=NOW), name=AZURE_RETAIL_SNAPSHOT
    )
    write_snapshot(
        claude_snapshot(models=["Claude Opus 5"], transport=_claude, now=NOW), name=CLAUDE_SNAPSHOT
    )
    provenance = {item["source"]: item for item in public_pricing_provenance(currency="USD")}
    assert provenance["claude_pricing_docs"]["included"] is True
    assert provenance["azure_retail_prices"]["included"] is False
    assert "never converts currencies" in provenance["azure_retail_prices"]["excluded_reason"]
    assert "EUR" in str(catalog_status()["public-sync-excluded"])


def test_the_packaged_catalog_is_never_dropped_for_a_foreign_currency_snapshot(cache):
    write_snapshot(
        retail_snapshot(currency="EUR", transport=_eur_retail, now=NOW), name=AZURE_RETAIL_SNAPSHOT
    )
    catalog = load_effective_reference_catalog()
    packaged = load_bundled_reference_catalog()
    assert catalog.currency == packaged.currency == "USD"
    assert len(catalog.prices) == len(packaged.prices)
    assert {item.model for item in catalog.prices} == {item.model for item in packaged.prices}


# --- Claude deployment modes come from Claude deployments only -------------


def test_claude_modes_are_derived_only_from_claude_deployments(cache, monkeypatch):
    """A data-zone GPT deployment must not add a data-zone premium to Claude."""
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)
    mixed = [
        DeploymentRecord(
            name="claude-opus-5",
            model="claude-opus-5",
            deployment_mode="global",
            publisher="anthropic",
        ),
        DeploymentRecord(
            name="luna-dz", model="gpt-5.6-luna", deployment_mode="data_zone", publisher="microsoft"
        ),
    ]
    captured: dict[str, object] = {}
    import tokenlens.pricing_sources.sync as sync_module

    real = sync_module.sync_public_pricing

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**{**kwargs, "transport": _transport})

    monkeypatch.setattr(sync_module, "sync_public_pricing", spy)
    sync_public_pricing_if_needed(ScriptedPrompter([]), _config(deployments=mixed))

    assert captured["claude_deployment_modes"] == ["global"]
    # The retail side still sees both modes it was asked for.
    assert captured["deployments"] == ["data_zone", "global"]
    claude = [item for item in public_pricing_catalog().prices if item.publisher == "anthropic"]
    assert {item.region for item in claude} == {"global"}
    assert all(item.input_per_million == 5.0 for item in claude if item.model == "claude-opus-5")


def test_a_claude_data_zone_deployment_drives_the_data_zone_rate(cache, monkeypatch):
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)
    dz_claude = [
        DeploymentRecord(
            name="claude-dz",
            model="claude-opus-5",
            deployment_mode="data_zone",
            publisher="anthropic",
        )
    ]
    captured: dict[str, object] = {}
    import tokenlens.pricing_sources.sync as sync_module

    real = sync_module.sync_public_pricing

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**{**kwargs, "transport": _transport})

    monkeypatch.setattr(sync_module, "sync_public_pricing", spy)
    sync_public_pricing_if_needed(ScriptedPrompter([]), _config(deployments=dz_claude))
    assert captured["claude_deployment_modes"] == ["data_zone"]
    opus = next(
        item
        for item in public_pricing_catalog().prices
        if item.model == "claude-opus-5" and item.region == "data_zone"
    )
    assert opus.input_per_million == pytest.approx(5.5)


def test_a_portfolio_with_no_claude_deployment_passes_no_claude_modes(cache, monkeypatch):
    monkeypatch.delenv("TOKENLENS_NO_PRICING_SYNC", raising=False)
    captured: dict[str, object] = {}
    import tokenlens.pricing_sources.sync as sync_module

    real = sync_module.sync_public_pricing

    def spy(**kwargs):
        captured.update(kwargs)
        return real(**{**kwargs, "transport": _transport})

    monkeypatch.setattr(sync_module, "sync_public_pricing", spy)
    no_claude = [item for item in DEPLOYMENTS if item.publisher != "anthropic"]
    sync_public_pricing_if_needed(ScriptedPrompter([]), _config(deployments=no_claude))
    assert captured["include_claude"] is False
    assert captured["claude_deployment_modes"] is None
