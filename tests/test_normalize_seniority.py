"""Titles. Ones marked "seen" were copied from live postings in Nov 2026."""

import pytest

from jobfeed.normalize.seniority import parse_seniority

CASES = [
    # --- seen ---
    ("Senior Software Engineer, Distributed Systems (Rust)", "senior"),
    ("Staff Software Engineer - Infrastructure Storage", "staff"),
    ("Staff+ Software Engineer, Platform", "staff"),
    ("Member of Technical Staff (Software Engineer, API Platform)", None),
    ("Member of Technical Staff - Cloud Infrastructure", None),
    ("Senior Software Engineer II, KYC", "senior"),
    ("Software Engineer II, Autonomous Freight Technology", "mid"),
    ("2026 Early Career Software Engineer", "junior"),
    ("Software Engineer - New Grad (2027 Start)", "junior"),
    ("Software Engineering Intern (2027)", "junior"),
    ("Lead Software Engineer, API/SDK", "senior"),
    ("Principal Flight Software Engineer", "principal"),
    ("Software engineer, agents", None),
    ("Software Engineer, Networking", None),
    ("Staff Software Engineer, Business Data", "staff"),
    # --- common formats ---
    ("Sr. Software Engineer", "senior"),
    ("Sr Backend Engineer", "senior"),
    ("Senior Staff Engineer", "staff"),
    ("Distinguished Engineer", "principal"),
    ("Tech Lead, Payments", "senior"),
    ("Software Engineer I", "junior"),
    ("Software Engineer III", "senior"),
    ("Engineer IV, Infrastructure", "staff"),
    ("SDE 2", "mid"),
    ("Software Engineer (L5)", "senior"),
    ("Software Engineer, IC4", "senior"),
    ("Junior Developer", "junior"),
    ("Mid-level Backend Engineer", "mid"),
    ("Associate Software Engineer", "junior"),
    ("Software Engineer", None),
    ("", None),
]


@pytest.mark.parametrize("title,expected", CASES, ids=[c[0] or "(empty)" for c in CASES])
def test_titles(title, expected):
    assert parse_seniority(title) == expected
