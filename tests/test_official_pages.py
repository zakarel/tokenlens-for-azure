import pytest

from tokenlens.pricing_sources.official_pages import (
    fetch_official_page,
    official_page_url,
    page_catalog,
    parse_official_page,
    sync_official_pages,
)
from tokenlens.pricing_sources.fetch import FetchedPage


HTML = """
<table>
  <thead><tr><th>Models</th><th>Price</th></tr></thead>
  <tbody>
    <tr><td>Mistral Medium 3.5 Global</td><td>Input: $1.50/1M Tokens Output: $7.50/1M Tokens</td></tr>
    <tr><td>Mistral OCR</td><td>$1/1K pages</td></tr>
  </tbody>
</table>
"""


def test_official_page_parser_preserves_components_and_provenance():
    rates = parse_official_page(HTML, provider_route="mistral")
    assert rates[0].components == {"input": 1.5, "output": 7.5}
    assert rates[0].unit == "/1M Tokens"
    assert rates[0].source_url.endswith("/mistral-ai/")
    assert rates[0].content_hash.startswith("sha256:")
    assert rates[1].components == {"unit": 1.0}


def test_page_routes_are_exactly_allow_listed():
    assert official_page_url("grok").endswith("/grok/")
    with pytest.raises(Exception):
        official_page_url("https://evil.example/")


def test_parser_fails_closed_on_changed_price_syntax():
    with pytest.raises(ValueError, match="unrecognized"):
        parse_official_page(
            "<table><tr><th>Models</th><th>Price</th></tr><tr><td>New</td><td>Contact sales</td></tr></table>",
            provider_route="grok",
        )


def test_page_catalog_excludes_native_unit_rows_without_inventing_token_rates():
    catalog = page_catalog(parse_official_page(HTML, provider_route="mistral"))
    assert len(catalog.prices) == 1
    assert catalog.prices[0].model == "Mistral Medium 3.5 Global"
    assert catalog.prices[0].source_url.endswith("/mistral-ai/")


def test_fine_tuning_table_keeps_training_hosting_and_inference_components_separate():
    html = """
    <table><tr><th>Models</th><th>Training per 1M tokens</th>
    <th>Hosting per hour</th><th>Input Usage per 1M tokens</th>
    <th>Output Usage per 1M tokens</th></tr>
    <tr><td>Mistral Ministral 3B Global</td><td>$1</td><td>$0.65</td>
    <td>$0.05</td><td>$0.15</td></tr></table>
    """
    rates = parse_official_page(html, provider_route="fine_tuning")
    assert rates[0].components == {
        "training": 1.0,
        "hosting": 0.65,
        "input": 0.05,
        "output": 0.15,
    }
    catalog = page_catalog(rates)
    assert catalog.prices[0].model == "ministral-3b"
    assert catalog.prices[0].input_per_million == 0.05
    assert catalog.prices[0].output_per_million == 0.15


def test_official_page_fetch_uses_allow_list_and_bounded_transport():
    def transport(url, timeout, max_bytes):
        assert url.endswith("/mistral-ai/")
        return FetchedPage(url=url, status=200, body=HTML.encode())

    rates = fetch_official_page("mistral", transport=transport)
    assert len(rates) == 2


def test_official_page_sync_writes_complete_private_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKENLENS_CONFIG_DIR", str(tmp_path))

    def transport(url, timeout, max_bytes):
        return FetchedPage(url=url, status=200, body=HTML.encode())

    snapshot, rates = sync_official_pages(["mistral"], transport=transport)
    assert snapshot.source == "official_foundry_pages"
    assert snapshot.feed_complete is True
    assert snapshot.rows_read == len(rates)
    assert (tmp_path / "pricing" / "official-foundry-pages.json").is_file()
