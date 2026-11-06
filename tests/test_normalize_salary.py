"""Salary strings. Ones marked "seen" were copied from live postings or API samples in Nov 2026."""

from __future__ import annotations

import pytest

from jobfeed.normalize.salary import format_salary, parse_salary

# (text, min, max, currency) — min/max annual USD; currency None means nothing found
CASES = [
    # --- seen ---
    ("Salary: $235,090- $285,600/yr", 235_090, 285_600, "USD"),
    ("$40,000 - $50,000", 40_000, 50_000, "USD"),
    ("90,000-120,000 USD annual", 90_000, 120_000, "USD"),
    ("Annual Base Salary Range: $258,000 - $376,000 USD", 258_000, 376_000, "USD"),
    # --- common formats ---
    ("The base pay range for this role is $170,000—$215,000 USD.", 170_000, 215_000, "USD"),
    ("US Pay Range\n$190,000—$260,000 USD", 190_000, 260_000, "USD"),
    ("$170K–$215K", 170_000, 215_000, "USD"),
    ("$120K-$150K", 120_000, 150_000, "USD"),
    ("$150-190k", 150_000, 190_000, "USD"),
    ("150k-190k USD", 150_000, 190_000, "USD"),
    ("USD 150,000 - 190,000", 150_000, 190_000, "USD"),
    ("$165,000 USD - $210,000 USD", 165_000, 210_000, "USD"),
    ("$150,000 to $190,000 per year", 150_000, 190_000, "USD"),
    ("between $147,000 and $220,000", 147_000, 220_000, "USD"),
    ("Compensation: 150000 - 190000", 150_000, 190_000, "USD"),
    ("$100k – $130k + equity", 100_000, 130_000, "USD"),
    ("The salary for this role is $185,000.", 185_000, 185_000, "USD"),
    ("OTE $250k", 250_000, 250_000, "USD"),
    ("Starting at $160,000", 160_000, None, "USD"),
    ("$200K+", 200_000, None, "USD"),
    ("Up to $180,000", None, 180_000, "USD"),
    # --- hourly and monthly, annualized at 2,080 hours / 12 months ---
    ("$75 - $95 per hour", 156_000, 197_600, "USD"),
    ("$45.50 - $60.00 per hour", 94_640, 124_800, "USD"),
    ("The hourly range is $55 - $70", 114_400, 145_600, "USD"),
    ("$60/hr", 124_800, 124_800, "USD"),
    ("$13,000 - $15,000 per month", 156_000, 180_000, "USD"),
    # --- bonuses and equity around the base pay ---
    ("Pay: $150,000-$190,000 plus a $10,000 signing bonus", 150_000, 190_000, "USD"),
    ("Bonus: $10,000 - $20,000\nBase: $160,000 - $200,000", 160_000, 200_000, "USD"),
    ("$180,000 - $220,000 + bonus + equity", 180_000, 220_000, "USD"),
    ("Salary $150k-$180k, plus $10,000 - $20,000 bonus", 150_000, 180_000, "USD"),
    ("$30,000 signing bonus", None, None, None),
    ("Range: $165.000 - $185.000", 165_000, 185_000, "USD"),
    # --- pay zones: the Seattle line wins; otherwise all ranges merge ---
    ("Zone A: $180,000 - $220,000\nZone B (Seattle, WA): $171,000 - $209,000\nZone C: $153,000 - $187,000",
     171_000, 209_000, "USD"),
    ("San Francisco: $200,000-$250,000; Remote US: $170,000-$212,500", 170_000, 250_000, "USD"),
    # --- other currencies: kept, but no USD figures ---
    ("£80,000 - £100,000", None, None, "GBP"),
    ("CA$150,000 - CA$190,000", None, None, "CAD"),
    ("$150,000 - $190,000 CAD", None, None, "CAD"),
    ("€70.000–€90.000", None, None, "EUR"),
    # --- not salaries ---
    ("$5M Series A", None, None, None),
    ("We raised $120 million", None, None, None),
    ("Our ARR is $10M-$20M", None, None, None),
    ("$1.2B valuation", None, None, None),
    ("$2,000 learning stipend", None, None, None),
    ("$1,000/month stipend", None, None, None),
    ("$500 home office budget", None, None, None),
    ("401(k) match up to 4%", None, None, None),
    ("$5 per month subscription", None, None, None),
    ("500 - 1,000 employees", None, None, None),
    ("We have 2019 - 2023 data", None, None, None),
    ("Join 100+ engineers", None, None, None),
    ("", None, None, None),
]


@pytest.mark.parametrize("text,low,high,currency", CASES, ids=[c[0][:50] or "(empty)" for c in CASES])
def test_salary_strings(text, low, high, currency):
    info = parse_salary(text)
    assert (info.min, info.max, info.currency) == (low, high, currency)


def test_case_count():
    assert len(CASES) >= 40


def test_raw_keeps_the_matched_text():
    assert parse_salary("Salary: $235,090- $285,600/yr").raw == "$235,090- $285,600/yr"
    assert parse_salary("£80,000 - £100,000").raw == "£80,000 - £100,000"


def test_first_text_with_a_salary_wins():
    info = parse_salary(None, "$120k - $150k", "Base salary: $200,000 - $250,000")
    assert (info.min, info.max) == (120_000, 150_000)


def test_falls_through_to_the_description():
    info = parse_salary("Competitive", "The base salary range is $160,000 - $200,000.")
    assert (info.min, info.max) == (160_000, 200_000)


def test_nothing_found():
    info = parse_salary(None, "Competitive pay and great benefits.")
    assert not info.found and info.currency is None


@pytest.mark.parametrize("low,high,text", [
    (170_000, 215_000, "$170K–$215K"),
    (185_000, 185_000, "$185K"),
    (200_000, None, "$200K+"),
    (None, 180_000, "up to $180K"),
    (94_640, 124_800, "$94.6K–$125K"),
    (None, None, ""),
])
def test_format_salary(low, high, text):
    assert format_salary(low, high) == text
