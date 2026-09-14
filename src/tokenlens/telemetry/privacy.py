"""Privacy enforcement for canonical telemetry.

Two guarantees are implemented here:

1. Keyed fingerprints. Repeated-prefix analysis uses HMAC-SHA256 with a local
   secret so a fingerprint cannot be reversed by dictionary attack. When no key
   is configured the fingerprint is omitted rather than downgraded to an
   unsalted hash.
2. Content rejection. Provider payloads and OpenTelemetry spans are filtered
   through an allow list; content-bearing keys are counted, never read into a
   record.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import Any, Iterable

FINGERPRINT_KEY_ENV = "TOKENLENS_FINGERPRINT_KEY"
FINGERPRINT_PREFIX = "hmac-sha256:"

#: Keys that may carry prompt/response content, credentials, or network
#: identity. They are never copied into a canonical record.
CONTENT_KEY_FRAGMENTS = (
    "prompt",
    "completion",
    "message",
    "messages",
    "content",
    "input",
    "output",
    "text",
    "arguments",
    "tool_call",
    "tool_result",
    "document",
    "chunk",
    "embedding",
    "header",
    "authorization",
    "api_key",
    "apikey",
    "token_value",
    "secret",
    "password",
    "cookie",
    "connection_string",
    "endpoint",
    "url",
    "uri",
    "ip",
    "user_id",
    "tenant",
    "subscription",
)

#: Fields a canonical record must never contain, checked defensively before a
#: record is written.
FORBIDDEN_RECORD_KEYS = (
    "messages",
    "prompt",
    "prompt_text",
    "completion",
    "response_text",
    "system_prompt",
    "tools",
    "tool_arguments",
    "retrieved_chunks",
    "headers",
    "api_key",
    "authorization",
    "endpoint",
    "connection_string",
    "subscription_id",
    "tenant_id",
    "request_id",
)


class ContentLeakError(ValueError):
    """Raised when a record would carry prompt/response or credential content."""


@dataclass(frozen=True)
class FingerprintPolicy:
    """Fingerprinting configuration.

    ``key`` is held only in memory and is never written into telemetry.
    """

    enabled: bool = True
    key: bytes | None = None

    @classmethod
    def from_env(cls, *, enabled: bool = True, key_env: str = FINGERPRINT_KEY_ENV) -> "FingerprintPolicy":
        raw = os.getenv(key_env) or None
        return cls(enabled=enabled and bool(raw), key=raw.encode("utf-8") if raw else None)

    @property
    def active(self) -> bool:
        return bool(self.enabled and self.key)

    def fingerprint(self, value: str | None) -> str | None:
        """Return a keyed fingerprint, or ``None`` when fingerprinting is inactive."""
        if not self.active or not value:
            return None
        digest = hmac.new(self.key or b"", value.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{FINGERPRINT_PREFIX}{digest}"


def is_content_key(key: str) -> bool:
    lowered = key.casefold()
    return any(fragment in lowered for fragment in CONTENT_KEY_FRAGMENTS)


def split_attributes(attributes: dict[str, Any], allow: Iterable[str]) -> tuple[dict[str, Any], int]:
    """Split span/log attributes into allow-listed values and a rejected count.

    Rejected values are counted but never returned, logged, or serialized.
    """
    allowed = set(allow)
    kept: dict[str, Any] = {}
    rejected = 0
    for key, value in attributes.items():
        if key in allowed:
            kept[key] = value
            continue
        if is_content_key(key):
            rejected += 1
    return kept, rejected


def assert_contentless(payload: dict[str, Any]) -> dict[str, Any]:
    """Fail closed when a serialized record contains a forbidden key."""
    found = sorted(key for key in _walk_keys(payload) if key in FORBIDDEN_RECORD_KEYS)
    if found:
        raise ContentLeakError(f"telemetry record contains forbidden field(s): {', '.join(found)}")
    return payload


#: Subtrees whose *keys* name a measurement rather than content. Their values
#: are already constrained by the canonical schema (integers, or keyed HMAC
#: fingerprints), so the key-name check does not apply inside them.
_MEASUREMENT_SUBTREES = ("content_features", "fingerprints")


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            if key in _MEASUREMENT_SUBTREES:
                continue
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)
