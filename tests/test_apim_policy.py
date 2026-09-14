"""Static safety checks for the APIM AI Gateway policy example.

TokenLens never deploys this policy automatically; it ships as a reviewable
documentation artifact under ``examples/``. These tests only confirm that
artifact stays metadata-only (no payload/body access, no credential values)
and carries the required cross-API warning, per
NEXT_AUTOMATED_TELEMETRY_COLLECTION_IMPLEMENTATION.md Stage 7.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "examples" / "apim-ai-gateway-policy.xml"

#: Substrings that would indicate the *executable* policy reads a payload or
#: a credential. Checked only against the policy body, with XML comments
#: stripped, so explanatory prose (which legitimately names the things this
#: policy avoids) cannot produce a false positive.
FORBIDDEN_SUBSTRINGS = (
    "context.Request.Body",
    "context.Response.Body",
    '"messages"',
    '"choices"',
    'Headers.GetValueOrDefault("Authorization"',
    'Headers.GetValueOrDefault("Ocp-Apim-Subscription-Key"',
    'name="Authorization"',
    'name="Ocp-Apim-Subscription-Key"',
)


def _strip_xml_comments(text: str) -> str:
    import re

    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def test_policy_example_file_exists():
    assert POLICY_PATH.is_file(), "examples/apim-ai-gateway-policy.xml must ship as documentation"


def test_policy_example_is_well_formed_xml():
    # A malformed policy would fail silently for anyone who copies it into
    # APIM; parsing it here catches that before it reaches a user.
    tree = ET.parse(POLICY_PATH)
    assert tree.getroot().tag == "policies"


def test_policy_example_never_touches_payload_or_credentials():
    body = _strip_xml_comments(POLICY_PATH.read_text(encoding="utf-8"))
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in body, f"policy example body must not reference {forbidden!r}"


def test_policy_example_warns_that_apim_support_differs_by_model_api():
    text = POLICY_PATH.read_text(encoding="utf-8").casefold()
    assert "warning" in text
    assert "differ" in text
    # Must name at least the documented Azure OpenAI-specific policy so a
    # reader knows it does not apply uniformly to every backend.
    assert "azure-openai-emit-token-metric" in text


def test_policy_example_emits_documented_metadata_only_elements():
    text = POLICY_PATH.read_text(encoding="utf-8")
    # Both the Azure OpenAI-specific and the fully generic metric emitters
    # are documented, built-in APIM policy elements.
    assert "<azure-openai-emit-token-metric" in text
    assert "<emit-metric" in text
    assert "StatusCode" in text
    assert "Deployment" in text


def test_policy_example_preserves_required_metadata_dimensions():
    root = ET.parse(POLICY_PATH).getroot()
    outbound = root.find("outbound")
    assert outbound is not None
    tags = {child.tag for child in outbound}
    assert {"azure-openai-emit-token-metric", "emit-metric", "base"}.issubset(tags)
