"""Location strings. Ones marked "seen" were copied from live postings in Nov 2026;
the rest are the common formats Greenhouse, Lever, Ashby and the aggregators use."""

from __future__ import annotations

import pytest

from jobfeed.normalize.location import parse_location, state_eligibility

# (raw, work_mode, remote_region, cities)
CASES = [
    # --- seen ---
    ("Seattle, WA", "onsite", None, ["Seattle, WA"]),
    ("Seattle, Washington", "onsite", None, ["Seattle, WA"]),
    ("Seattle, Washington, United States", "onsite", None, ["Seattle, WA"]),
    ("Seattle", "onsite", None, ["Seattle"]),
    ("US - Seattle", "onsite", None, ["Seattle"]),
    ("Seattle, WA (Remote)", "remote", "us", ["Seattle, WA"]),
    ("Multiple locations", "unknown", None, []),
    ("San Francisco, CA • New York, NY • United States", "onsite", None, ["San Francisco, CA", "New York, NY"]),
    ("Rosslyn, VA", "onsite", None, ["Rosslyn, VA"]),
    ("Bellevue, Washington", "onsite", None, ["Bellevue, WA"]),
    ("Worldwide", "remote", "global", []),
    ("Multiple locations: Seattle, WA; New York, NY", "onsite", None, ["Seattle, WA", "New York, NY"]),
    # --- remote, US ---
    ("Remote (US)", "remote", "us", []),
    ("Remote - US", "remote", "us", []),
    ("Remote, USA", "remote", "us", []),
    ("Remote-US", "remote", "us", []),
    ("US-Remote", "remote", "us", []),
    ("US Remote", "remote", "us", []),
    ("Remote (US-only)", "remote", "us", []),
    ("Remote within the US", "remote", "us", []),
    ("Remote - United States", "remote", "us", []),
    ("Remote/USA", "remote", "us", []),
    ("Remote, US (Pacific Time preferred)", "remote", "us", []),
    ("Remote (Pacific Time Zone)", "remote", "us", []),
    ("Remote (US or Canada)", "remote", "us", []),
    ("Remote - US; Remote - Canada", "remote", "us", []),
    ("Seattle, WA; Remote (US)", "remote", "us", ["Seattle, WA"]),
    ("Remote - Seattle, WA", "remote", "us", ["Seattle, WA"]),
    ("Remote - CA, CO, NY", "remote", "us", []),
    ("Remote - Washington", "remote", "us", []),
    # --- remote, other regions ---
    ("Remote (U.S.)", "remote", "us", []),
    ("Remote US & Canada", "remote", "us", []),
    ("Remote (Brazil or United States)", "remote", "us", []),
    ("Remote - East Coast", "remote", "us", []),
    ("Remote - US (excluding CA, NY)", "remote", "us", []),
    # a bare remote option next to offices takes the offices' country
    ("Austin, TX or Remote", "remote", "us", ["Austin, TX"]),
    ("Seattle, WA / Remote", "remote", "us", ["Seattle, WA"]),
    ("London, UK; Remote", "remote", "other", ["London, UK"]),
    ("Remote", "remote", None, []),
    ("Remote; Hybrid", "remote", None, []),
    ("Remote (Seattle preferred)", "remote", None, ["Seattle"]),
    ("Remote - North America", "remote", "americas", []),
    ("Remote - Americas", "remote", "americas", []),
    ("Remote (Anywhere)", "remote", "global", []),
    ("Anywhere in the World", "remote", "global", []),
    ("Remote - Canada", "remote", "other", []),
    ("Remote - EMEA", "remote", "other", []),
    ("Remote, Europe", "remote", "other", []),
    ("Remote - LATAM", "remote", "other", []),
    ("Remote - India", "remote", "other", []),
    ("Remote (UK)", "remote", "other", []),
    ("Remote - Toronto, ON", "remote", "other", ["Toronto, Canada"]),
    # --- hybrid and onsite ---
    ("Hybrid - Seattle, WA", "hybrid", None, ["Seattle, WA"]),
    ("Seattle, WA - Hybrid", "hybrid", None, ["Seattle, WA"]),
    ("Bellevue, WA (Hybrid)", "hybrid", None, ["Bellevue, WA"]),
    ("Seattle (Hybrid, 3 days a week)", "hybrid", None, ["Seattle"]),
    ("Hybrid (Seattle / Bellevue)", "hybrid", None, ["Seattle", "Bellevue"]),
    ("In-Office - Seattle", "onsite", None, ["Seattle"]),
    ("On-site: Renton, WA", "onsite", None, ["Renton, WA"]),
    ("Kirkland, WA; Redmond, WA", "onsite", None, ["Kirkland, WA", "Redmond, WA"]),
    ("Seattle, WA, New York, NY", "onsite", None, ["Seattle, WA", "New York, NY"]),
    ("San Francisco, CA | New York, NY | Seattle, WA", "onsite", None,
     ["San Francisco, CA", "New York, NY", "Seattle, WA"]),
    ("San Francisco, California, United States", "onsite", None, ["San Francisco, CA"]),
    ("Lynnwood, WA", "onsite", None, ["Lynnwood, WA"]),
    ("Bothell, WA", "onsite", None, ["Bothell, WA"]),
    ("Issaquah, Washington", "onsite", None, ["Issaquah, WA"]),
    ("Greater Seattle Area", "onsite", None, ["Seattle, WA"]),
    ("Seattle Metro Area", "onsite", None, ["Seattle"]),
    ("SF Bay Area", "onsite", None, ["San Francisco, CA"]),
    ("NYC", "onsite", None, ["New York, NY"]),
    ("New York", "onsite", None, ["New York, NY"]),
    ("New York, NY", "onsite", None, ["New York, NY"]),
    ("Washington, DC", "onsite", None, ["Washington, DC"]),
    ("Portland, OR", "onsite", None, ["Portland, OR"]),
    ("Hartford, CT", "onsite", None, ["Hartford, CT"]),
    ("Bellevue, NE", "onsite", None, ["Bellevue, NE"]),
    ("London, UK", "onsite", None, ["London, UK"]),
    ("London, United Kingdom", "onsite", None, ["London, United Kingdom"]),
    ("Toronto, ON, Canada", "onsite", None, ["Toronto, Canada"]),
    ("Berlin, Germany", "onsite", None, ["Berlin, Germany"]),
    ("Winston-Salem, NC", "onsite", None, ["Winston-Salem, NC"]),
    ("Seattle HQ", "onsite", None, ["Seattle"]),
    # --- not enough to say ---
    ("", "unknown", None, []),
    ("USA", "unknown", None, []),
    ("United States", "unknown", None, []),
]


