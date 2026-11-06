from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
import yaml

from jobfeed.cli import main
from jobfeed.companies import load_companies
from jobfeed.discover import EditError, Discovery, discover_company, slug_candidates, update_companies_file
from jobfeed.http import make_client
from tests.conftest import REPO_ROOT


@pytest.mark.parametrize("name,slugs", [
    ("Stripe", [("stripe", True)]),
    ("Grafana Labs", [("grafanalabs", True), ("grafana-labs", True), ("grafana", False)]),
    ("Union.ai", [("unionai", True), ("union-ai", True), ("union", False)]),
    ("Fly.io", [("flyio", True), ("fly-io", True), ("fly", False)]),
    ("Expedia Group", [("expediagroup", True), ("expedia-group", True), ("expedia", False)]),
    ("Rune Technologies", [("runetechnologies", True), ("rune-technologies", True), ("rune", False)]),
    ("Overland AI", [("overlandai", True), ("overland-ai", True), ("overland", False)]),
    ("xAI", [("xai", True)]),
    ("Dropzone AI", [("dropzoneai", True), ("dropzone-ai", True), ("dropzone", False)]),
    ("Fluidstack", [("fluidstack", True)]),
])
def test_slug_candidates(name, slugs):
    assert slug_candidates(name) == slugs


class FakeBoards:
    """A transport that serves a few boards on each ATS and 404s everything else."""

    def __init__(self, greenhouse=None, lever=None, ashby=None, broken=False):
        self.greenhouse = greenhouse or {}   # slug -> (postings, company_name)
        self.lever = lever or {}             # slug -> postings
        self.ashby = ashby or {}             # slug -> postings
        self.broken = broken
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(f"{request.url.host}{request.url.path}")
        if self.broken:
            return httpx.Response(503)
        parts = request.url.path.strip("/").split("/")
        host = request.url.host
        if host == "boards-api.greenhouse.io" and parts[-1] == "jobs" and parts[-2] in self.greenhouse:
            n, name = self.greenhouse[parts[-2]]
            return httpx.Response(200, json={"jobs": [{"id": i + 1, "company_name": name} for i in range(n)]})
        if host in ("api.lever.co",) and parts[-1] in self.lever:
            return httpx.Response(200, json=[{"id": f"l{i}", "text": "x"} for i in range(self.lever[parts[-1]])])
        if host == "api.ashbyhq.com" and parts[-1] in self.ashby:
            return httpx.Response(200, json={"jobs": [{"id": f"a{i}"} for i in range(self.ashby[parts[-1]])]})
        return httpx.Response(404)


def _discover(name, boards: FakeBoards, no_sleep=None) -> Discovery:
    with make_client(httpx.MockTransport(boards)) as client:
        return discover_company(client, name)


def test_full_name_on_greenhouse():
    d = _discover("Stripe", FakeBoards(greenhouse={"stripe": (12, "Stripe")}))
    assert (d.ats, d.slug, d.postings, d.board_name, d.warnings) == ("greenhouse", "stripe", 12, "Stripe", [])


def test_lever_and_ashby_hits():
    assert _discover("Lever Co", FakeBoards(lever={"leverco": 3})).ats == "lever"
    d = _discover("Ashby Co", FakeBoards(ashby={"ashby-co": 5}))
    assert (d.ats, d.slug) == ("ashby", "ashby-co")


def test_short_name_match_is_flagged():
    d = _discover("Rune Technologies", FakeBoards(lever={"rune": 4}))
    assert (d.ats, d.slug) == ("lever", "rune")
    assert d.warnings == ["matched the short name 'rune'; check it's Rune Technologies"]


def test_greenhouse_board_with_another_name_is_flagged():
    d = _discover("Snap", FakeBoards(greenhouse={"snap": (2, "Snapdragon Bakery")}))
    assert "the board is named 'Snapdragon Bakery'" in d.warnings


def test_company_suffixes_dont_trigger_the_name_check():
    d = _discover("Grafana Labs", FakeBoards(greenhouse={"grafanalabs": (9, "Grafana Labs, Inc.")}))
    assert d.warnings == []


