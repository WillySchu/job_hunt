from __future__ import annotations

import io
import json

import httpx
import pytest

from jobfeed.cli import main
from tests.conftest import router


def _transport(greenhouse_jobs):
    return router({
        "/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=greenhouse_jobs),
        "/v1/boards/examplecorp": lambda r: httpx.Response(200, json={"name": "Example Corp"}),
        "/v1/boards/emptyco/jobs": lambda r: httpx.Response(200, json={"jobs": [], "meta": {"total": 0}}),
        "/v1/boards/emptyco": lambda r: httpx.Response(200, json={"name": "Empty Co"}),
    })


def _run(argv, transport=None):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=transport, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_dry_run_prints_postings_newest_first(greenhouse_jobs):
    code, out, err = _run(
        ["run", "--dry-run", "--board", "greenhouse:examplecorp", "--board", "greenhouse:nope",
         "--board", "greenhouse:emptyco"],
        _transport(greenhouse_jobs),
    )
    assert code == 0
    assert "== Example Corp (greenhouse:examplecorp) · 3 postings" in out
    assert out.index("Staff Infrastructure Engineer") < out.index("Senior Backend Engineer, Platform")
    assert "Seattle, WA; Remote (US)" in out
    # the normalizers' reading of each posting
    assert "→ remote (us) · Seattle, WA · $170K–$215K · senior" in out
    assert "→ onsite · Bellevue, WA; New York, NY · no salary listed · staff" in out
    assert "→ onsite · London, UK · no salary listed · level unstated" in out
    assert "3 boards · 2 ok · 1 failed · 3 postings" in err
    assert "FAILED greenhouse:nope: board not found" in err
    assert "EMPTY  greenhouse:emptyco" in err


def test_limit_hides_the_rest(greenhouse_jobs):
    code, out, _ = _run(["run", "--dry-run", "--board", "greenhouse:examplecorp", "--limit", "1"],
                        _transport(greenhouse_jobs))
    assert code == 0
    assert "… 2 more (use --limit 0 to show all)" in out


def test_json_output_is_one_job_per_line(greenhouse_jobs):
    code, out, _ = _run(["run", "--dry-run", "--json", "--board", "greenhouse:examplecorp"],
                        _transport(greenhouse_jobs))
    rows = [json.loads(line) for line in out.splitlines()]
    assert code == 0
    assert [r["id"] for r in rows] == ["greenhouse:4412983", "greenhouse:4412990", "greenhouse:4413000"]
    assert rows[0]["company"] == "Example Corp"
    assert (rows[0]["work_mode"], rows[0]["salary_max"], rows[0]["seniority"]) == ("remote", 215_000, "senior")


def test_all_boards_failing_exits_1():
    code, _, err = _run(["run", "--dry-run", "--board", "greenhouse:nope"], router({}))
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


def test_companies_yaml_boards_are_used(tmp_path, greenhouse_jobs):
    path = tmp_path / "companies.yaml"
    path.write_text("companies:\n  - { name: Example (from yaml), ats: greenhouse, slug: examplecorp }\n")
    code, out, _ = _run(["run", "--dry-run", "--companies", str(path)], _transport(greenhouse_jobs))
    assert code == 0
    assert "== Example (from yaml) (greenhouse:examplecorp)" in out


@pytest.mark.parametrize("spec", ["greenhouse", "workday:acme", ":acme"])
def test_bad_board_specs_are_usage_errors(spec, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["run", "--dry-run", "--board", spec])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["discover", "replay"])
def test_future_commands_say_so(command):
    code, _, err = _run([command])
    assert code == 2 and "isn't built yet" in err
