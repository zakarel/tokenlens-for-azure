from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import tiktoken


_ENCODING_ASSETS = {
    "cl100k_base": (
        "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
        "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    ),
    "o200k_base": (
        "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken",
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
    ),
    "o200k_harmony": (
        "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken",
        "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
    ),
    "r50k_base": (
        "https://openaipublic.blob.core.windows.net/encodings/r50k_base.tiktoken",
        "306cd27f03c1a714eca7108e03d66b7dc042abe8c258b44c199a7ed9838dd930",
    ),
    "p50k_base": (
        "https://openaipublic.blob.core.windows.net/encodings/p50k_base.tiktoken",
        "94b5ca7dff4d00767bc256fdd1b27e5b17361d7b8a5f968547f9f23eb70d2069",
    ),
    "p50k_edit": (
        "https://openaipublic.blob.core.windows.net/encodings/p50k_base.tiktoken",
        "94b5ca7dff4d00767bc256fdd1b27e5b17361d7b8a5f968547f9f23eb70d2069",
    ),
}


@dataclass(frozen=True)
class _ByteHeuristicEncoding:
    """Offline fallback that substitutes UTF-8 byte counts for BPE token counts."""

    name: str

    def count(self, value: str) -> int:
        return len(value.encode("utf-8"))


def _cache_directory() -> Path | None:
    configured = os.environ.get("TIKTOKEN_CACHE_DIR")
    if configured is None:
        configured = os.environ.get("DATA_GYM_CACHE_DIR")
    if configured == "":
        return None
    return Path(configured) if configured is not None else Path(tempfile.gettempdir()) / "data-gym-cache"


def _encoding_asset_is_cached(name: str) -> bool:
    asset = _ENCODING_ASSETS.get(name)
    cache = _cache_directory()
    if asset is None or cache is None:
        return False
    url, expected_hash = asset
    cached_file = cache / hashlib.sha1(url.encode("utf-8")).hexdigest()
    try:
        digest = hashlib.sha256(cached_file.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == expected_hash


@lru_cache(maxsize=32)
def _encoding(model: str):
    """Use exact BPE data only when already cached; analysis never downloads it."""
    try:
        name = tiktoken.model.encoding_name_for_model(model.casefold())
    except KeyError:
        name = "cl100k_base"
    if _encoding_asset_is_cached(name):
        return tiktoken.get_encoding(name)
    return _ByteHeuristicEncoding(name)


def tokenizer_provenance(model: str) -> dict[str, str]:
    """Describe whether this model used exact BPE data or an offline heuristic."""
    encoding = _encoding(model)
    if isinstance(encoding, _ByteHeuristicEncoding):
        return {
            "model": model,
            "encoding": encoding.name,
            "method": "utf8_byte_heuristic",
            "confidence": "low",
        }
    return {
        "model": model,
        "encoding": encoding.name,
        "method": "tiktoken_bpe",
        "confidence": "exact",
    }


def _count(value: str, model: str) -> int:
    encoding = _encoding(model)
    if isinstance(encoding, _ByteHeuristicEncoding):
        return encoding.count(value)
    return len(encoding.encode(value))


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            part.get("text", "")
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def count_text(value: Any, model: str) -> int:
    return _count(_text(value), model)


def count_messages(messages: list[dict[str, Any]], model: str) -> int:
    total = 0
    for message in messages:
        total += 4
        total += count_text(message.get("role", ""), model)
        total += count_text(message.get("content", ""), model)
        if message.get("name"):
            total += count_text(message["name"], model)
    return total + (2 if messages else 0)


def count_tools(tools: list[dict[str, Any]], model: str) -> int:
    if not tools:
        return 0
    return _count(json.dumps(tools, sort_keys=True, ensure_ascii=False), model)


def count_chunks(chunks: list[Any], model: str) -> int:
    return sum(count_text(chunk, model) for chunk in chunks)
