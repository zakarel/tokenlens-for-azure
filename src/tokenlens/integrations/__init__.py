"""Provider instrumentation wrappers.

Each wrapper is imported lazily so the offline analyzer never requires a
provider SDK. Import the specific module you need:

``from tokenlens.integrations.openai import instrument_openai``
``from tokenlens.integrations.anthropic_foundry import instrument_anthropic_foundry``
"""

from __future__ import annotations

__all__ = ["instrument_openai", "instrument_anthropic_foundry"]


def __getattr__(name: str):
    if name == "instrument_openai":
        from .openai import instrument_openai

        return instrument_openai
    if name == "instrument_anthropic_foundry":
        from .anthropic_foundry import instrument_anthropic_foundry

        return instrument_anthropic_foundry
    raise AttributeError(name)
