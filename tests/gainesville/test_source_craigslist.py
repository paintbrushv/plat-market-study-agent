from __future__ import annotations

from pathlib import Path

from etl.gainesville.sources.craigslist import (
    filter_rss_items,
    parse_post_html,
    parse_rss,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_rss_extracts_items() -> None:
    items = parse_rss((FIXTURES / "craigslist_search.rss").read_text(encoding="utf-8"))
    assert len(items) == 3
    titles = [i.title for i in items]
    assert "3BR/2BA in Gainesville TX - $1500" in titles


def test_filter_rss_items_keyword_match() -> None:
    items = parse_rss((FIXTURES / "craigslist_search.rss").read_text(encoding="utf-8"))
    keep = filter_rss_items(items, keywords=["gainesville", "76240"])
    assert len(keep) == 1
    assert "Gainesville" in keep[0].title


def test_parse_post_extracts_full_record() -> None:
    html = (FIXTURES / "craigslist_post.html").read_text(encoding="utf-8")
    post = parse_post_html(html)
    assert post.beds == 3.0
    assert post.baths == 2.0
    assert post.sqft == 1450
    assert post.asking_rent == 1500
    assert post.lat == 33.626
    assert post.lon == -97.133
    assert post.zip == "76240"
    assert "Gainesville" in (post.body or "")
    assert post.source_listing_id == "7654321"


def test_parse_post_address_from_mapaddress() -> None:
    """Existing mapaddress block is still extracted as before."""
    html = (FIXTURES / "craigslist_post.html").read_text(encoding="utf-8")
    post = parse_post_html(html)
    assert post.address_raw == "123 Main St near 1st Ave"


def test_parse_post_address_from_jsonld() -> None:
    """When mapaddress absent, JSON-LD RealEstateListing streetAddress is used."""
    html = (FIXTURES / "craigslist_post_jsonld.html").read_text(encoding="utf-8")
    post = parse_post_html(html)
    assert post.address_raw is not None
    assert "801 Oak Street" in post.address_raw
    assert "Gainesville" in post.address_raw


def test_parse_post_address_from_body() -> None:
    """When neither mapaddress nor JSON-LD present, body street-address regex fires."""
    html = (FIXTURES / "craigslist_post_bodyaddr.html").read_text(encoding="utf-8")
    post = parse_post_html(html)
    assert post.address_raw is not None
    assert "426" in post.address_raw
    assert "Clements" in post.address_raw


def test_parse_post_no_address_stays_empty() -> None:
    """Post with no address anywhere leaves address_raw empty and normalization None."""
    from etl.gainesville.address_normalize import normalize_address

    html = (FIXTURES / "craigslist_post_noaddr.html").read_text(encoding="utf-8")
    post = parse_post_html(html)
    # address_raw is empty string (not None) because no source matched
    assert post.address_raw == ""
    # normalize_address on empty string should return None — no fabrication
    assert normalize_address(post.address_raw) is None
