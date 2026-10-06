"""Reference pricing: the cached official sync first, then the packaged snapshot.

``load_bundled_reference_catalog`` stays exactly what it was — the dated,
offline catalog shipped inside the package. ``load_effective_reference_catalog``
is what analysis uses: it prefers a locally cached snapshot synchronized from an
official source and falls back to the packaged catalog for anything that
snapshot does not cover.
"""

from __future__ import annotations

from importlib.resources import files

import yaml
import json
from pathlib import Path

from .pricing import PricingCatalog
from .foundry_workflow.configuration import user_config_dir


def load_bundled_reference_catalog() -> PricingCatalog:
    payload = yaml.safe_load(
        files("tokenlens").joinpath("assets/reference-pricing.yml").read_text(encoding="utf-8")
    )
    return PricingCatalog.model_validate(payload)


def load_effective_reference_catalog(*, use_public_cache: bool = True) -> PricingCatalog:
    """Merge the cached public sync ahead of the packaged catalog.

    Entries synchronized from an official source carry ``source_rank=10`` and
    therefore win over a packaged entry for the same model and deployment mode,
    while a customer override still wins over both.

    The packaged catalog is the tool's own denomination and is **never**
    dropped. Only snapshots in that same currency are merged; a snapshot in
    another currency is excluded, with the reason exposed through
    ``public_pricing_provenance``, because TokenLens never converts currencies.
    """
    packaged = load_bundled_reference_catalog()
    if not use_public_cache:
        return packaged
    from .pricing_sources.sync import PUBLIC_CATALOG_NAME, public_pricing_catalog

    public = public_pricing_catalog(currency=packaged.currency)
    imported_contract = _load_imported_contract_catalog(currency=packaged.currency)
    if public is None and imported_contract is None:
        return packaged
    retrieved = max(
        [value for value in (
            public.retrieved_at if public else None,
            imported_contract.retrieved_at if imported_contract else None,
            packaged.retrieved_at,
        ) if value is not None],
        default=None,
    )
    public_prices = public.prices if public else []
    contract_prices = imported_contract.prices if imported_contract else []
    return PricingCatalog(
        currency=packaged.currency,
        catalog_name=f"{PUBLIC_CATALOG_NAME}+{packaged.catalog_name}",
        source_url=(public.source_url if public else None) or packaged.source_url,
        retrieved_at=retrieved,
        # Both sides are dated snapshots of current published pricing, so the
        # merged catalog keeps the packaged catalog's analysis-date semantics
        # rather than silently changing how either side is applied.
        pricing_basis="analysis_date_snapshot",
        assumptions=(public.assumptions if public else None),
        prices=[*contract_prices, *public_prices, *packaged.prices],
    )


def _load_imported_contract_catalog(*, currency: str) -> PricingCatalog | None:
    target = user_config_dir() / "pricing-imports" / "contract.json"
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        prices = []
        by_model: dict[str, dict[str, dict]] = {}
        for row in rows:
            if not isinstance(row, dict) or str(row.get("currency", currency)).upper() != currency.upper():
                continue
            by_model.setdefault(str(row["model"]), {})[str(row["component"])] = row
        for model, components in by_model.items():
            if "input_tokens" not in components or "output_tokens" not in components:
                continue
            input_row = components["input_tokens"]
            output_row = components["output_tokens"]
            prices.append({
                "provider": "contract",
                "model": model,
                "effective_from": input_row["effective_from"],
                "input_per_million": float(input_row["unit_price"]) * (1_000_000 / float(input_row.get("unit_multiplier", 1))),
                "output_per_million": float(output_row["unit_price"]) * (1_000_000 / float(output_row.get("unit_multiplier", 1))),
                "confidence": "customer_override",
                "source_rank": 1,
                "note": f"Imported contract rows {input_row.get('source_record_id', 'unknown')}/{output_row.get('source_record_id', 'unknown')}",
            })
        if not prices:
            return None
        return PricingCatalog(
            currency=currency.upper(),
            catalog_name="imported-contract",
            retrieved_at=None,
            prices=prices,
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
