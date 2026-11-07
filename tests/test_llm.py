from __future__ import annotations

import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from jobfeed import llm
from jobfeed.cli import main
from jobfeed.config import Config, ConfigError, load_config
from jobfeed.fit import Resume, Score, clean
from jobfeed.http import make_client
from jobfeed.llm import KeyRejected, LLMError, build_prompt, call, cost, parse, posting_text, request_body
from jobfeed.store import Store
from tests.conftest import router
from tests.fake_claude import KEY, FakeClaude
from tests.test_fit import RESUME, RESUME_PATH, job

NOW = datetime(2026, 11, 7, 19, 0, tzinfo=timezone.utc)


def resume() -> Resume:
    return Resume(clean(RESUME), "abc123", "test")


# --- the request -------------------------------------------------------------------------------

def test_request_body_and_headers():
    config = Config()
    config.fit.context_note = "Also built a Rust CLI in 2026."
    prompt = build_prompt(resume(), config)
    body = request_body(prompt, job(description="Requirements:\n- Go"), config)
    assert body["model"] == "claude-haiku-5-5" and body["max_tokens"] == 2000
    [system] = body["system"]
    assert system["cache_control"] == {"type": "ephemeral"}
    assert "<resume>" in system["text"] and "pat@example.com" not in system["text"]
    assert system["text"].rstrip().endswith("<context>\nAlso built a Rust CLI in 2026.\n</context>")
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == ["score", "reason"]
    assert "temperature" not in body                      # refused by API version 2026-10-21
    assert body["messages"][0]["content"].startswith("<posting>\nTitle: Senior Backend Engineer\nCompany: Co")
    fake = FakeClaude()
    with make_client(httpx.MockTransport(fake.handle)) as client:
        call(client, KEY, body)
    assert fake.headers[0]["anthropic-version"] == "2026-10-21" and fake.headers[0]["x-api-key"] == KEY


def test_prompt_version_follows_the_instructions_not_the_resume():
    config = Config()
    a = build_prompt(resume(), config)
    b = build_prompt(Resume("other text", "zzz", "t"), config)
    assert a.version == b.version and a.version.startswith("claude-haiku-5-5/prompt-")
    config.fit.llm_model = "claude-sonnet-5-5"
    assert build_prompt(resume(), config).version.startswith("claude-sonnet-5-5/prompt-")


def test_posting_text_states_pay_and_cuts_long_descriptions():
    text = posting_text(job(description="x" * 20_000, salary_min=180_000, salary_max=220_000))
    assert "Pay: $180K–$220K" in text and text.count("x") == 15_000 and "[description cut short]" in text
    assert "Pay: not stated" in posting_text(job())


# --- the reply ----------------------------------------------------------------------------------

def _reply(text='{"score": 77, "reason": "Good."}', stop="end_turn", **usage):
    return {"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": text}], "stop_reason": stop,
            "model": "claude-haiku-5-5", "usage": {"input_tokens": 1200, "output_tokens": 250,
                                                    "cache_read_input_tokens": 1400, **usage}}


def test_parse_reads_the_text_block_and_prices_it():
    prompt = build_prompt(resume(), Config())
    s = parse(_reply(), prompt, "claude-haiku-5-5")
    assert (s.scorer, s.score, s.reason, s.version) == ("llm", 77, "Good.", prompt.version)
    # 1,200 x $0.10 + 1,400 x $0.01 + 250 x $0.50, per million
    assert s.working["cost_usd"] == pytest.approx(0.000259)
    assert parse(_reply('{"score": 140, "reason": "' + "x" * 300 + '"}'), prompt, "m").score == 100
    assert len(parse(_reply('{"score": 50, "reason": "' + "x" * 300 + '"}'), prompt, "m").reason) == 160
    with pytest.raises(LLMError, match="refusal"):
        parse(_reply(stop="refusal"), prompt, "m")
    with pytest.raises(LLMError, match="expected JSON"):
        parse(_reply("not json"), prompt, "m")


def test_cost_of_a_cache_write():
    assert cost({"input_tokens": 1200, "cache_creation_input_tokens": 1400, "output_tokens": 250},
                "claude-haiku-5-5") == pytest.approx(0.00042)


