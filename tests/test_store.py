from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from jobfeed.models import Job
from jobfeed.store import SCHEMA_VERSION, Store, StoreError

T0 = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)


def _job(id_="greenhouse:1", title="Senior Backend Engineer", **kw) -> Job:
    return Job(id=id_, source="greenhouse", company="Acme", title=title, url="https://example.com", **kw)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "jobs.db"


def test_creates_schema_with_version(db):
    with Store.open(db):
        pass
    conn = sqlite3.connect(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
    assert cols == ["id", "dedupe_key", "first_seen", "last_seen", "matched", "emailed_at", "data", "code", "section"]


def test_save_and_read_back(db):
    job = _job(salary_max=200_000, cities=["Seattle, WA"])
    with Store.open(db) as store:
        store.save_run([(job, "acme|senior backend engineer", True)], [], T0)
        row = store.get([job.id])[job.id]
    assert row.job == job
    assert (row.first_seen, row.last_seen, row.matched, row.emailed_at) == (T0, T0, True, None)
    assert row.dedupe_key == "acme|senior backend engineer"


def test_upsert_keeps_first_seen_and_emailed_at(db):
    t1 = T0 + timedelta(days=1)
    with Store.open(db) as store:
        store.save_run([(_job(), "k", True)], ["greenhouse:1"], T0)
        store.save_run([(_job(title="Senior Backend Engineer II"), "k2", False)], [], t1)
        row = store.get(["greenhouse:1"])["greenhouse:1"]
    assert row.first_seen == T0 and row.emailed_at == T0
    assert row.last_seen == t1
    assert (row.job.title, row.dedupe_key, row.matched) == ("Senior Backend Engineer II", "k2", False)


def test_marking_sent_twice_keeps_the_first_date(db):
    with Store.open(db) as store:
        store.save_run([(_job(), "k", True)], ["greenhouse:1"], T0)
        store.save_run([(_job(), "k", True)], ["greenhouse:1"], T0 + timedelta(days=2))
        assert store.get(["greenhouse:1"])["greenhouse:1"].emailed_at == T0


def test_emailed_keys_since(db):
    with Store.open(db) as store:
        store.save_run([(_job("a"), "old", True), (_job("b"), "new", True), (_job("c"), "unsent", True)],
                       [], T0)
        store.save_run([(_job("a"), "old", True)], ["a"], T0 - timedelta(days=40))
        store.save_run([(_job("b"), "new", True)], ["b"], T0)
        assert store.emailed_keys_since(["old", "new", "unsent", "missing"], T0 - timedelta(days=30)) == {"new"}


def test_get_handles_many_ids(db):
    jobs = [(_job(f"greenhouse:{n}"), f"k{n}", True) for n in range(1200)]
    with Store.open(db) as store:
        store.save_run(jobs, [], T0)
        assert len(store.get(j.id for j, _, _ in jobs)) == 1200
        assert store.count() == 1200


def test_all_rows_seen_since(db):
    with Store.open(db) as store:
        store.save_run([(_job("old"), "k1", True)], [], T0 - timedelta(days=10))
        store.save_run([(_job("new"), "k2", True)], [], T0)
        assert [r.job.id for r in store.all_rows()] == ["old", "new"]
        assert [r.job.id for r in store.all_rows(seen_since=T0 - timedelta(days=3))] == ["new"]


def test_readonly_missing_file_is_none_and_creates_nothing(db):
    assert Store.open_readonly(db) is None
    assert not db.exists()


def test_readonly_refuses_writes(db):
    with Store.open(db) as store:
        store.save_run([(_job(), "k", True)], [], T0)
    with Store.open_readonly(db) as ro:
        assert ro.get(["greenhouse:1"])
        with pytest.raises(StoreError, match="read-only"):
            ro.save_run([(_job("x"), "k", True)], [], T0)


def test_newer_schema_is_refused(db):
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(StoreError, match="newer jobfeed"):
        Store.open(db)


def test_garbage_file_is_a_store_error(db):
    db.write_text("not a database")
    with pytest.raises(StoreError):
        Store.open(db)
    with pytest.raises(StoreError):
        Store.open_readonly(db)
