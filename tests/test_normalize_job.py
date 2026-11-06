from jobfeed.models import Job
from jobfeed.normalize import normalize_job


def _job(**kw) -> Job:
    base = dict(id="greenhouse:1", source="greenhouse", company="Acme", title="Senior Backend Engineer",
                url="https://example.com/1")
    return Job(**{**base, **kw})


def test_fills_every_normalized_field():
    job = normalize_job(_job(
        location_raw="Seattle, WA; Remote (US)",
        description="We can hire in the following states: WA, OR, CA.\nBase salary: $170,000 - $215,000",
    ))
    assert job.work_mode == "remote"
    assert job.remote_region == "us"
    assert job.cities == ["Seattle, WA"]
    assert job.remote_states == ["WA", "OR", "CA"]
    assert (job.salary_min, job.salary_max, job.salary_currency) == (170_000, 215_000, "USD")
    assert job.salary_raw == "$170,000 - $215,000"
    assert job.seniority == "senior"


def test_adapter_set_fields_are_kept():
    job = normalize_job(_job(
        location_raw="Seattle, WA",
        work_mode="hybrid",
        salary_min=150_000, salary_max=180_000,
        seniority="staff",
        description="Base salary: $100,000 - $120,000. This role is fully remote.",
    ))
    assert job.work_mode == "hybrid"
    assert (job.salary_min, job.salary_max, job.salary_currency) == (150_000, 180_000, "USD")
    assert job.seniority == "staff"


def test_aggregator_remote_hint_reads_bare_country_as_region():
    job = normalize_job(_job(source="remotive", location_raw="USA", work_mode="remote"))
    assert (job.work_mode, job.remote_region) == ("remote", "us")


def test_salary_raw_from_the_adapter_is_parsed_first():
    job = normalize_job(_job(salary_raw="$120k - $150k", description="Base salary $200,000 - $250,000"))
    assert (job.salary_min, job.salary_max) == (120_000, 150_000)


def test_non_usd_pay_is_kept_but_not_compared():
    job = normalize_job(_job(location_raw="London, UK", description="Salary: £80,000 - £100,000"))
    assert (job.salary_min, job.salary_max, job.salary_currency) == (None, None, "GBP")
    assert job.salary_raw == "£80,000 - £100,000"


def test_onsite_job_gets_no_remote_fields():
    job = normalize_job(_job(location_raw="Bellevue, WA", description="We hire in the following states: CA, NY."))
    assert (job.work_mode, job.remote_region, job.remote_states) == ("onsite", None, [])


def test_round_trips_through_dict_after_normalizing():
    job = normalize_job(_job(location_raw="Remote - CA, NY", description="Salary: $200,000 - $240,000"))
    assert Job.from_dict(job.to_dict()) == job
