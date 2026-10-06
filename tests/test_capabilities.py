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


def test_ga_capability_matrix_keeps_modalities_and_units_explicit():
    cases = {
        "gpt-4o": ("text", "input_tokens"),
        "text-embedding-3-large": ("embedding", "input_tokens"),
        "gpt-4o-realtime-preview": ("audio", "audio_seconds"),
        "gpt-4o-mini-tts": ("text_to_speech", "characters"),
        "whisper-1": ("speech_to_text", "audio_hours"),
        "dall-e-3": ("image", "images"),
        "sora-2": ("video", "video_seconds"),
        "grok-4.6": ("text", "input_tokens"),
        "claude-sonnet-4": ("text", "input_tokens"),
        "mistral-large": ("text", "input_tokens"),
        "fine-tuned": ("text", "training_tokens"),
        "model-router": ("router", "input_tokens"),
        "managed-compute": ("managed_compute", "compute_hours"),
    }
    for model, (modality, usage_basis) in cases.items():
        capability = resolve_model_capability(model)
        assert capability is not None, model
        assert capability.modality == modality
        assert usage_basis in capability.usage_bases


def test_unknown_future_model_has_no_guessed_properties():
    assert resolve_model_capability("future-model-2027") is None


def test_discovery_preserves_fine_tuning_and_router_identity():
    fine_tuned = enrich_deployment({
        "name": "tuned",
        "model": "fine-tuned",
        "base_model": "gpt-4o",
        "fine_tuned": True,
    })
    router = enrich_deployment({
        "name": "router",
        "model": "model-router",
        "router": True,
        "spillover_deployment": "fallback",
    })
    assert fine_tuned.base_model == "gpt-4o"
    assert fine_tuned.fine_tuned is True
    assert "training_tokens" in fine_tuned.supported_usage_bases
    assert router.router_configured is True
    assert router.spillover_deployment == "fallback"
