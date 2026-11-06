from __future__ import annotations

import io
import json
from datetime import date

import httpx
import pytest

from jobfeed.cli import main
from tests.conftest import router

TODAY = date(2026, 11, 6)


def _transport(greenhouse_jobs):
    return router({
        "/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs),
        "/v1/boards/examplecorp": lambda r: httpx.Response(200, json={"name": "Example Corp"}),
        "/v1/boards/emptyco/jobs": lambda r: httpx.Response(200, json={"jobs": [], "meta": {"total": 0}}),
        "/v1/boards/emptyco": lambda r: httpx.Response(200, json={"name": "Empty Co"}),
    })


def _run(argv, transport=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=transport, out=out, err=err, today=TODAY)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def no_companies(tmp_path):
    """An empty companies.yaml, so tests don't depend on the repo's seed list."""
    path = tmp_path / "companies.yaml"
    path.write_text("companies: []\n")
    return ["--companies", str(path)]


# --- run --dry-run -------------------------------------------------------------

def test_dry_run_previews_the_digest(greenhouse_jobs, no_companies):
    code, out, err = _run(
        ["run", "--dry-run", *no_companies, "--board", "greenhouse:examplecorp", "--board", "greenhouse:nope",
         "--board", "greenhouse:emptyco"],
        _transport(greenhouse_jobs),
    )
    assert code == 0
    assert out.startswith("Job feed · Fri Nov 6 · 2 new (0 watchlist)")
    assert "== New matches (1)" in out
    assert ("Senior Backend Engineer, Platform — Example Corp · Seattle, WA; Remote (US) · $170K–$215K · Senior"
            in out)
    assert "remote (us) · via Greenhouse · posted 2026-10-28" in out
    assert "== No salary listed (1)" in out
    assert "Staff Infrastructure Engineer — Example Corp" in out
    assert "Frontend Engineer" not in out          # dropped, and --all wasn't given
    assert "2 matched · 1 dropped (role 1) (add --all to see why)" in err
    assert "3 boards · 2 ok · 1 failed · 3 postings" in err
    assert "FAILED greenhouse:nope: board not found" in err
    assert "EMPTY  greenhouse:emptyco" in err


def test_all_lists_drops_with_reasons(greenhouse_jobs, no_companies):
    _, out, _ = _run(["run", "--dry-run", "--all", *no_companies, "--board", "greenhouse:examplecorp"],
                     _transport(greenhouse_jobs))
    assert "== Dropped (1)" in out
    assert "role      Frontend Engineer — Example Corp · London, UK" in out
    assert "title says 'Frontend'" in out


def test_watchlist_from_companies_yaml(greenhouse_jobs, tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - { name: Example Corp, ats: greenhouse, slug: examplecorp, watch: true }\n")
    code, out, _ = _run(["run", "--dry-run", "--companies", str(path)], _transport(greenhouse_jobs))
    assert code == 0
    assert "2 new (2 watchlist)" in out
    assert "== Watchlist (2)" in out


def test_limit_caps_each_section(greenhouse_jobs, tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - { name: Example Corp, ats: greenhouse, slug: examplecorp, watch: true }\n")
    _, out, _ = _run(["run", "--dry-run", "--companies", str(path), "--limit", "1"], _transport(greenhouse_jobs))
    assert "== Watchlist (2)" in out
    assert "… 1 more not shown" in out


def test_config_rules_apply(greenhouse_jobs, tmp_path, no_companies):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("salary:\n  floor_usd: 250000\n")
    _, out, err = _run(["run", "--dry-run", "--all", *no_companies, "--config", str(cfg),
                        "--board", "greenhouse:examplecorp"], _transport(greenhouse_jobs))
    assert "pay tops out at $215K (floor $250K)" in out
    assert "1 matched · 2 dropped (role 1, salary 1)" in err


def test_bad_config_exits_2(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("salary:\n  floor: 1\n")
    code, _, err = _run(["run", "--dry-run", "--config", str(cfg), "--board", "greenhouse:examplecorp"])
    assert code == 2
    assert "config error: unknown setting `salary.floor`" in err


def test_json_output_is_every_posting_unfiltered(greenhouse_jobs, no_companies):
    code, out, _ = _run(["run", "--dry-run", "--json", *no_companies, "--board", "greenhouse:examplecorp"],
                        _transport(greenhouse_jobs))
    rows = [json.loads(line) for line in out.splitlines()]
    assert code == 0
    assert [r["id"] for r in rows] == ["greenhouse:4412983", "greenhouse:4412990", "greenhouse:4413000"]
    assert (rows[0]["work_mode"], rows[0]["salary_max"], rows[0]["seniority"]) == ("remote", 215_000, "senior")


def test_all_boards_failing_exits_1(no_companies):
    code, _, err = _run(["run", "--dry-run", *no_companies, "--board", "greenhouse:nope"], router({}))
    assert code == 1
    assert "0 ok · 1 failed" in err


def test_run_without_dry_run_refuses():
    code, _, err = _run(["run", "--board", "greenhouse:examplecorp"])
    assert code == 2
    assert "Nothing was fetched" in err


def test_no_configured_boards_explains(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - { name: Acme, ats: null, slug: null }\n")
    code, _, err = _run(["run", "--dry-run", "--companies", str(path)])
    assert code == 2
    assert "--board greenhouse:SLUG" in err


@pytest.mark.parametrize("spec", ["greenhouse", "workday:acme", ":acme"])
def test_bad_board_specs_are_usage_errors(spec, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--dry-run", "--board", spec])
    assert exc.value.code == 2


# --- replay ------------------------------------------------------------------

def _dump(greenhouse_jobs, no_companies, tmp_path):
    _, out, _ = _run(["run", "--dry-run", "--json", *no_companies, "--board", "greenhouse:examplecorp"],
                     _transport(greenhouse_jobs))
    path = tmp_path / "postings.jsonl"
    path.write_text(out)
    return path


def test_replay_matches_the_dry_run(greenhouse_jobs, no_companies, tmp_path):
    dump = _dump(greenhouse_jobs, no_companies, tmp_path)
    _, live, _ = _run(["run", "--dry-run", "--all", *no_companies, "--board", "greenhouse:examplecorp"],
                      _transport(greenhouse_jobs))
    code, replayed, err = _run(["replay", "--all", *no_companies, "--from", str(dump)])
    assert code == 0
    assert replayed == live
    assert f"3 postings replayed from {dump}" in err


def test_replay_uses_the_current_config(greenhouse_jobs, no_companies, tmp_path):
    dump = _dump(greenhouse_jobs, no_companies, tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("role:\n  exclude: [manager]\nsalary:\n  keep_missing: false\n")
    _, out, err = _run(["replay", "--all", *no_companies, "--config", str(cfg), "--from", str(dump)])
    assert "Frontend Engineer" in out and "== Dropped" in out
    assert "1 matched · 2 dropped (location 1, salary 1)" in err   # London is now a location drop


def test_replay_without_from_explains():
    code, _, err = _run(["replay"])
    assert code == 2
    assert "jobfeed replay --from postings.jsonl" in err


def test_replay_reports_bad_lines(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"id": "x"}\n')
    code, _, err = _run(["replay", "--from", str(path)])
    assert code == 2
    assert "bad.jsonl:1 isn't a Job record" in err


def test_replay_missing_file(tmp_path):
    code, _, err = _run(["replay", "--from", str(tmp_path / "nope.jsonl")])
    assert code == 2
    assert "can't read postings" in err


def test_discover_says_not_built_yet():
    code, _, err = _run(["discover"])
    assert code == 2 and "isn't built yet" in err
