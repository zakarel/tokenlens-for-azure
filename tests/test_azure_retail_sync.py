"""Azure Retail Prices synchronization: allow-list, bounds, parser, and cache.

Every test runs against committed synthetic fixtures through an injected
transport. The suite-wide guard in ``conftest.py`` makes a real socket
impossible, so a regression that reintroduces network access fails loudly.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, date, datetime
from urllib.parse import quote, unquote
from pathlib import Path

import pytest

from tokenlens.pricing_sources import azure_retail
from tokenlens.pricing_sources.azure_retail import (
    RETAIL_ALLOW_LIST,
    RETAIL_PRICES_URL,
    RetailRow,
    build_initial_url,
    crosswalk_product_names,
    crosswalk_rows,
    parse_meter,
    parse_retail_page,
    retail_snapshot,
    rows_from_payload,
    to_per_million,
    units_per_measure,
)
from tokenlens.pricing_sources.cache import (
    AZURE_RETAIL_SNAPSHOT,
    load_snapshot,
    snapshot_is_stale,
    snapshot_path,
    write_snapshot,
)
from tokenlens.pricing_sources.fetch import (
    AllowListError,
    BudgetExceededError,
    FetchBudget,
    FetchError,
    FetchedPage,
    PageParse,
    fetch_pages,
    redirect_probe,
)

FIXTURES = Path(__file__).parent / "fixtures" / "pricing"
AS_OF = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


def _page(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _rows(*names: str) -> list[RetailRow]:
    rows: list[RetailRow] = []
    for name in names:
        rows.extend(rows_from_payload(json.loads(_page(name))))
    return rows


def _transport(pages: dict[str, bytes]):
    """Serve fixture bytes for the initial URL and its continuation."""

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        body = pages["skip"] if "skip" in url else pages["first"]
        return FetchedPage(url=url, status=200, body=body)

    return transport


def _entry(entries, model: str):
    return next(item for item in entries if item.model == model)


# --- Allow-list -------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://prices.azure.com/api/retail/prices",
        "https://prices.azure.com.evil.test/api/retail/prices",
        "https://prices.azure.com/api/retail/../admin",
        "https://prices.azure.com/api/retail/prices2",
        "https://prices.azure.com:8443/api/retail/prices",
        "https://user:secret@prices.azure.com/api/retail/prices",
        "https://management.azure.com/api/retail/prices",
        "file:///etc/passwd",
    ],
)
def test_only_the_exact_official_host_and_path_are_accepted(url):
    with pytest.raises(AllowListError):
        RETAIL_ALLOW_LIST.validate(url)


def test_the_official_url_and_its_port_443_form_are_accepted():
    assert RETAIL_ALLOW_LIST.validate(build_initial_url())
    assert RETAIL_ALLOW_LIST.validate(
        "https://prices.azure.com:443/api/retail/prices?api-version=2023-01-01-preview&%24skip=100"
    )


def test_the_initial_url_filters_to_foundry_models():
    url = build_initial_url(region="eastus2")
    assert url.startswith(RETAIL_PRICES_URL + "?")
    assert "serviceName%20eq%20%27Foundry%20Models%27" in url
    assert "armRegionName%20eq%20%27eastus2%27" in url
    assert "api-version=2023-01-01-preview" in url


def test_a_hostile_continuation_link_is_refused_before_any_request():
    requested: list[str] = []

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        requested.append(url)
        return FetchedPage(url=url, status=200, body=b"{}")

    pages = fetch_pages(
        build_initial_url(),
        allow_list=RETAIL_ALLOW_LIST,
        parse=lambda page: PageParse({}, "https://attacker.invalid/api/retail/prices", 0),
        transport=transport,
    )
    with pytest.raises(AllowListError):
        list(pages)
    assert requested == [build_initial_url()]


# --- Redirects --------------------------------------------------------------


def test_a_cross_host_redirect_is_refused():
    with pytest.raises(AllowListError, match="not in the pricing source allow-list"):
        redirect_probe(
            RETAIL_ALLOW_LIST,
            from_url=build_initial_url(),
            to_url="https://attacker.invalid/api/retail/prices",
        )


def test_an_https_to_http_redirect_is_refused():
    with pytest.raises(AllowListError, match="Only https is allowed"):
        redirect_probe(
            RETAIL_ALLOW_LIST,
            from_url=build_initial_url(),
            to_url="http://prices.azure.com/api/retail/prices",
        )


def test_a_redirect_to_another_path_on_the_same_host_is_refused():
    with pytest.raises(AllowListError, match="not in the pricing source allow-list"):
        redirect_probe(
            RETAIL_ALLOW_LIST,
            from_url=build_initial_url(),
            to_url="https://prices.azure.com/api/retail/admin",
        )


def test_an_allow_listed_redirect_is_permitted():
    request = redirect_probe(
        RETAIL_ALLOW_LIST,
        from_url=build_initial_url(),
        to_url="https://prices.azure.com:443/api/retail/prices?%24skip=100",
    )
    assert request is not None
    assert request.full_url.startswith("https://prices.azure.com")


def test_the_final_validated_url_is_recorded_on_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    redirected = "https://prices.azure.com:443/api/retail/prices?api-version=2023-01-01-preview"

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        name = "azure_retail_page2.json" if "skip" in url else "azure_retail_page1.json"
        return FetchedPage(url=url, status=200, body=_page(name), final_url=redirected)

    snapshot = retail_snapshot(transport=transport, now=NOW)
    assert snapshot.source_url == redirected


# --- Pagination and bounds --------------------------------------------------


def test_validated_continuation_links_are_followed_once_each():
    seen: list[str] = []
    pages = {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        seen.append(url)
        return FetchedPage(url=url, status=200, body=pages["skip"] if "skip" in url else pages["first"])

    collected = list(
        fetch_pages(
            build_initial_url(),
            allow_list=RETAIL_ALLOW_LIST,
            parse=parse_retail_page,
            transport=transport,
        )
    )
    assert len(collected) == 2
    assert len(seen) == 2 and seen[0] != seen[1]
    # Every page is parsed exactly once, with a stable index and content hash.
    assert [item.index for item in collected] == [0, 1]
    assert all(item.content_hash.startswith("sha256:") for item in collected)
    assert collected[0].content_hash != collected[1].content_hash
    assert collected[0].payload["Items"], "the payload is carried, not re-parsed"


def test_each_page_body_is_parsed_exactly_once():
    calls: list[int] = []

    def parse(page: FetchedPage) -> PageParse:
        calls.append(len(page.body))
        return parse_retail_page(page)

    pages = {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
    list(
        fetch_pages(
            build_initial_url(),
            allow_list=RETAIL_ALLOW_LIST,
            parse=parse,
            transport=lambda url, t, m: FetchedPage(
                url=url, status=200, body=pages["skip"] if "skip" in url else pages["first"]
            ),
        )
    )
    assert len(calls) == 2


def test_the_page_budget_raises_rather_than_returning_a_truncated_feed():
    counter = {"n": 0}

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        counter["n"] += 1
        # A distinct URL each time defeats the repeat-URL guard on purpose.
        return FetchedPage(
            url=url,
            status=200,
            body=json.dumps(
                {
                    "Items": [],
                    "NextPageLink": f"https://prices.azure.com/api/retail/prices?page={counter['n']}",
                }
            ).encode(),
        )

    with pytest.raises(BudgetExceededError) as excinfo:
        list(
            fetch_pages(
                build_initial_url(),
                allow_list=RETAIL_ALLOW_LIST,
                parse=parse_retail_page,
                budget=FetchBudget(max_pages=3, retries=0),
                transport=transport,
            )
        )
    assert excinfo.value.limit == "pages"
    assert "truncated" in str(excinfo.value)
    assert counter["n"] == 3


def test_the_item_budget_raises_rather_than_returning_a_truncated_feed():
    counter = {"n": 0}

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        counter["n"] += 1
        return FetchedPage(
            url=url,
            status=200,
            body=json.dumps(
                {
                    "Items": [{}] * 5,
                    "NextPageLink": f"https://prices.azure.com/api/retail/prices?p={counter['n']}",
                }
            ).encode(),
        )

    with pytest.raises(BudgetExceededError) as excinfo:
        list(
            fetch_pages(
                build_initial_url(),
                allow_list=RETAIL_ALLOW_LIST,
                parse=parse_retail_page,
                budget=FetchBudget(max_pages=50, max_items=9, retries=0),
                transport=transport,
            )
        )
    assert excinfo.value.limit == "items"


def test_a_complete_feed_inside_the_budget_does_not_raise():
    pages = {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
    collected = list(
        fetch_pages(
            build_initial_url(),
            allow_list=RETAIL_ALLOW_LIST,
            parse=parse_retail_page,
            budget=FetchBudget(max_pages=5, max_items=500, retries=0),
            transport=lambda url, t, m: FetchedPage(
                url=url, status=200, body=pages["skip"] if "skip" in url else pages["first"]
            ),
        )
    )
    assert len(collected) == 2 and collected[-1].next_url is None


def test_an_oversized_page_raises_a_byte_budget_error():
    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        return FetchedPage(url=url, status=200, body=b"x" * 64)

    with pytest.raises(BudgetExceededError) as excinfo:
        list(
            fetch_pages(
                build_initial_url(),
                allow_list=RETAIL_ALLOW_LIST,
                parse=parse_retail_page,
                budget=FetchBudget(max_bytes_per_page=16, retries=2, sleep=lambda _: None),
                transport=transport,
            )
        )
    assert excinfo.value.limit == "bytes"


def test_a_transient_failure_is_retried_within_the_budget():
    attempts = {"n": 0}
    slept: list[float] = []

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise FetchError("synthetic 503")
        return FetchedPage(url=url, status=200, body=b'{"Items": [], "NextPageLink": null}')

    pages = list(
        fetch_pages(
            build_initial_url(),
            allow_list=RETAIL_ALLOW_LIST,
            parse=lambda page: PageParse({}, None, 0),
            budget=FetchBudget(retries=2, sleep=slept.append),
            transport=transport,
        )
    )
    assert len(pages) == 1
    assert attempts["n"] == 3
    assert slept == [0.5, 1.0]


def test_retries_are_bounded_and_the_failure_surfaces():
    attempts = {"n": 0}

    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        attempts["n"] += 1
        raise FetchError("synthetic outage")

    with pytest.raises(FetchError):
        list(
            fetch_pages(
                build_initial_url(),
                allow_list=RETAIL_ALLOW_LIST,
                parse=lambda page: PageParse({}, None, 0),
                budget=FetchBudget(retries=1, sleep=lambda _: None),
                transport=transport,
            )
        )
    assert attempts["n"] == 2


def test_a_non_200_response_is_retried_then_reported():
    def transport(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        return FetchedPage(url=url, status=503, body=b"")

    with pytest.raises(FetchError, match="503"):
        list(
            fetch_pages(
                build_initial_url(),
                allow_list=RETAIL_ALLOW_LIST,
                parse=lambda page: PageParse({}, None, 0),
                budget=FetchBudget(retries=1, sleep=lambda _: None),
                transport=transport,
            )
        )


# --- Unit conversion --------------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "expected"),
    [("1K", 1_000.0), ("1M", 1_000_000.0), ("10K", 10_000.0), ("100", 100.0), ("1k tokens", 1_000.0)],
)
def test_supported_units_of_measure(unit, expected):
    assert units_per_measure(unit) == expected


@pytest.mark.parametrize("unit", ["", "1/Hour", "Hours", "unit"])
def test_unsupported_units_of_measure_are_rejected(unit):
    assert units_per_measure(unit) is None


def test_thousand_unit_rates_convert_to_per_million_and_million_rates_pass_through():
    assert to_per_million(0.00004, "1K") == pytest.approx(0.04)
    assert to_per_million(0.20, "1M") == pytest.approx(0.20)


# --- Crosswalk: GPT-5.6 Luna ------------------------------------------------


def test_luna_input_output_cached_and_cache_write_rates_are_exact():
    result = crosswalk_rows(_rows("azure_retail_page1.json"), as_of=AS_OF)
    entry = _entry(result.entries, "gpt-5.6-luna")
    assert entry.region == "global"
    assert entry.context_window == "short"
    assert entry.service_tier == "standard"
    assert entry.billing_basis == "token_rate"
    assert entry.confidence == "verified"
    assert entry.input_per_million == pytest.approx(0.20)
    assert entry.output_per_million == pytest.approx(1.20)
    assert entry.cached_input_per_million == pytest.approx(0.02)
    assert entry.cache_write_per_million == pytest.approx(0.25)
    # Every priced dimension names the exact meter it came from.
    assert set(entry.meter_ids) == {"input", "output", "cached_input", "cache_write"}
    assert entry.source_url == RETAIL_PRICES_URL


def test_a_deployment_model_name_matches_the_published_meter_label():
    """`gpt-5.6-luna` must match the retail label `5.6 luna`."""
    result = crosswalk_rows(_rows("azure_retail_page1.json"), as_of=AS_OF)
    entry = _entry(result.entries, "gpt-5.6-luna")
    assert entry.model_match_rank("gpt-5.6-luna") == 0
    assert entry.model_match_rank("GPT 5.6 Luna") == 0
    assert entry.model_match_rank("gpt-5.4") is None


def test_long_context_data_zone_priority_flex_and_batch_meters_are_excluded():
    rows = _rows("azure_retail_page1.json")
    priced_ids = {
        meter_id
        for entry in crosswalk_rows(rows, as_of=AS_OF).entries
        for meter_id in entry.meter_ids.values()
    }
    for row in rows:
        label = row.meter_name.casefold()
        if any(marker in label for marker in (" longco ", " dz ", " pp ", " fl ", " batch ", "provisioned")):
            assert row.meter_id not in priced_ids, row.meter_name


def test_a_future_effective_date_is_not_applied_yet():
    rows = _rows("azure_retail_page1.json")
    entry = _entry(crosswalk_rows(rows, as_of=AS_OF).entries, "gpt-5.6-luna")
    # The 2026-12-01 row prices input at 0.18 and must not win on 2026-09-22.
    assert entry.input_per_million == pytest.approx(0.20)
    later = _entry(crosswalk_rows(rows, as_of=date(2026, 12, 2)).entries, "gpt-5.6-luna")
    assert later.input_per_million == pytest.approx(0.18)


def test_tiered_and_non_consumption_rows_are_never_priced():
    rows = _rows("azure_retail_page1.json")
    tiered = next(row for row in rows if row.tier_minimum_units)
    non_consumption = next(row for row in rows if row.type != "Consumption")
    assert parse_meter(tiered).reason == "tiered-meter"
    assert parse_meter(non_consumption).reason == "non-consumption-meter"


def test_identical_global_rates_across_regions_are_deduped_with_provenance():
    result = crosswalk_rows(_rows("azure_retail_page1.json"), as_of=AS_OF)
    entry = _entry(result.entries, "gpt-5.6-luna")
    assert "eastus2" in (entry.note or "")
    assert len([item for item in result.entries if item.model == "gpt-5.6-luna"]) == 1


def test_conflicting_regional_rates_are_quarantined_unless_a_region_is_given():
    rows = list(_rows("azure_retail_page1.json"))
    conflicting = RetailRow(
        meter_id="99999999-0000-0000-0000-000000000001",
        meter_name="5.6 luna ShortCo Inp Std Gl 1M Tokens",
        sku_name="5.6 luna ShortCo Inp Std Gl",
        arm_sku_name="56 luna ShortCo Inp Std Gl",
        product_name="Azure OpenAI GPT5",
        unit_of_measure="1M",
        retail_price=0.99,
        arm_region_name="westus3",
        currency_code="USD",
        effective_start_date="2026-08-01T00:00:00Z",
    )
    rows.append(conflicting)
    clashing = crosswalk_rows(rows, as_of=AS_OF)
    assert any(item.reason == "region-rate-conflict" for item in clashing.quarantined)
    assert not any(item.model == "gpt-5.6-luna" for item in clashing.entries)

    # The account's own region resolves the clash without averaging anything.
    resolved = crosswalk_rows(rows, as_of=AS_OF, account_region="eastus2")
    assert _entry(resolved.entries, "gpt-5.6-luna").input_per_million == pytest.approx(0.20)


def test_an_unrecognized_meter_token_is_quarantined_rather_than_guessed():
    row = RetailRow(
        meter_id="aaaaaaaa-0000-0000-0000-000000000001",
        meter_name="5.6 luna ShortCo Inp Std Gl Turbo 1M Tokens",
        sku_name="5.6 luna ShortCo Inp Std Gl Turbo",
        arm_sku_name="56 luna ShortCo Inp Std Gl Turbo",
        product_name="Azure OpenAI GPT5",
        unit_of_measure="1M",
        retail_price=0.20,
        arm_region_name="eastus2",
        currency_code="USD",
        effective_start_date="2026-08-01T00:00:00Z",
    )
    rejection = parse_meter(row)
    assert rejection.reason == "unrecognized-meter-token"
    assert rejection.quarantine is True
    assert "turbo" in (rejection.detail or "")


def test_a_meter_naming_both_input_and_output_is_quarantined():
    row = RetailRow(
        meter_id="aaaaaaaa-0000-0000-0000-000000000002",
        meter_name="5.6 luna ShortCo Inp Opt Std Gl 1M Tokens",
        sku_name="5.6 luna ShortCo Inp Opt Std Gl",
        arm_sku_name="56 luna ShortCo Inp Opt Std Gl",
        product_name="Azure OpenAI GPT5",
        unit_of_measure="1M",
        retail_price=0.20,
        arm_region_name="eastus2",
        currency_code="USD",
        effective_start_date="2026-08-01T00:00:00Z",
    )
    assert parse_meter(row).reason == "dimension-ambiguous"


def test_an_incomplete_meter_set_is_quarantined_not_half_priced():
    rows = [
        row
        for row in _rows("azure_retail_page1.json")
        if row.meter_name != "5.6 luna ShortCo Opt Std Gl 1M Tokens"
    ]
    result = crosswalk_rows(rows, as_of=AS_OF)
    assert not any(item.model == "gpt-5.6-luna" for item in result.entries)
    assert any(item.reason == "incomplete-meter-set" for item in result.quarantined)


def test_a_model_published_only_as_fine_tuning_is_explained_not_silently_dropped():
    """The live retail feed publishes Ministral 3B only with ``-FT`` meters."""
    fine_tuning_only = [
        row for row in _rows("azure_retail_page2.json") if "FT" in row.meter_name
    ]
    assert fine_tuning_only
    result = crosswalk_rows(fine_tuning_only, as_of=AS_OF)
    assert result.entries == []
    record = next(
        item for item in result.quarantined if item.reason == "no-normal-inference-meter-published"
    )
    assert record.meter_name == "ministral-3b"
    assert "published-only-as=fine-tuning" in (record.detail or "")
    assert any("no retail meter matches normal inference" in note for note in result.notes)


def test_a_model_published_only_for_other_dimensions_is_explained():
    long_context_only = [
        row
        for row in _rows("azure_retail_page1.json")
        if "LongCo" in row.meter_name and "Std Gl" in row.meter_name
    ]
    assert long_context_only
    result = crosswalk_rows(long_context_only, as_of=AS_OF)
    assert result.entries == []
    record = next(
        item for item in result.quarantined if item.reason == "no-meter-for-requested-dimensions"
    )
    assert record.meter_name == "gpt-5.6-luna"
    assert "global/long-context" in (record.detail or "")


# --- Crosswalk: Ministral 3B ------------------------------------------------


def test_ministral_prices_only_normal_inference_and_never_fine_tuning():
    result = crosswalk_rows(_rows("azure_retail_page2.json"), as_of=AS_OF)
    entry = _entry(result.entries, "ministral-3b")
    # 0.00004 per 1K tokens is 0.04 per million.
    assert entry.input_per_million == pytest.approx(0.04)
    assert entry.output_per_million == pytest.approx(0.04)
    assert entry.meter_ids["input"] == "44444444-0000-0000-0000-000000000001"
    assert all("FT" not in (meter or "") for meter in entry.meter_ids.values())


@pytest.mark.parametrize(
    "meter_name",
    [
        "Ministral 3B Model In-FT Tokens",
        "Ministral 3B Model Out-FT Tokens",
        "Mnstrl 3b Inp FT regnl Tokens",
        "Mnstrl 3B FT Deployment Hosting Unit",
        "Ministral 3bFTregnl Deployment Hosting Unit",
    ],
)
def test_every_ministral_fine_tuning_and_hosting_meter_is_rejected(meter_name):
    row = next(row for row in _rows("azure_retail_page2.json") if row.meter_name == meter_name)
    rejection = parse_meter(row)
    assert rejection.reason in {"excluded-fine-tuning", "non-token-meter"}


def test_a_model_outside_the_crosswalk_is_skipped_not_quarantined():
    codestral = next(
        row for row in _rows("azure_retail_page2.json") if row.meter_name.startswith("Codestral")
    )
    assert parse_meter(codestral).reason == "model-not-in-crosswalk"
    result = crosswalk_rows([codestral], as_of=AS_OF)
    assert result.entries == [] and result.quarantined == []


def test_a_foreign_currency_row_is_ignored_rather_than_converted():
    rows = [row for row in _rows("azure_retail_page2.json") if row.currency_code == "EUR"]
    assert rows, "fixture must contain a non-USD row"
    assert crosswalk_rows(rows, currency="USD", as_of=AS_OF).entries == []


def test_an_unstated_dimension_is_recorded_as_an_explicit_assumption():
    result = crosswalk_rows(_rows("azure_retail_page2.json"), as_of=AS_OF)
    entry = _entry(result.entries, "ministral-3b")
    # The Ministral meters state no deployment or context window, so the
    # documented defaults are applied *and labelled*.
    assert "deployment=global" in entry.assumed_dimensions
    assert "context=short" in entry.assumed_dimensions
    assert "purchase_model=retail" in entry.assumed_dimensions
    assert "deployment=global" in result.assumed_dimensions


def test_an_exact_stated_dimension_overrides_the_default():
    # Luna publishes the deployment mode explicitly, so nothing is assumed there.
    entry = _entry(crosswalk_rows(_rows("azure_retail_page1.json"), as_of=AS_OF).entries, "gpt-5.6-luna")
    assert "deployment=global" not in entry.assumed_dimensions
    assert "context=short" not in entry.assumed_dimensions
    # Asking for the data-zone mode returns the data-zone meters instead.
    dz = crosswalk_rows(_rows("azure_retail_page1.json"), deployments=["data_zone"], as_of=AS_OF)
    assert _entry(dz.entries, "gpt-5.6-luna").input_per_million == pytest.approx(0.22)


def test_asking_for_long_context_returns_the_long_context_meters():
    result = crosswalk_rows(_rows("azure_retail_page1.json"), contexts=["long"], as_of=AS_OF)
    entry = _entry(result.entries, "gpt-5.6-luna")
    assert entry.context_window == "long"
    assert entry.input_per_million == pytest.approx(0.40)


# --- Snapshot ---------------------------------------------------------------


def test_a_snapshot_carries_provenance_and_is_cached_user_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = retail_snapshot(
        region="eastus2",
        account_region="eastus2",
        transport=_transport(
            {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
        ),
        now=NOW,
    )
    assert snapshot.source == "azure_retail_prices"
    assert snapshot.api_version == azure_retail.RETAIL_API_VERSION
    assert snapshot.content_hash.startswith("sha256:")
    assert snapshot.pages_read == 2
    assert {entry.model for entry in snapshot.catalog.prices} == {"gpt-5.6-luna", "ministral-3b"}
    assert all(entry.content_hash == snapshot.content_hash for entry in snapshot.catalog.prices)
    assert all(entry.retrieved_at == NOW for entry in snapshot.catalog.prices)

    path = write_snapshot(snapshot, name=AZURE_RETAIL_SNAPSHOT)
    assert path == snapshot_path(AZURE_RETAIL_SNAPSHOT)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # No temporary artefact survives the atomic replace.
    assert [item.name for item in path.parent.iterdir()] == [AZURE_RETAIL_SNAPSHOT]

    reloaded = load_snapshot(AZURE_RETAIL_SNAPSHOT)
    assert reloaded is not None
    assert reloaded.content_hash == snapshot.content_hash
    assert len(reloaded.catalog.prices) == len(snapshot.catalog.prices)


def test_a_snapshot_write_replaces_the_previous_file_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = retail_snapshot(
        transport=_transport(
            {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
        ),
        now=NOW,
    )
    first = write_snapshot(snapshot, name=AZURE_RETAIL_SNAPSHOT)
    original = first.read_text(encoding="utf-8")
    later = retail_snapshot(
        transport=_transport(
            {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
        ),
        now=datetime(2026, 9, 23, tzinfo=UTC),
    )
    second = write_snapshot(later, name=AZURE_RETAIL_SNAPSHOT)
    assert second == first
    assert second.read_text(encoding="utf-8") != original
    assert stat.S_IMODE(second.stat().st_mode) == 0o600
    assert len(list(second.parent.iterdir())) == 1


def test_a_corrupt_cache_file_is_ignored_rather_than_trusted(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    target = snapshot_path(AZURE_RETAIL_SNAPSHOT)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    assert load_snapshot(AZURE_RETAIL_SNAPSHOT) is None
    target.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    assert load_snapshot(AZURE_RETAIL_SNAPSHOT) is None


def test_staleness_is_measured_against_the_retrieval_timestamp(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = retail_snapshot(
        transport=_transport(
            {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
        ),
        now=NOW,
    )
    assert snapshot_is_stale(snapshot, max_age_days=7, now=NOW) is False
    assert snapshot_is_stale(snapshot, max_age_days=7, now=datetime(2026, 10, 5, tzinfo=UTC)) is True
    assert snapshot_is_stale(None) is True


def test_os_environment_isolation_keeps_the_real_profile_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    assert str(tmp_path) in str(snapshot_path(AZURE_RETAIL_SNAPSHOT))
    assert os.environ["TOKENLENS_CONFIG_DIR"].startswith(str(tmp_path))


# --- Bounded queries and truncation -----------------------------------------


def test_the_query_is_bounded_to_the_product_families_the_crosswalk_knows():
    url = build_initial_url(region="eastus2")
    assert crosswalk_product_names() == ("Azure Mistral Models", "Azure OpenAI GPT5")
    for name in crosswalk_product_names():
        assert quote(f"productName eq '{name}'", safe="") in url
    assert quote(" or ", safe="") in url
    # A caller can still widen or narrow the bound explicitly.
    assert "productName" not in unquote(build_initial_url(product_names=[]))


def test_a_truncated_feed_is_reported_and_never_packaged(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    counter = {"n": 0}

    def endless(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        counter["n"] += 1
        payload = json.loads(_page("azure_retail_page1.json"))
        payload["NextPageLink"] = f"https://prices.azure.com/api/retail/prices?page={counter['n']}"
        return FetchedPage(url=url, status=200, body=json.dumps(payload).encode())

    with pytest.raises(BudgetExceededError) as excinfo:
        retail_snapshot(
            transport=endless, budget=FetchBudget(max_pages=2, retries=0), now=NOW
        )
    assert excinfo.value.limit == "pages"
    assert load_snapshot(AZURE_RETAIL_SNAPSHOT) is None


def test_a_truncated_read_never_overwrites_a_complete_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    from tokenlens.pricing_sources.sync import sync_public_pricing

    complete = retail_snapshot(
        transport=_transport(
            {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
        ),
        now=NOW,
    )
    write_snapshot(complete, name=AZURE_RETAIL_SNAPSHOT)
    before = snapshot_path(AZURE_RETAIL_SNAPSHOT).read_text(encoding="utf-8")

    counter = {"n": 0}

    def endless(url: str, timeout: float, max_bytes: int) -> FetchedPage:
        counter["n"] += 1
        payload = json.loads(_page("azure_retail_page1.json"))
        payload["NextPageLink"] = f"https://prices.azure.com/api/retail/prices?page={counter['n']}"
        return FetchedPage(url=url, status=200, body=json.dumps(payload).encode())

    report = sync_public_pricing(
        include_claude=False,
        transport=endless,
        budget=FetchBudget(max_pages=2, retries=0),
        now=datetime(2026, 9, 23, tzinfo=UTC),
    )
    outcome = report.outcomes[0]
    assert outcome.ok is False
    assert outcome.feed_complete is False
    assert outcome.used_cache is True
    assert "truncated" in (outcome.reason or "")
    assert snapshot_path(AZURE_RETAIL_SNAPSHOT).read_text(encoding="utf-8") == before


def test_an_incomplete_snapshot_is_refused_by_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = retail_snapshot(
        transport=_transport(
            {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
        ),
        now=NOW,
    )
    truncated = snapshot.model_copy(update={"feed_complete": False})
    with pytest.raises(ValueError, match="truncated pricing feed is never cached"):
        write_snapshot(truncated, name=AZURE_RETAIL_SNAPSHOT)
    assert load_snapshot(AZURE_RETAIL_SNAPSHOT) is None


def test_a_truncated_feed_cannot_claim_a_meter_is_missing():
    fine_tuning_only = [row for row in _rows("azure_retail_page2.json") if "FT" in row.meter_name]
    truncated = crosswalk_rows(fine_tuning_only, as_of=AS_OF, feed_complete=False)
    reasons = {item.reason for item in truncated.quarantined}
    assert reasons == {"feed-truncated-model-unresolved"}
    assert "no-normal-inference-meter-published" not in reasons
    assert any("cannot tell a missing meter from an unread one" in note for note in truncated.notes)

    complete = crosswalk_rows(fine_tuning_only, as_of=AS_OF, feed_complete=True)
    assert "no-normal-inference-meter-published" in {item.reason for item in complete.quarantined}


def test_a_complete_snapshot_records_that_the_feed_was_fully_read(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path / "cfg"))
    snapshot = retail_snapshot(
        transport=_transport(
            {"first": _page("azure_retail_page1.json"), "skip": _page("azure_retail_page2.json")}
        ),
        now=NOW,
    )
    assert snapshot.feed_complete is True
    assert snapshot.currency == "USD"
    assert any("read to completion" in note for note in snapshot.notes)
