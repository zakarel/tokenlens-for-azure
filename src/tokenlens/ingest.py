from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from .models import TraceRecord, Usage


class InputError(ValueError):
    """Raised when a JSONL record cannot be normalized."""


def _content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(
            part.get("text", "")
            for part in value
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _usage(raw: dict[str, Any]) -> Usage:
    usage = raw.get("usage") or {}
    details = usage.get("prompt_tokens_details") or usage.get("input_token_details") or {}
    return Usage(
        input_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0,
        output_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0,
        cached_tokens=usage.get("cached_tokens", details.get("cached_tokens", 0)) or 0,
    )


def normalize_record(raw: dict[str, Any], line_number: int = 0) -> TraceRecord:
    """Normalize the project schema and common OpenAI/Azure envelopes."""
    request = raw.get("request") if isinstance(raw.get("request"), dict) else raw
    response = raw.get("response") if isinstance(raw.get("response"), dict) else raw
    request_messages = request.get("messages") or []
    if not request_messages and isinstance(request.get("input"), list):
        request_messages = request["input"]
    usage = _usage(response)
    if not usage.input_tokens and not usage.output_tokens:
        usage = _usage(request)

    messages = [
        message
        for message in request_messages
        if isinstance(message, dict) and message.get("role")
    ]
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    if not metadata and isinstance(request.get("metadata"), dict):
        metadata = request["metadata"]

    candidate = {
        "timestamp": raw.get("timestamp") or request.get("timestamp"),
        "request_id": raw.get("request_id") or raw.get("id") or request.get("request_id"),
        "model": raw.get("model") or request.get("model") or response.get("model") or "unknown",
        "messages": messages,
        "tools": request.get("tools") or [],
        "max_output_tokens": request.get("max_output_tokens", request.get("max_tokens")),
        "usage": usage,
        "latency_ms": raw.get("latency_ms"),
        "status_code": raw.get("status_code"),
        "retry_of": raw.get("retry_of"),
        "retrieved_chunks": raw.get("retrieved_chunks") or request.get("retrieved_chunks") or [],
        "metadata": metadata,
        "response": response if response is not raw else None,
    }
    try:
        return TraceRecord.model_validate(candidate)
    except ValidationError as exc:
        raise InputError(f"Invalid JSONL record on line {line_number}: {exc}") from exc


def iter_records(source: TextIO) -> Iterator[TraceRecord]:
    for line_number, line in enumerate(source, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise InputError(f"JSONL line {line_number} must contain an object")
        yield normalize_record(raw, line_number)


def load_records(path: str) -> tuple[list[TraceRecord], str]:
    if path == "-":
        import sys

        return list(iter_records(sys.stdin)), "stdin"
    file_path = Path(path)
    if not file_path.is_file():
        raise InputError(f"Input file not found: {path}")
    with file_path.open("r", encoding="utf-8") as source:
        return list(iter_records(source)), str(file_path)


def message_text(messages: Iterable[dict[str, Any]]) -> str:
    return "\n".join(
        f"{message.get('role', '')}:{_content(message.get('content', ''))}"
        for message in messages
    )