def test_call_retries_overload_and_refuses_bad_keys():
    sleeps = []
    fake = FakeClaude(fail=[529, 503])
    with make_client(httpx.MockTransport(fake.handle)) as client:
        reply = call(client, KEY, request_body(build_prompt(resume(), Config()), job(), Config()), sleep=sleeps.append)
        assert reply["stop_reason"] == "end_turn" and sleeps == [1.0, 1.0]
        with pytest.raises(KeyRejected, match="ANTHROPIC_API_KEY"):
            call(client, "wrong", {}, sleep=sleeps.append)
    fake = FakeClaude(fail=[400])
    with make_client(httpx.MockTransport(fake.handle)) as client:
        with pytest.raises(LLMError, match="HTTP 400"):
            call(client, KEY, {}, sleep=sleeps.append)


def test_load_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GMAIL_USER=me@gmail.com\nANTHROPIC_API_KEY=sk-ant-file\n")
    assert llm.load_key(env, environ={}) == "sk-ant-file"
    assert llm.load_key(env, environ={"ANTHROPIC_API_KEY": "sk-ant-env"}) == "sk-ant-env"
    assert llm.load_key(tmp_path / "missing", environ={}) is None


@pytest.mark.parametrize("text, message", [
    ("fit:\n  llm_effort: tiny\n", "llm_effort"),
    ("fit:\n  llm_monthly_budget: -1\n", "can't be negative"),
    ("fit:\n  llm_concurrency: 0\n", "1 to 16"),
    ("fit:\n  llm_monthly_budget: cheap\n", "must be a number"),
])
def test_llm_settings_are_checked(tmp_path, text, message):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(text)
    with pytest.raises(ConfigError, match=message):
        load_config(cfg)
    cfg.write_text("fit:\n  llm_monthly_budget: 3\n")
    assert load_config(cfg).fit.llm_monthly_budget == 3.0


# --- in the run -----------------------------------------------------------------------------------------

@pytest.fixture
def pi(tmp_path, greenhouse_jobs):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    cfg = tmp_path / "config.yaml"
    cfg.write_text("tracking:\n  read_mail: false\n")
    env = tmp_path / ".env"
    env.write_text(f"ANTHROPIC_API_KEY={KEY}\n")
    env.chmod(0o600)
    fake = FakeClaude()
    state = {"jobs": greenhouse_jobs}

    def handle(request):
        if request.url.host == "api.anthropic.com":
            return fake.handle(request)
        return router({"/v1/boards/examplecorp/jobs": lambda r: httpx.Response(200, json=state["jobs"]),
                       RESUME_PATH: lambda r: httpx.Response(200, text=RESUME)}).handle_request(request)

    transport = httpx.MockTransport(handle)

    class Pi:
        db = tmp_path / "jobs.db"
        args = ["--db", str(tmp_path / "jobs.db"), "--config", str(cfg), "--env", str(env)]

        def main(self, argv, day=0):
            out, err = io.StringIO(), io.StringIO()
            code = main(argv, transport=transport, out=out, err=err, now=NOW + timedelta(days=day))
            return code, out.getvalue(), err.getvalue()

        def run(self, day=0):
            code, out, err = self.main(["run", "--no-email", "--companies", str(companies), *self.args,
                                        "--board", "greenhouse:examplecorp"], day)
            assert code == 0, err
            return out, err

        def llm_scores(self):
            conn = sqlite3.connect(self.db)
            return {j: (s, r, v, json.loads(w)) for j, s, r, v, w in conn.execute(
                "SELECT job_id, score, reason, version, working FROM fit_scores WHERE scorer = 'llm'")}

    p = Pi()
    p.fake, p.cfg, p.env, p.state = fake, cfg, env, state
    return p


def test_the_run_scores_with_the_llm_and_reports_cost(pi, no_sleep):
    out, err = pi.run()
    note = next(l for l in err.splitlines() if l.startswith("Fit scores"))
    # first call writes the cache ($0.00042), the second reads it ($0.000259)
    assert note == ("Fit scores: 2 postings scored by keywords; 2 by the LLM ($0.001; $0.00 this month) "
                    "(hidden for now).")
    scores = pi.llm_scores()
    assert scores["greenhouse:4412983"][:2] == (82, "Strong Go and Kubernetes match; scored 82.")
    assert scores["greenhouse:4412983"][2].startswith("claude-haiku-5-5/prompt-")
    assert sum(w["cost_usd"] for *_, w in scores.values()) == pytest.approx(0.000679)
    assert "82" not in out and "Strong Go" not in out              # hidden
    _, err = pi.run(1)
    assert "Fit scores" not in err and len(pi.fake.calls) == 2      # each posting once
    _, out, _ = pi.main(["fit", "status", *pi.args])
    assert "  llm       2 scored" in out and "LLM spend this month: $0.00 of $5.00" in out


