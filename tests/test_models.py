from datetime import datetime, timezone

from jobfeed.models import Job


def test_round_trips_through_dict():
    job = Job(
        id="greenhouse:1",
        source="greenhouse",
        company="Acme",
        title="Senior Backend Engineer",
        url="https://example.com/1",
        cities=["Seattle"],
        posted_at=datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc),
    )
    d = job.to_dict()
    assert d["posted_at"] == "2026-11-01T08:30:00+00:00"
    assert Job.from_dict(d) == job


def test_missing_posted_at_round_trips():
    job = Job(id="greenhouse:2", source="greenhouse", company="Acme", title="SRE", url="u")
    assert Job.from_dict(job.to_dict()) == job
