"""Instrument an Azure OpenAI client once; normal traffic produces telemetry.

Run this file's `main()` inside your own application startup, not per prompt.
Telemetry is contentless: prompts, responses, headers, and credentials are
never written.

    python -m pip install "tokenlens-azure[foundry]"
    export AZURE_OPENAI_ENDPOINT="https://YOUR-RESOURCE.openai.azure.com/"
    export TOKENLENS_TELEMETRY_DIR="tokenlens-traces"
    export TOKENLENS_FINGERPRINT_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"

Then analyze offline:

    tokenlens-azure analyze tokenlens-traces --format html --open
"""

from __future__ import annotations

import os

from tokenlens.integrations.openai import instrument_openai


def build_client():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    from openai import AzureOpenAI

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        azure_ad_token_provider=token_provider,
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )
    # Instrument once, at startup. `deployment_mode` is never inferred: PTU
    # sizing and pricing differ between Global and Regional deployments.
    return instrument_openai(
        client,
        deployment_mode="global",
        workload="support-assistant",
        measure_tokens=False,
    )


def main() -> None:
    client = build_client()
    # Every normal call now emits one canonical, contentless telemetry record.
    response = client.chat.completions.create(
        model=os.environ.get("AZURE_OPENAI_DEPLOYMENT", "example-support-prod"),
        messages=[{"role": "user", "content": "Classify this request as billing or support."}],
        max_tokens=64,
    )
    print(response.choices[0].message.content)


if __name__ == "__main__":
    main()