def test_backlog_cap_failures_and_budget(pi, no_sleep):
    pi.cfg.write_text("tracking:\n  read_mail: false\nfit:\n  llm: false\n")
    pi.run()                                                     # sent before the LLM was on
    pi.cfg.write_text("tracking:\n  read_mail: false\nfit:\n  llm_max_per_run: 1\n")
    _, err = pi.run(1)
    assert "0 postings" not in err and "1 by the LLM" in err and "1 left for later" in err
    _, err = pi.run(2)
    assert "1 by the LLM" in err and "left for later" not in err
    assert len(pi.llm_scores()) == 2


def test_failures_are_retried_next_run(pi, no_sleep):
    pi.fake.text = "not json"
    _, err = pi.run()
    assert "0 by the LLM" in err and "2 failed" in err and pi.llm_scores() == {}
    pi.fake.text = None
    _, err = pi.run(1)
    assert "2 by the LLM" in err


def test_a_rejected_key_pauses_scoring(pi, no_sleep):
    pi.env.write_text("ANTHROPIC_API_KEY=wrong\n")
    _, err = pi.run()
    assert "paused: the API refused the key (HTTP 401); check ANTHROPIC_API_KEY in .env" in err
    assert pi.llm_scores() == {}


def test_the_monthly_budget_pauses_scoring(pi, no_sleep):
    pi.cfg.write_text("tracking:\n  read_mail: false\nfit:\n  llm_monthly_budget: 0.0003\n")
    _, err = pi.run()
    assert "1 by the LLM" in err and "paused: this month's $0.00 budget is spent" in err
    assert len(pi.llm_scores()) == 1
    _, err = pi.run(30)                                          # December: a new month, the backlog goes
    assert "1 by the LLM" in err


def test_without_a_key_the_keywords_still_run(pi, no_sleep):
    pi.env.write_text("GMAIL_USER=me@gmail.com\n")
    _, err = pi.run()
    assert f"LLM scoring off: no ANTHROPIC_API_KEY in {pi.env}" in err and pi.llm_scores() == {}


def test_backfill_and_check(pi, no_sleep):
    pi.cfg.write_text("tracking:\n  read_mail: false\nfit:\n  enabled: false\n")
    pi.run()
    pi.cfg.write_text("tracking:\n  read_mail: false\n")
    code, out, _ = pi.main(["fit", "backfill", *pi.args])
    assert code == 0
    assert out.splitlines() == ["Scored 2 of 2 postings sent by keywords; the rest were already scored.",
                                "Scored 2 by the LLM ($0.001).", "Scores stay hidden."]
    code, out, _ = pi.main(["fit", "check", *pi.args])
    assert code == 0
    lines = out.splitlines()
    assert lines[0].startswith("The LLM scorer works (claude-haiku-5-5/prompt-")
    assert lines[1] == "  Senior Backend Engineer at Example Co: 82"
    assert lines[3].startswith("  tokens: 1200 in")
    assert len(pi.llm_scores()) == 2                              # the check stores nothing
    pi.env.write_text("ANTHROPIC_API_KEY=wrong\n")
    code, _, err = pi.main(["fit", "check", *pi.args])
    assert code == 1 and "refused the key" in err


def test_spent_this_month(tmp_path):
    with Store.open(tmp_path / "jobs.db") as store:
        r = resume()
        for day, c in [(-8, 1.0), (0, 0.25), (1, 0.5)]:     # Oct 30, Nov 7, Nov 8
            from jobfeed.fit import save_score
            save_score(store, f"j{day}", Score("llm", "v", 50, "r", {"cost_usd": c}), r, NOW + timedelta(days=day))
        assert llm.spent_this_month(store, NOW + timedelta(days=1)) == pytest.approx(0.75)
