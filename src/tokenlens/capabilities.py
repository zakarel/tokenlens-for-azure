"""Versioned capability metadata for exact, offline Foundry model identities."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Modality = Literal[
    "text",
    "embedding",
    "speech_to_text",
    "text_to_speech",
    "audio",
    "image",
    "video",
    "multimodal",
    "router",
    "managed_compute",
    "mixed",
    "unknown",
]
UsageBasis = Literal[
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "audio_seconds",
    "audio_hours",
    "characters",
    "images",
    "megapixels",
    "video_seconds",
    "training_tokens",
    "training_hours",
    "hosting_hours",
    "compute_hours",
    "ptu_hours",
]
CommerceType = Literal["azure_direct", "marketplace", "managed_compute", "unknown"]
LifecycleStatus = Literal["active", "preview", "retired", "unknown"]

CAPABILITY_CATALOG_VERSION = "2026-10-06.2"


@dataclass(frozen=True)
class ModelCapability:
    canonical_model: str
    aliases: tuple[str, ...]
    modality: Modality
    usage_bases: tuple[UsageBasis, ...]
    commerce_type: CommerceType
    ptu_eligible: bool | None
    supported_deployment_types: tuple[str, ...] = (
        "global",
        "regional",
        "data_zone",
        "global_provisioned",
        "regional_provisioned",
        "data_zone_provisioned",
        "batch",
    )
    metric_capabilities: tuple[str, ...] = ()
    capacity_source_key: str | None = None
    lifecycle_status: LifecycleStatus = "active"
    base_model: str | None = None


_CAPABILITIES = (
    ModelCapability(
        canonical_model="gpt-4o",
        aliases=("gpt-4o", "gpt-4o-2024-05-13", "gpt-4o-2024-08-06", "gpt-4o-2024-11-20"),
        modality="text",
        usage_bases=("input_tokens", "cached_input_tokens", "output_tokens"),
        commerce_type="azure_direct",
        ptu_eligible=True,
        metric_capabilities=("input_tokens", "cached_input_tokens", "output_tokens", "requests"),
        capacity_source_key="azure_openai:gpt-4o",
    ),
    ModelCapability(
        canonical_model="gpt-4.1",
        aliases=("gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano", "gpt-4.1-2025-04-14"),
        modality="text",
        usage_bases=("input_tokens", "cached_input_tokens", "output_tokens"),
        commerce_type="azure_direct",
        ptu_eligible=True,
        metric_capabilities=("input_tokens", "cached_input_tokens", "output_tokens", "requests"),
        capacity_source_key="azure_openai:gpt-4.1",
    ),
    ModelCapability(
        canonical_model="gpt-5",
        aliases=(
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-5-chat",
            "gpt-5.1",
            "gpt-5.4",
            "gpt-5.6-luna",
        ),
        modality="text",
        usage_bases=("input_tokens", "cached_input_tokens", "output_tokens"),
        commerce_type="azure_direct",
        ptu_eligible=True,
        metric_capabilities=("input_tokens", "cached_input_tokens", "output_tokens", "requests"),
        capacity_source_key="azure_openai:gpt-5",
    ),
    ModelCapability(
        canonical_model="text-embedding",
        aliases=("text-embedding-3-large", "text-embedding-3-small", "text-embedding-ada-002"),
        modality="embedding",
        usage_bases=("input_tokens",),
        commerce_type="azure_direct",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="tts-1",
        aliases=("tts", "tts-1", "tts-1-hd", "gpt-4o-mini-tts"),
        modality="text_to_speech",
        usage_bases=("characters", "audio_seconds"),
        commerce_type="azure_direct",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="whisper",
        aliases=("whisper", "whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"),
        modality="speech_to_text",
        usage_bases=("audio_seconds", "audio_hours"),
        commerce_type="azure_direct",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="gpt-4o-audio",
        aliases=("gpt-4o-audio-preview", "gpt-4o-mini-audio-preview"),
        modality="audio",
        usage_bases=("input_tokens", "output_tokens", "audio_seconds"),
        commerce_type="azure_direct",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="gpt-4o-realtime",
        aliases=("gpt-4o-realtime-preview", "gpt-4o-mini-realtime-preview"),
        modality="audio",
        usage_bases=("input_tokens", "output_tokens", "audio_seconds"),
        commerce_type="azure_direct",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="dall-e",
        aliases=("dall-e-3", "dall-e-2", "gpt-image-1"),
        modality="image",
        usage_bases=("images", "megapixels", "input_tokens", "output_tokens"),
        commerce_type="azure_direct",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="sora",
        aliases=("sora", "sora-1", "sora-2"),
        modality="video",
        usage_bases=("video_seconds",),
        commerce_type="azure_direct",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="claude",
        aliases=("claude", "claude-3-5-sonnet", "claude-sonnet-4", "claude-opus-4"),
        modality="text",
        usage_bases=("input_tokens", "output_tokens"),
        commerce_type="marketplace",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="grok",
        aliases=("grok", "grok-4", "grok-4.1", "grok-4.6"),
        modality="text",
        usage_bases=("input_tokens", "cached_input_tokens", "output_tokens"),
        commerce_type="azure_direct",
        ptu_eligible=None,
    ),
    ModelCapability(
        canonical_model="mistral",
        aliases=("mistral", "mistral-small", "mistral-large", "ministral-3b", "mixtral-8x7b"),
        modality="text",
        usage_bases=("input_tokens", "cached_input_tokens", "output_tokens"),
        commerce_type="azure_direct",
        ptu_eligible=None,
    ),
    ModelCapability(
        canonical_model="flux-image",
        aliases=("flux.1-kontext-pro", "flux.1-schnell"),
        modality="image",
        usage_bases=("images", "megapixels"),
        commerce_type="unknown",
        ptu_eligible=False,
    ),
    ModelCapability(
        canonical_model="model-router",
        aliases=("model-router", "model-router-global"),
        modality="router",
        usage_bases=("input_tokens", "cached_input_tokens", "output_tokens"),
        commerce_type="azure_direct",
        ptu_eligible=False,
        metric_capabilities=("input_tokens", "cached_input_tokens", "output_tokens", "requests"),
    ),
    ModelCapability(
        canonical_model="fine-tuned",
        aliases=("fine-tuned", "fine_tuned", "fine-tuning"),
        modality="text",
        usage_bases=(
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "training_tokens",
            "training_hours",
            "hosting_hours",
        ),
        commerce_type="azure_direct",
        ptu_eligible=False,
        metric_capabilities=("input_tokens", "cached_input_tokens", "output_tokens", "requests"),
    ),
    ModelCapability(
        canonical_model="managed-compute",
        aliases=("managed-compute", "managed_compute", "managed compute"),
        modality="managed_compute",
        usage_bases=("compute_hours", "hosting_hours", "requests"),
        commerce_type="managed_compute",
        ptu_eligible=False,
        supported_deployment_types=("managed_compute",),
        metric_capabilities=("compute_hours", "hosting_hours", "requests"),
    ),
)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


_BY_ALIAS = {
    _key(alias): capability
    for capability in _CAPABILITIES
    for alias in capability.aliases
}


def resolve_model_capability(model: str | None) -> ModelCapability | None:
    """Resolve only curated exact aliases; unknown or new variants stay unknown."""
    normalized = _key(model or "")
    return _BY_ALIAS.get(normalized)


__all__ = [
    "CAPABILITY_CATALOG_VERSION",
    "CommerceType",
    "LifecycleStatus",
    "ModelCapability",
    "Modality",
    "UsageBasis",
    "resolve_model_capability",
]
