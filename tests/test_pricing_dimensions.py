"""One canonicalizer for every pricing dimension, used by every source.

Deployment mode, context window, service tier, and inference mode are
normalized in exactly one place. Azure and Claude consume the same canonical
values, an unstated dimension is distinguished from an invalid one, and the CLI
refuses an unsupported value before any request is made.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tokenlens.cli import app
from tokenlens.pricing_sources.assumptions import (
    CONTEXT_VALUES,
    DEPLOYMENT_VALUES,
    INFERENCE_VALUES,
    SERVICE_TIER_VALUES,
    PricingDimensionError,
    canonical_context,
    canonical_deployment,
    canonical_inference,
    canonical_service_tier,
    optional_deployment,
)

runner = CliRunner()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("global", "global"),
        ("Global", "global"),
        ("GlobalStandard", "global"),
        ("global_standard", "global"),
        ("Gl", "global"),
        ("data_zone", "data_zone"),
        ("data-zone", "data_zone"),
        ("Data Zone", "data_zone"),
        ("DZ", "data_zone"),
        ("regional", "regional"),
        ("regnl", "regional"),
    ],
)
def test_deployment_modes_normalize_to_one_canonical_value(raw, expected):
    assert canonical_deployment(raw) == expected
    assert expected in DEPLOYMENT_VALUES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("short", "short"),
        ("Short Context", "short"),
        ("ShortCo", "short"),
        ("shco", "short"),
        ("long", "long"),
        ("LongCo", "long"),
        ("loco", "long"),
    ],
)
def test_context_windows_normalize_to_one_canonical_value(raw, expected):
    assert canonical_context(raw) == expected
    assert expected in CONTEXT_VALUES


def test_service_tier_and_inference_normalize():
    assert canonical_service_tier("Std") == "standard"
    assert canonical_service_tier("standard") in SERVICE_TIER_VALUES
    assert canonical_inference("Normal Inference") == "normal"
    assert canonical_inference("normal") in INFERENCE_VALUES


@pytest.mark.parametrize(
    "raw", ["", "unknown", "none", "unspecified", None, "NULL"]
)
def test_an_unstated_deployment_mode_is_not_an_error(raw):
    assert optional_deployment(raw) is None


@pytest.mark.parametrize("raw", ["wibble", "batch", "provisioned", "global-ish", "dzone2"])
def test_an_unrecognized_deployment_mode_is_rejected(raw):
    with pytest.raises(PricingDimensionError, match="supported deployment mode"):
        canonical_deployment(raw)
    with pytest.raises(PricingDimensionError):
        optional_deployment(raw)


@pytest.mark.parametrize("raw", ["medium", "huge", "", "unknown"])
def test_an_unrecognized_context_window_is_rejected(raw):
    with pytest.raises(PricingDimensionError, match="supported context window"):
        canonical_context(raw)


@pytest.mark.parametrize("raw", ["batch", "priority", "provisioned", "flex"])
def test_an_excluded_purchasing_mode_is_never_a_service_tier(raw):
    with pytest.raises(PricingDimensionError, match="supported service tier"):
        canonical_service_tier(raw)


@pytest.mark.parametrize("raw", ["fine_tuned", "batch", "hosted"])
def test_an_excluded_inference_mode_is_rejected(raw):
    with pytest.raises(PricingDimensionError, match="supported inference mode"):
        canonical_inference(raw)


# --- Both sources consume the same canonical values -------------------------


def test_the_retail_crosswalk_rejects_an_unsupported_dimension():
    from tokenlens.pricing_sources.azure_retail import crosswalk_rows

    with pytest.raises(PricingDimensionError):
        crosswalk_rows([], deployments=["wibble"])
    with pytest.raises(PricingDimensionError):
        crosswalk_rows([], contexts=["medium"])


def test_the_retail_crosswalk_accepts_an_alias_for_a_supported_dimension():
    from tokenlens.pricing_sources.azure_retail import crosswalk_rows

    # No rows, but the call must not raise: the alias resolves.
    assert crosswalk_rows([], deployments=["Data Zone"], contexts=["LongCo"]).entries == []


def test_the_claude_multiplier_uses_the_same_canonicalizer():
    from tokenlens.pricing_sources.claude_docs import deployment_multiplier

    assert deployment_multiplier("Data Zone") == (1.1, True)
    assert deployment_multiplier("GlobalStandard") == (1.0, True)
    with pytest.raises(PricingDimensionError):
        deployment_multiplier("wibble")


def test_claude_refuses_a_mode_it_does_not_publish():
    from tokenlens.pricing_sources.claude_docs import PricingSourceDrift, deployment_multiplier

    with pytest.raises(PricingSourceDrift, match="regional"):
        deployment_multiplier("regional")


# --- CLI ---------------------------------------------------------------------


def test_the_cli_rejects_an_unsupported_deployment_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    result = runner.invoke(app, ["pricing", "sync", "--deployment-mode", "wibble"])
    assert result.exit_code == 1
    assert "not a supported deployment mode" in result.output
    # The refusal happens before anything is contacted or written.
    assert not (tmp_path / "cfg" / "pricing").exists()


def test_the_cli_rejects_an_unsupported_context_window(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    result = runner.invoke(app, ["pricing", "sync", "--context", "medium"])
    assert result.exit_code == 1
    assert "not a supported context window" in result.output
    assert not (tmp_path / "cfg" / "pricing").exists()
