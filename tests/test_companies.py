from pathlib import Path

import pytest

from jobfeed.companies import load_companies
from tests.conftest import REPO_ROOT


def test_repo_seed_list_loads():
    companies = load_companies(REPO_ROOT / "companies.yaml")
    assert len(companies.companies) >= 60
    assert "Microsoft" in companies.manual_check
    names = {c.name for c in companies.companies}
    assert {"Stripe", "Anthropic", "Zillow"} <= names


def test_only_filled_in_companies_become_boards(tmp_path: Path):
    path = tmp_path / "companies.yaml"
    path.write_text(
        "companies:\n"
        "  - { name: Acme, ats: greenhouse, slug: acme, watch: true }\n"
        "  - { name: Pending, ats: null, slug: null }\n"
        "  - { name: Custom, ats: unsupported, slug: null }\n"
        "  - { name: Leverco, ats: lever, slug: leverco }\n"
    )
    companies = load_companies(path)
    boards = companies.boards("greenhouse")
    assert len(boards) == 1
    assert boards[0].slug == "acme" and boards[0].company == "Acme" and boards[0].watch
    assert [b.source for b in companies.boards()] == ["greenhouse", "lever"]


def test_missing_file_is_empty(tmp_path: Path):
    assert load_companies(tmp_path / "nope.yaml").companies == []


def test_entry_without_name_is_rejected(tmp_path: Path):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - { ats: greenhouse, slug: x }\n")
    with pytest.raises(ValueError, match="needs a name"):
        load_companies(path)