def test_a_board_with_postings_beats_an_empty_one():
    d = _discover("Acme", FakeBoards(greenhouse={"acme": (0, None)}, ashby={"acme": 7}))
    assert (d.ats, d.postings, d.warnings) == ("ashby", 7, [])


def test_empty_board_is_used_only_as_a_last_resort():
    d = _discover("Acme", FakeBoards(ashby={"acme": 0}))
    assert (d.ats, d.postings) == ("ashby", 0)
    assert d.warnings == ["board has no open postings; it may be old or someone else's"]


def test_nowhere_is_unsupported():
    boards = FakeBoards()
    d = _discover("Zillow", boards)
    assert not d.found and d.error is None
    assert d.tried == ["greenhouse:zillow", "lever:zillow", "ashby:zillow"]


def test_network_trouble_is_an_error_not_unsupported(no_sleep):
    d = _discover("Zillow", FakeBoards(broken=True))
    assert not d.found and d.error and "HTTP 503" in d.error


# --- editing companies.yaml ------------------------------------------------------

SAMPLE = """\
# My seed list. Comments matter.
companies:

  # --- Seattle ---
  - { name: Stripe,        bucket: seattle-hiring, origin: seed, ats: null, slug: null, watch: false }
  - { name: Rune Technologies, bucket: seattle-ai, origin: seed, ats: null, slug: null, watch: true }
  - { name: "Odd: Name", bucket: null, origin: seed, ats: null, slug: null, watch: false }
  - { name: Zillow, bucket: seattle-local, origin: seed, ats: null, slug: null, watch: false }
  - { name: Flaky, ats: null, slug: null }

# Not polled.
manual_check:
  - Microsoft
"""


@pytest.fixture
def companies_file(tmp_path) -> Path:
    path = tmp_path / "companies.yaml"
    path.write_text(SAMPLE)
    return path


def test_edits_in_place_and_keeps_comments(companies_file):
    added = update_companies_file(companies_file, [
        Discovery("Stripe", ats="greenhouse", slug="stripe"),
        Discovery("rune technologies", ats="lever", slug="rune"),     # names match case-insensitively
        Discovery("Odd: Name", ats="ashby", slug="odd-name"),
        Discovery("Zillow"),                                           # found nowhere
        Discovery("Flaky", error="HTTP 503"),                          # left alone
    ])
    text = companies_file.read_text()
    assert added == []
    assert "# My seed list. Comments matter." in text and "# --- Seattle ---" in text and "# Not polled." in text
    assert "  - { name: Stripe,        bucket: seattle-hiring, origin: seed, ats: greenhouse, slug: stripe, watch: false }" in text
    assert "ats: lever, slug: rune, watch: true }" in text
    assert "  - { name: Zillow, bucket: seattle-local, origin: seed, ats: unsupported, slug: null, watch: false }" in text
    assert "  - { name: Flaky, ats: null, slug: null }" in text
    by_name = {c.name: c for c in load_companies(companies_file).companies}
    assert (by_name["Odd: Name"].ats, by_name["Odd: Name"].slug) == ("ashby", "odd-name")
    assert companies_file.with_name("companies.yaml.bak").read_text() == SAMPLE


def test_new_names_are_appended_inside_the_companies_list(companies_file):
    added = update_companies_file(companies_file, [Discovery("Datadog", ats="greenhouse", slug="datadog"),
                                                   Discovery("Nobody Inc")])
    assert added == ["Datadog", "Nobody Inc"]
    data = yaml.safe_load(companies_file.read_text())
    names = [c["name"] for c in data["companies"]]
    assert names[-2:] == ["Datadog", "Nobody Inc"]
    assert data["manual_check"] == ["Microsoft"]
    text = companies_file.read_text()
    assert text.index("# --- added by jobfeed discover ---") < text.index("# Not polled.")

    # a second round appends under the same header
    update_companies_file(companies_file, [Discovery("Temporal", ats="ashby", slug="temporal")])
    text = companies_file.read_text()
    assert text.count("# --- added by jobfeed discover ---") == 1
    assert [c["name"] for c in yaml.safe_load(text)["companies"]][-1] == "Temporal"


