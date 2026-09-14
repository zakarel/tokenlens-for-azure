"""Mirror existing GenAI OpenTelemetry spans into canonical TokenLens JSONL.

The processor consumes spans your application already produces. It does not
replace your exporter, does not require an OTLP endpoint, and discards every
content-bearing attribute.

    python -m pip install "tokenlens-azure[otel]"

Already exporting spans elsewhere? Import a saved export instead:

    tokenlens-azure import-otel spans.json --output-dir tokenlens-traces
"""

from __future__ import annotations


def install(provider) -> None:
    """Attach the TokenLens processor to an existing TracerProvider."""
    from tokenlens.telemetry.otel import TokenLensSpanProcessor

    provider.add_span_processor(TokenLensSpanProcessor(output_dir="tokenlens-traces"))


def main() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    # Your own exporters stay exactly as they are.
    install(provider)
    trace.set_tracer_provider(provider)
    print("TokenLens span processor installed; spans mirror to tokenlens-traces/")


if __name__ == "__main__":
    main()
