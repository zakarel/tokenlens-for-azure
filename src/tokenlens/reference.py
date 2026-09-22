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

from .pricing import PricingCatalog


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
    if public is None:
        return packaged
    retrieved = max(
        [value for value in (public.retrieved_at, packaged.retrieved_at) if value is not None],
        default=None,
    )
    return PricingCatalog(
        currency=packaged.currency,
        catalog_name=f"{PUBLIC_CATALOG_NAME}+{packaged.catalog_name}",
        source_url=public.source_url or packaged.source_url,
        retrieved_at=retrieved,
        # Both sides are dated snapshots of current published pricing, so the
        # merged catalog keeps the packaged catalog's analysis-date semantics
        # rather than silently changing how either side is applied.
        pricing_basis="analysis_date_snapshot",
        assumptions=public.assumptions,
        prices=[*public.prices, *packaged.prices],
    )