def test_missing_file_is_created(tmp_path):
    path = tmp_path / "companies.yaml"
    update_companies_file(path, [Discovery("Stripe", ats="greenhouse", slug="stripe")])
    assert [(c.name, c.ats) for c in load_companies(path).companies] == [("Stripe", "greenhouse")]


def test_multi_line_entries_are_refused(tmp_path):
    path = tmp_path / "companies.yaml"
    original = "companies:\n  - name: Stripe\n    ats: null\n    slug: null\n"
    path.write_text(original)
    with pytest.raises(EditError, match="several lines"):
        update_companies_file(path, [Discovery("Stripe", ats="greenhouse", slug="stripe")])
    assert path.read_text() == original


def test_repo_seed_list_can_be_edited_cleanly(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text((REPO_ROOT / "companies.yaml").read_text())
    names = [c.name for c in load_companies(path).companies]
    found = [Discovery(n, ats="greenhouse", slug=f"s{i}") if i % 3 else Discovery(n) for i, n in enumerate(names)]
    update_companies_file(path, found)
    after = load_companies(path)
    assert [c.name for c in after.companies] == names
    assert all(c.ats == ("greenhouse" if i % 3 else "unsupported") for i, c in enumerate(after.companies))
    assert after.manual_check == load_companies(REPO_ROOT / "companies.yaml").manual_check


# --- the command -------------------------------------------------------------------

def _cli(argv, boards: FakeBoards):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=httpx.MockTransport(boards), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_discover_command_updates_pending_companies(companies_file, no_sleep):
    boards = FakeBoards(greenhouse={"stripe": (12, "Stripe")}, lever={"rune": 4})
    code, out, err = _cli(["discover", "--companies", str(companies_file)], boards)
    assert code == 0
    assert "Stripe" in out and "greenhouse:stripe · 12 postings" in out
    assert "check: matched the short name 'rune'" in out
    assert "not on Greenhouse, Lever or Ashby" in out
    assert "5 checked · 2 found (1 greenhouse, 1 lever) · 3 unsupported · 1 worth a look" in out
    assert "Updated" in err
    by_name = {c.name: c for c in load_companies(companies_file).companies}
    assert (by_name["Stripe"].ats, by_name["Zillow"].ats) == ("greenhouse", "unsupported")


def test_discover_skips_companies_already_done_unless_retry(companies_file):
    _cli(["discover", "--companies", str(companies_file)], FakeBoards())
    code, _, err = _cli(["discover", "--companies", str(companies_file)], FakeBoards())
    assert code == 0 and "already has an ats" in err
    boards = FakeBoards(greenhouse={"zillow": (3, "Zillow")})
    _, out, _ = _cli(["discover", "--retry", "--companies", str(companies_file)], boards)
    assert "zillow" in out
    assert {c.name: c.ats for c in load_companies(companies_file).companies}["Zillow"] == "greenhouse"


def test_discover_named_companies_and_dry_run(companies_file):
    boards = FakeBoards(greenhouse={"datadog": (40, "Datadog")})
    code, out, err = _cli(["discover", "Datadog", "--dry-run", "--companies", str(companies_file)], boards)
    assert code == 0 and "greenhouse:datadog · 40 postings" in out
    assert "not changed" in err
    assert companies_file.read_text() == SAMPLE

    _, _, err = _cli(["discover", "Datadog", "--companies", str(companies_file)], boards)
    assert "added 1 new: Datadog" in err


def test_discover_all_errors_exits_1(companies_file, no_sleep):
    code, out, _ = _cli(["discover", "Stripe", "--companies", str(companies_file)], FakeBoards(broken=True))
    assert code == 1
    assert "couldn't check" in out
    assert companies_file.read_text().count("ats: null") == 5     # nothing changed
