from __future__ import annotations

import json
import glob
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


def _first_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def normalize_record(raw: dict[str, Any], line_number: int = 0) -> TraceRecord:
    """Normalize the project schema and common OpenAI/Azure envelopes."""
    request = raw.get("request") if isinstance(raw.get("request"), dict) else raw
    response = raw.get("response") if isinstance(raw.get("response"), dict) else raw
    request_messages = request.get("messages") or []
    if not request_messages and isinstance(request.get("input"), list):
        request_messages = request["input"]
    usage = _usage(response)
    if not usage.input_tokens and not usage.output_tokens:
        usage = _usage(raw)
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

    # Deployment and model identity are deliberately separate. Azure/OpenAI
    # request ``model`` is commonly the deployment name, while the response
    # model identifies the underlying model family.
    deployment = _first_value(
        raw.get("deployment_name"),
        raw.get("deployment"),
        request.get("deployment_name"),
        request.get("deployment"),
        metadata.get("deployment_name"),
        metadata.get("deployment"),
        request.get("model"),
    ) or "unknown"
    model_name = _first_value(
        raw.get("model_name"),
        response.get("model_name"),
        response.get("model"),
        metadata.get("model_name"),
    ) or "unknown"
    provider = _first_value(
        raw.get("provider"),
        request.get("provider"),
        metadata.get("provider"),
    )
    endpoint = _first_value(raw.get("endpoint"), request.get("endpoint"), metadata.get("endpoint"))
    if not provider and (endpoint or raw.get("deployment") or raw.get("deployment_name")):
        provider = "azure_foundry"
    model = model_name if model_name != "unknown" else str(deployment)
    candidate = {
        "timestamp": raw.get("timestamp") or request.get("timestamp"),
        "request_id": raw.get("request_id") or raw.get("id") or request.get("request_id"),
        "model": model,
        "deployment_name": str(deployment),
        "model_name": str(model_name),
        "provider": str(provider or "unknown"),
        "resource_name": _first_value(raw.get("resource_name"), request.get("resource_name"), metadata.get("resource_name")),
        "project_name": _first_value(raw.get("project_name"), request.get("project_name"), metadata.get("project_name")),
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


def load_records_many(paths: Iterable[str]) -> tuple[list[TraceRecord], str]:
    """Load one or more files, directories, or glob patterns without merging paths silently."""
    expanded: list[Path] = []
    for value in paths:
        if value == "-":
            records, source = load_records(value)
            return records, source
        path = Path(value)
        matches = [Path(item) for item in glob.glob(value, recursive=True)] if any(char in value for char in "*?[") else [path]
        for match in matches:
            if match.is_dir():
                expanded.extend(sorted(item for item in match.glob("*.jsonl") if item.is_file()))
            elif match.is_file():
                expanded.append(match)
    # Preserve command-line order while avoiding accidental duplicate glob matches.
    unique = list(dict.fromkeys(expanded))
    if not unique:
        raise InputError(f"Input file not found: {', '.join(paths)}")
    records: list[TraceRecord] = []
    for path in unique:
        with path.open("r", encoding="utf-8") as source:
            records.extend(iter_records(source))
    return records, ", ".join(str(path) for path in unique)


def message_text(messages: Iterable[dict[str, Any]]) -> str:
    return "\n".join(
        f"{message.get('role', '')}:{_content(message.get('content', ''))}"
        for message in messages
    )
