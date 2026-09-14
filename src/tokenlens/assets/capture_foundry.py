"""Capture one explicit Microsoft Foundry/Azure OpenAI call with Entra ID.

Install optional dependencies with ``.venv/bin/python -m pip install -e '.[foundry]'``
and authenticate with ``az login``. No API key or bearer token is written to the
trace. A request is never made until both --deployment and --prompt are supplied.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one Entra-authenticated Foundry request.")
    parser.add_argument("--deployment", required=True, help="The deployment name to call.")
    parser.add_argument("--prompt", required=True, help="The prompt to send.")
    parser.add_argument("--output", default="foundry-traces/capture.jsonl", help="JSONL output path.")
    parser.add_argument("--endpoint", default=os.getenv("AZURE_OPENAI_ENDPOINT") or os.getenv("FOUNDRY_ENDPOINT"))
    parser.add_argument("--api-version", default=os.getenv("OPENAI_API_VERSION", "2024-10-21"))
    parser.add_argument("--model", default=None, help="Optional model name hint.")
    parser.add_argument("--workload", default="manual-capture")
    parser.add_argument("--tenant", default="redacted")
    args = parser.parse_args()
    if not args.endpoint:
        parser.error("set AZURE_OPENAI_ENDPOINT or pass --endpoint")

    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI
    except ImportError as exc:
        parser.error("install optional dependencies with: .venv/bin/python -m pip install -e '.[foundry]'")
        raise AssertionError from exc

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    client = AzureOpenAI(
        azure_endpoint=args.endpoint,
        api_version=args.api_version,
        azure_ad_token_provider=token_provider,
    )
    started = time.perf_counter()
    response = client.chat.completions.create(
        model=args.deployment,
        messages=[{"role": "user", "content": args.prompt}],
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    usage = response.usage
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "request_id": response.id,
        "deployment_name": args.deployment,
        "model_name": response.model or args.model or "unknown",
        "provider": "azure_foundry",
        "request": {
            "model": args.deployment,
            "messages": [{"role": "user", "content": args.prompt}],
        },
        "response": {"model": response.model or args.model or "unknown"},
        "usage": {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            "cached_tokens": getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
        },
        "latency_ms": latency_ms,
        "status_code": 200,
        "metadata": {"workload": args.workload, "tenant": args.tenant},
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"Captured 1 record to {destination.resolve()} (deployment={args.deployment})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