@pytest.mark.parametrize("raw,mode,region,cities", CASES, ids=[c[0] or "(empty)" for c in CASES])
def test_location_strings(raw, mode, region, cities):
    info = parse_location(raw)
    assert (info.work_mode, info.remote_region, info.cities) == (mode, region, cities)


def test_case_count():
    assert len(CASES) >= 50


@pytest.mark.parametrize("raw,states", [
    ("Remote - CA, CO, NY", ["CA", "CO", "NY"]),
    ("Remote - Washington", ["WA"]),
    ("Remote - Seattle, WA", ["WA"]),
    ("Remote (US)", []),
    ("Seattle, WA; Remote (US)", []),  # the office's state doesn't limit the remote option
])
def test_remote_states_from_location(raw, states):
    assert parse_location(raw).remote_states == states


@pytest.mark.parametrize("raw,excluded", [
    ("Remote - US (excluding CA, NY)", ["CA", "NY"]),
    ("Remote - US, except Hawaii and Alaska", ["HI", "AK"]),
])
def test_excluded_states_from_location(raw, excluded):
    info = parse_location(raw)
    assert (info.excluded_states, info.remote_states, info.cities) == (excluded, [], [])


@pytest.mark.parametrize("raw,expected", [
    ("USA", ("remote", "us")),
    ("Canada", ("remote", "other")),
    ("Worldwide", ("remote", "global")),
    ("Americas", ("remote", "americas")),
    ("", ("remote", None)),
])
def test_aggregator_locations_assume_remote(raw, expected):
    info = parse_location(raw, assume_remote=True)
    assert (info.work_mode, info.remote_region) == expected


# --- description hints -------------------------------------------------------

def test_description_can_make_a_city_posting_remote():
    info = parse_location("Seattle, WA", "This role is fully remote within the US.")
    assert (info.work_mode, info.remote_region) == ("remote", "us")


def test_description_negation_keeps_it_onsite():
    assert parse_location("Seattle, WA", "This is not a remote position.").work_mode == "onsite"


def test_description_hybrid():
    assert parse_location("Seattle, WA", "We work in a hybrid model, in office Tue-Thu.").work_mode == "hybrid"


def test_description_never_overrides_explicit_location_mode():
    assert parse_location("Remote (US)", "Our hybrid teams meet quarterly.").work_mode == "remote"


@pytest.mark.parametrize("description,allowed,excluded", [
    ("At this time we can only hire in the following states: California, Colorado, New York and Texas.",
     ["CA", "CO", "NY", "TX"], []),
    ("Candidates must reside in one of these states: WA, OR, CA.", ["WA", "OR", "CA"], []),
    ("We are currently unable to hire in the following states: Alaska, Hawaii, Washington.",
     [], ["AK", "HI", "WA"]),
    ("We hire in all US states except Alaska and Hawaii.", [], ["AK", "HI"]),
    ("We can hire in the following states:\nArizona\nOregon\nWashington\nWhat you'll do", ["AZ", "OR", "WA"], []),
    # Pay-transparency sentences name states without limiting who can apply.
    ("The base salary range for candidates located in California and New York states is $180,000.", [], []),
    # Offices aren't eligibility.
    ("Our hubs are located in the following locations: New York and San Francisco.", [], []),
    ("We hire in all 50 states.", [], []),
    ("", [], []),
])
def test_state_eligibility(description, allowed, excluded):
    assert state_eligibility(description) == (allowed, excluded)


def test_eligibility_sets_remote_states_and_region():
    info = parse_location("Remote", "We can only hire employees residing in the following states: CA, NY.")
    assert (info.remote_region, info.remote_states) == ("us", ["CA", "NY"])


def test_eligibility_is_ignored_for_onsite_roles():
    info = parse_location("Seattle, WA", "We can hire in the following states: CA, NY.")
    assert info.remote_states == []
