from __future__ import annotations

from tokenlens.capabilities import (
    CAPABILITY_CATALOG_VERSION,
    resolve_model_capability,
)
from tokenlens.foundry_workflow.discovery import enrich_deployment


def test_tts_deployment_is_classified_from_exact_model_identity():
    deployment = enrich_deployment({"name": "speech-prod", "model": "tts-1"})

    assert deployment.modality == "text_to_speech"
    assert deployment.supported_usage_bases == ("characters", "audio_seconds")
    assert deployment.ptu_eligible is False
    assert deployment.capability_status == "verified"
    assert deployment.capability_catalog_version == CAPABILITY_CATALOG_VERSION


def test_capability_aliases_are_explicit_and_case_insensitive():
    first = resolve_model_capability("TEXT-EMBEDDING-3-LARGE")
    second = resolve_model_capability("text_embedding_3_large")

    assert first is not None
    assert second is first
    assert first.modality == "embedding"
    assert first.ptu_eligible is False


def test_unknown_or_near_match_model_is_not_assigned_a_guessed_modality():
    deployment = enrich_deployment({"name": "new-speech-deployment", "model": "tts-1-next"})

    assert deployment.modality == "unknown"
    assert deployment.supported_usage_bases == ()
    assert deployment.ptu_eligible is None
    assert deployment.capability_status == "unknown"
    assert deployment.capability_catalog_version is None


def test_catalog_separates_marketplace_from_azure_direct_commerce():
    claude = resolve_model_capability("claude-sonnet-4")
    gpt = resolve_model_capability("gpt-4o")

    assert claude is not None and claude.commerce_type == "marketplace"
    assert gpt is not None and gpt.commerce_type == "azure_direct"
