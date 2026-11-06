from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobfeed.config import Config
from jobfeed.dedupe import dedupe_key, plan_run
from jobfeed.filters import MATCHES, NO_SALARY, WATCHLIST
from jobfeed.models import Job
from jobfeed.normalize import normalize_job
from jobfeed.store import Store

NOW = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
PAY = "Base salary: $170,000 - $210,000"


def job(id_, title="Senior Backend Engineer", company="Acme", source="greenhouse", location="Remote (US)",
        description=PAY, **kw) -> Job:
    return normalize_job(Job(id=id_, source=source, company=company, title=title, url="https://example.com",
                             location_raw=location, description=description, **kw))


@pytest.fixture
def store(tmp_path):
    with Store.open(tmp_path / "jobs.db") as s:
        yield s


def _sent(store: Store, jobs: list[Job], when: datetime) -> None:
    store.save_run([(j, dedupe_key(j), True) for j in jobs], [j.id for j in jobs], when)


def _ids(plan, section=MATCHES):
    return [e.job.id for e in plan.digest.sections[section]]


# --- keys --------------------------------------------------------------------

@pytest.mark.parametrize("company,title,key", [
    ("Acme", "Senior Backend Engineer", "acme|senior backend engineer"),
    ("Acme, Inc.", "Senior Backend Engineer (Remote)", "acme|senior backend engineer"),
    ("ACME Inc", "Senior Backend Engineer - Remote US", "acme|senior backend engineer"),
    ("Acme Corp", "Senior Backend Engineer [USA]", "acme|senior backend engineer"),
    ("Grafana Labs", "Staff Engineer, Mimir", "grafana|staff engineer mimir"),
    ("Acme", "Senior C++ Engineer", "acme|senior c++ engineer"),
    ("Acme", "Senior Backend Engineer (Payments)", "acme|senior backend engineer payments"),
])
def test_dedupe_key(company, title, key):
    assert dedupe_key(job("x", title=title, company=company)) == key


def test_different_levels_are_different_roles():
    assert dedupe_key(job("a", title="Backend Engineer")) != dedupe_key(job("b", title="Senior Backend Engineer"))


# --- a run on its own ----------------------------------------------------------

def test_without_a_store_everything_matching_is_new():
    plan = plan_run([job("a"), job("b", title="Staff Engineer", description="")], Config(), watched=set(),
                    store=None, now=NOW)
    assert _ids(plan) == ["a"] and _ids(plan, NO_SALARY) == ["b"]
    assert plan.to_send == ["a", "b"]
    assert plan.matched == {"a": True, "b": True}


def test_ats_copy_beats_aggregator_copy_in_the_same_run():
    remote = job("remotive:9", source="remotive", title="Senior Backend Engineer (Remote)")
    ats = job("greenhouse:1")
    for order in ([remote, ats], [ats, remote]):
        plan = plan_run(order, Config(), watched=set(), store=None, now=NOW)
        assert plan.to_send == ["greenhouse:1"]
        assert [j.id for j in plan.duplicates] == ["remotive:9"]


def test_same_role_at_two_offices_is_sent_once():
    plan = plan_run([job("greenhouse:2", location="Seattle, WA"), job("greenhouse:1", location="Bellevue, WA")],
                    Config(), watched=set(), store=None, now=NOW)
    assert plan.to_send == ["greenhouse:1"]
    assert [j.id for j in plan.duplicates] == ["greenhouse:2"]


def test_copy_in_a_better_section_wins():
    no_pay = job("greenhouse:1", description="")
    paid = job("greenhouse:2")
    plan = plan_run([no_pay, paid], Config(), watched=set(), store=None, now=NOW)
    assert plan.to_send == ["greenhouse:2"]


def test_dropped_postings_stay_out_of_dedupe():
    plan = plan_run([job("a", title="Frontend Engineer"), job("b")], Config(), watched=set(), store=None, now=NOW)
    assert plan.to_send == ["b"]
    assert [e.job.id for e in plan.digest.dropped] == ["a"]
    assert plan.matched == {"a": False, "b": True}


# --- across runs ---------------------------------------------------------------

def test_sent_postings_are_not_sent_again(store):
    _sent(store, [job("a")], NOW - timedelta(days=1))
    plan = plan_run([job("a"), job("b", title="Staff Backend Engineer")], Config(), watched=set(), store=store,
                    now=NOW)
    assert plan.to_send == ["b"]
    assert [j.id for j in plan.already_sent] == ["a"]


def test_seen_but_unsent_postings_are_still_new(store):
    store.save_run([(job("a"), dedupe_key(job("a")), True)], [], NOW - timedelta(days=1))
    assert plan_run([job("a")], Config(), watched=set(), store=store, now=NOW).to_send == ["a"]


def test_role_sent_from_another_source_within_30_days_is_skipped(store):
    _sent(store, [job("greenhouse:1")], NOW - timedelta(days=29))
    later = job("remotive:9", source="remotive", title="Senior Backend Engineer (Remote)")
    plan = plan_run([later], Config(), watched=set(), store=store, now=NOW)
    assert plan.to_send == []
    assert [j.id for j in plan.duplicates] == ["remotive:9"]


def test_role_sent_more_than_30_days_ago_comes_back(store):
    _sent(store, [job("greenhouse:1")], NOW - timedelta(days=31))
    repost = job("greenhouse:2")
    assert plan_run([repost], Config(), watched=set(), store=store, now=NOW).to_send == ["greenhouse:2"]


def test_overflow_waits_for_the_next_run(store):
    cfg = Config()
    cfg.digest.max_per_section = 2
    jobs = [job(f"greenhouse:{n}", title=f"Senior Backend Engineer {n}") for n in range(3)]
    first = plan_run(jobs, cfg, watched=set(), store=store, now=NOW)
    assert len(first.to_send) == 2 and first.digest.hidden == {MATCHES: 1}
    store.save_run([(j, first.keys[j.id], True) for j in jobs], first.to_send, NOW)

    second = plan_run(jobs, cfg, watched=set(), store=store, now=NOW + timedelta(days=1))
    assert len(second.to_send) == 1
    assert set(second.to_send) | set(first.to_send) == {j.id for j in jobs}


def test_watchlist_section_survives_dedupe(store):
    plan = plan_run([job("a")], Config(), watched={"acme"}, store=store, now=NOW)
    assert _ids(plan, WATCHLIST) == ["a"]
