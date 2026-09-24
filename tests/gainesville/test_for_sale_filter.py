"""Tests for the is_for_sale_disguised detector in validate.py."""

from __future__ import annotations

import datetime as dt

from etl.gainesville.dataclasses import RawObservation
from etl.gainesville.validate import is_for_sale_disguised


def _obs(*, title: str | None = None, body: str | None = None) -> RawObservation:
    return RawObservation(
        observation_id="x",
        run_id="r",
        source="craigslist",
        source_listing_id="sl",
        url="https://dallas.craigslist.org/ndf/apa/1234.html",
        scraped_at=dt.datetime(2026, 5, 11, tzinfo=dt.UTC),
        listing_kind="sfr",
        address_raw="426 N Clements St",
        address_normalized=None,
        addr_norm_version=1,
        city="Gainesville",
        zip="76240",
        lat=None,
        lon=None,
        beds=3.0,
        baths=2.0,
        sqft=1350,
        asking_rent=None,
        concessions_text=None,
        date_posted=None,
        date_available=None,
        title=title,
        body=body,
        raw_payload_path=None,
    )


# ---------------------------------------------------------------------------
# Strong-signal tests
# ---------------------------------------------------------------------------

def test_turbotenant_link_drops_listing() -> None:
    obs = _obs(body="Apply now: https://apply.turbotenant.com/listing/abc123")
    assert is_for_sale_disguised(obs) == "turbotenant_link"


def test_turbotenant_domain_only_drops() -> None:
    obs = _obs(body="Prescreening via TurboTenant — click the link below.")
    assert is_for_sale_disguised(obs) == "turbotenant_link"


def test_mortgage_payment_estimate_drops() -> None:
    obs = _obs(body="Estimated monthly mortgage payment: $2,307/mo based on current rates.")
    assert is_for_sale_disguised(obs) == "mortgage_payment_estimate"


def test_estimated_mortgage_short_form_drops() -> None:
    obs = _obs(body="Estimated mortgage: $1,800/mo. Contact agent for details.")
    assert is_for_sale_disguised(obs) == "mortgage_payment_estimate"


def test_fsbo_in_title_drops() -> None:
    obs = _obs(title="FSBO — 3BR Gainesville Home")
    assert is_for_sale_disguised(obs) == "fsbo"


def test_for_sale_by_owner_drops() -> None:
    obs = _obs(body="This is a for sale by owner listing. No realtor fees.")
    assert is_for_sale_disguised(obs) == "fsbo"


def test_price_reduced_phrasing_drops() -> None:
    obs = _obs(body="Price reduced! Was $250k, now $229k. Don't miss this deal.")
    assert is_for_sale_disguised(obs) == "price_reduced"


def test_price_drop_drops() -> None:
    obs = _obs(body="Huge price drop — motivated seller, bring all offers.")
    assert is_for_sale_disguised(obs) == "price_reduced"


def test_list_price_drops() -> None:
    obs = _obs(body="List price: $219,000. Schedule a showing today.")
    assert is_for_sale_disguised(obs) == "list_price"


def test_listing_price_drops() -> None:
    obs = _obs(body="Listing price has been reduced to $199,900.")
    assert is_for_sale_disguised(obs) == "list_price"


def test_asking_price_drops() -> None:
    obs = _obs(body="Asking price: $235,000. Negotiable for quick close.")
    assert is_for_sale_disguised(obs) == "asking_price"


def test_seller_financing_drops() -> None:
    obs = _obs(body="Seller financing available. 10% down, 6% interest.")
    assert is_for_sale_disguised(obs) == "seller_financing"


def test_owner_financing_drops() -> None:
    obs = _obs(body="Owner financing considered for qualified buyers.")
    assert is_for_sale_disguised(obs) == "seller_financing"


def test_home_for_sale_drops() -> None:
    obs = _obs(title="Beautiful Home for Sale in 76240", body="Move-in ready.")
    assert is_for_sale_disguised(obs) == "home_for_sale"


# ---------------------------------------------------------------------------
# Weak-signal tests
# ---------------------------------------------------------------------------

def test_weak_signal_alone_does_not_fire() -> None:
    obs = _obs(body="Security deposit required. Down payment not applicable.")
    assert is_for_sale_disguised(obs) is None


def test_closing_costs_alone_does_not_fire() -> None:
    obs = _obs(body="All closing costs are negotiable with the right offer.")
    assert is_for_sale_disguised(obs) is None


def test_appraised_at_alone_does_not_fire() -> None:
    obs = _obs(body="Home appraised at $270,000 last year.")
    assert is_for_sale_disguised(obs) is None


def test_two_weak_signals_fire() -> None:
    obs = _obs(body="Down payment assistance programs available. Closing costs negotiable.")
    result = is_for_sale_disguised(obs)
    assert result is not None
    assert "two_weak_signals" in result
    assert "down_payment" in result
    assert "closing_costs" in result


def test_appraised_and_down_payment_fire() -> None:
    obs = _obs(body="Appraised at $215,000. Down payment as low as 3.5% with FHA.")
    result = is_for_sale_disguised(obs)
    assert result is not None
    assert "two_weak_signals" in result


def test_property_tax_hoa_combo_fires_as_one_weak_then_needs_partner() -> None:
    # property_tax + hoa is a single combined weak signal — alone it should not fire.
    obs = _obs(body="Property tax: $3,200/yr. HOA: $150/mo.")
    # One weak signal → does not fire.
    assert is_for_sale_disguised(obs) is None


def test_property_tax_hoa_plus_down_payment_fires() -> None:
    obs = _obs(body="Property tax: $3,200/yr. HOA: $150/mo. Down payment as low as 5%.")
    result = is_for_sale_disguised(obs)
    assert result is not None
    assert "two_weak_signals" in result


# ---------------------------------------------------------------------------
# True-rental passes (should return None)
# ---------------------------------------------------------------------------

def test_clean_rental_listing_passes() -> None:
    obs = _obs(
        title="3BR/2BA for rent in Gainesville",
        body="Rent: $1,500 / month. 12-month lease. Security deposit: $1,500. No pets.",
    )
    assert is_for_sale_disguised(obs) is None


def test_rent_to_own_listing_passes() -> None:
    obs = _obs(
        title="Rent-to-own available in 76240",
        body="Rent-to-own option available. $1,200/mo rent, 24-month term before purchase option.",
    )
    assert is_for_sale_disguised(obs) is None


def test_monthly_payment_without_mortgage_passes() -> None:
    obs = _obs(body="Monthly payment: $1,400. Includes water and trash. Available now.")
    assert is_for_sale_disguised(obs) is None


def test_word_sale_alone_does_not_fire() -> None:
    obs = _obs(body="Moving sale happening this weekend. Rent: $1,200/mo. 12-month lease.")
    assert is_for_sale_disguised(obs) is None


def test_none_title_none_body_passes() -> None:
    obs = _obs(title=None, body=None)
    assert is_for_sale_disguised(obs) is None


def test_empty_strings_pass() -> None:
    obs = _obs(title="", body="")
    assert is_for_sale_disguised(obs) is None
