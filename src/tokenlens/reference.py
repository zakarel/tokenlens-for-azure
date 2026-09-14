"""Load the offline, dated reference catalog bundled with TokenLens."""

from __future__ import annotations

from importlib.resources import files

import yaml

from .pricing import PricingCatalog


def load_bundled_reference_catalog() -> PricingCatalog:
    payload = yaml.safe_load(
        files("tokenlens").joinpath("assets/reference-pricing.yml").read_text(encoding="utf-8")
    )
    return PricingCatalog.model_validate(payload)
