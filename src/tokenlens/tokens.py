from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import tiktoken


@lru_cache(maxsize=32)
def _encoding(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


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
    return len(_encoding(model).encode(_text(value)))


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
    return len(_encoding(model).encode(json.dumps(tools, sort_keys=True, ensure_ascii=False)))


def count_chunks(chunks: list[Any], model: str) -> int:
    return sum(count_text(chunk, model) for chunk in chunks)

