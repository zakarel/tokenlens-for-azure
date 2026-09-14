"""Instrument a Claude on Microsoft Foundry client once.

Claude deployments use the Anthropic Messages API. TokenLens never routes a
Claude deployment through OpenAI `/chat/completions`, because a failed call
retried through a different API would create a second billable request.

    python -m pip install "tokenlens-azure[foundry-claude]"
    export FOUNDRY_ENDPOINT="https://YOUR-RESOURCE.services.ai.azure.com/"
"""

from __future__ import annotations

import os

from tokenlens.integrations.anthropic_foundry import instrument_anthropic_foundry


def build_client():
    from anthropic import AnthropicFoundry  # type: ignore[attr-defined]

    client = AnthropicFoundry(base_url=os.environ["FOUNDRY_ENDPOINT"])
    return instrument_anthropic_foundry(
        client,
        deployment_mode="global",
        workload="coding-agent",
    )


def main() -> None:
    client = build_client()
    message = client.messages.create(
        model=os.environ.get("FOUNDRY_CLAUDE_DEPLOYMENT", "example-claude-prod"),
        max_tokens=128,
        messages=[{"role": "user", "content": "Summarize this changelog entry."}],
    )
    # Input, output, cache-read, and cache-write tokens are all captured.
    print(message.content)


if __name__ == "__main__":
    main()
