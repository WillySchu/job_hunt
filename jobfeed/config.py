"""Loading the filter rules from config.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path("config.yaml")


class ConfigError(ValueError):
    """config.yaml has an unknown key, a wrong type or a bad pattern."""


@dataclass
class RoleRules:
    include: list[str] = field(default_factory=lambda: [
        "engineer(ing)?", "developer", "swe", "sre", "backend", "platform", "infrastructure",
        "member of technical staff",
    ])
    exclude: list[str] = field(default_factory=lambda: [
        "front ?end", "full ?stack", "ui engineer", "web developer", "manager", "director", "head of",
        "intern(ship)?", "recruiter", "sales engineer", "solutions? engineer", "support engineer", "mechanical",
        "electrical", "hardware", "manufacturing",
    ])
    frontend_terms: list[str] = field(default_factory=lambda: [
        "react", "vue", "angular", "css", r"next\.js", "tailwind", "redux", "html", "frontend", "front-end",
    ])
    backend_terms: list[str] = field(default_factory=lambda: [
        "golang", "rust", "java", "scala", "kotlin", r"c\+\+", "python", "distributed systems", "kafka",
        "postgres(ql)?", "mysql", "kubernetes", "grpc", "microservices", "backend", "back-end", "infrastructure",
    ])


@dataclass
class LocationRules:
    remote_regions: list[str] = field(default_factory=lambda: ["us", "americas", "global", "unknown"])
    home_state: str = "WA"
    metro: list[str] = field(default_factory=lambda: [
        "Seattle", "Bellevue", "Redmond", "Kirkland", "Bothell", "Issaquah", "Renton", "Lynnwood",
    ])


@dataclass
class SalaryRules:
    floor_usd: int = 150_000
    compare: str = "max"
    keep_missing: bool = True


@dataclass
class SeniorityRules:
    prefer: list[str] = field(default_factory=lambda: ["senior", "staff"])


@dataclass
class DigestRules:
    max_per_section: int = 50


@dataclass
class AggregatorRules:
    remotive: bool = True
    remotive_category: str = "software-dev"
    remoteok: bool = True
    remoteok_tags: list[str] = field(default_factory=list)
    himalayas: bool = True
    himalayas_queries: list[str] = field(default_factory=lambda: [
        "software engineer", "backend engineer", "platform engineer",
    ])
    himalayas_country: str = "US"
    himalayas_pages: int = 2
    harvest: bool = True


@dataclass
class TrackingRules:
    resume_version: str = "v1"     # recorded on each `jobfeed apply` unless --resume says otherwise
    cover_letter: bool = False     # whether you usually send one; --cover-letter / --no-cover-letter override
    follow_up_days: int = 10       # list an open application in the digest after this many quiet days
    no_response_days: int = 21     # close an application with no reply after this many days
    read_mail: bool = True         # read your replies to the digest, and job mail, from Gmail on each run
    ats_senders: list[str] = field(default_factory=lambda: [
        "greenhouse-mail.io", "greenhouse.io", "lever.co", "ashbyhq.com",
    ])                             # domains applicant-tracking systems send candidate mail from
    weekly_report: bool = True     # email `jobfeed stats` as a weekly report, on weekly_day
    weekly_day: str = "monday"
    backups_kept: int = 8          # weekly copies of jobs.db kept in backups/; 0 turns backups off
    attach_backup: bool = True     # attach a gzipped copy of jobs.db to the weekly report


# Technical terms the keyword scorer looks for, as "Name: pattern". The pattern is a
# regular expression matched as whole words, ignoring case; (?-i:...) makes part of it
# case-sensitive, so "Go" the language isn't "go" the verb. No colon: the name is the pattern.
DEFAULT_FIT_TERMS = [
    "Python: python", "Go: (?-i:Go)|golang", "Java: java(?!script)", "JavaScript: javascript|node\\.?js",
    "TypeScript: typescript", "Rust: rust", "C++: c\\+\\+", "C#: c#", "C: (?<!Series )(?-i:C)(?![+#-])", "Ruby: ruby",
    "Lua: lua", "Scala: scala", "Kotlin: kotlin", "Elixir: elixir", "Erlang: erlang", "Haskell: haskell",
    "Clojure: clojure", "PHP: php",
    "PostgreSQL: postgres(?:ql)?", "MySQL: mysql", "Redis: redis", "MongoDB: mongo(?:db)?",
    "DynamoDB: dynamo(?:db)?", "Cassandra: cassandra", "Elasticsearch: elastic(?:search)?|opensearch",
    "Snowflake: snowflake", "BigQuery: bigquery", "ClickHouse: clickhouse", "SQL: sql",
    "Kafka: kafka", "RabbitMQ: rabbitmq", "Spark: spark", "Flink: flink", "Airflow: airflow",
    "dbt: (?-i:dbt)",
    "AWS: aws|amazon web services", "GCP: gcp|google cloud", "Azure: azure",
    "Kubernetes: kubernetes|k8s", "Docker: docker|containers?", "Terraform: terraform", "Helm: helm",
    "Linux: linux", "CI/CD: ci/cd|continuous (?:integration|delivery|deployment)",
    "Grafana: grafana", "Prometheus: prometheus", "Datadog: datadog", "OpenTelemetry: opentelemetry|otel",
    "Observability: observability|monitoring|alerting",
    "gRPC: grpc", "GraphQL: graphql", "REST: restful|rest apis?", "API design: api design|apis?",
    "Microservices: micro-?services", "Distributed systems: distributed systems",
    "Data pipelines: data pipelines?|etl|data ingestion", "Machine learning: machine learning|ml(?:ops)?",
    "Caching: caching|cache", "Performance: performance tuning|performance optimi[sz]ation|latency",
    "Time series: time[- ]series", "Flask: flask", "Django: django", "FastAPI: fastapi",
    "React: react(?:\\.?js)?", "Angular: angular", "Vue: vue(?:\\.?js)?", "Redux: redux", "Next.js: next\\.?js",
    "CSS: css|tailwind|sass", "HTML: html",
]


@dataclass
class FitRules:
    enabled: bool = True
    # The resume both scorers read; a local copy is kept beside jobs.db for when it can't be fetched.
    resume_url: str = "https://raw.githubusercontent.com/WillySchu/pandoc_resume/master/markdown/resume.md"
    terms: list[str] = field(default_factory=lambda: list(DEFAULT_FIT_TERMS))
    extra_terms: list[str] = field(default_factory=list)   # added to `terms`, same "Name: pattern" form
    strong: list[str] = field(default_factory=list)     # term names to count fully, whatever the resume says
    basic: list[str] = field(default_factory=list)      # term names to count half
    extra_skills: list[str] = field(default_factory=list)   # term names you have that the resume doesn't show
    experience_years: int = 0      # 0: worked out from the resume's dates
    context_note: str = ""         # a line or two the resume doesn't say, for the LLM scorer
    llm: bool = True               # score with Claude too, when ANTHROPIC_API_KEY is in .env
    llm_model: str = "claude-haiku-5-5"
    llm_effort: str = "low"
    llm_max_per_run: int = 300
    llm_monthly_budget: float = 5.0   # USD; scoring pauses for the month once it's spent
    llm_concurrency: int = 4


@dataclass
class Config:
    role: RoleRules = field(default_factory=RoleRules)
    location: LocationRules = field(default_factory=LocationRules)
    salary: SalaryRules = field(default_factory=SalaryRules)
    seniority: SeniorityRules = field(default_factory=SeniorityRules)
    digest: DigestRules = field(default_factory=DigestRules)
    aggregators: AggregatorRules = field(default_factory=AggregatorRules)
    tracking: TrackingRules = field(default_factory=TrackingRules)
    fit: FitRules = field(default_factory=FitRules)


_SECTIONS = {"role": RoleRules, "location": LocationRules, "salary": SalaryRules,
             "seniority": SeniorityRules, "digest": DigestRules, "aggregators": AggregatorRules,
             "tracking": TrackingRules, "fit": FitRules}
_REGIONS = {"us", "americas", "global", "other", "unknown"}
_RANKS = {"junior", "mid", "senior", "staff", "principal"}


def _check_type(where: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false (got {value!r})")
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be a whole number (got {value!r})")
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where} must be a number (got {value!r})")
        value = float(value)
    elif isinstance(default, str):
        if not isinstance(value, str):
            raise ConfigError(f"{where} must be text (got {value!r})")
    elif isinstance(default, list):
        if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
            raise ConfigError(f"{where} must be a list (got {value!r})")
        value = [str(v) for v in value]
    return value


def _section(name: str, cls: type, data: Any) -> Any:
    obj = cls()
    if data is None:
        return obj
    if not isinstance(data, dict):
        raise ConfigError(f"`{name}` must be a mapping of settings")
    known = {f.name for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            raise ConfigError(f"unknown setting `{name}.{key}` (expected one of: {', '.join(sorted(known))})")
        setattr(obj, key, _check_type(f"`{name}.{key}`", value, getattr(obj, key)))
    return obj


def _validate(cfg: Config) -> None:
    for where, patterns in [("role.include", cfg.role.include), ("role.exclude", cfg.role.exclude),
                            ("role.frontend_terms", cfg.role.frontend_terms),
                            ("role.backend_terms", cfg.role.backend_terms)]:
        for p in patterns:
            try:
                re.compile(p)
            except re.error as exc:
                raise ConfigError(f"`{where}` has a bad pattern {p!r}: {exc}") from exc
    if not cfg.role.include:
        raise ConfigError("`role.include` needs at least one pattern, or every posting is dropped")
    bad = set(cfg.location.remote_regions) - _REGIONS
    if bad:
        raise ConfigError(f"`location.remote_regions` has unknown region(s) {sorted(bad)}; "
                          f"use {', '.join(sorted(_REGIONS))}")
    cfg.location.home_state = cfg.location.home_state.upper()
    if cfg.salary.compare not in ("max", "min"):
        raise ConfigError(f"`salary.compare` must be max or min (got {cfg.salary.compare!r})")
    if cfg.salary.floor_usd < 0:
        raise ConfigError("`salary.floor_usd` can't be negative")
    bad = set(cfg.seniority.prefer) - _RANKS
    if bad:
        raise ConfigError(f"`seniority.prefer` has unknown level(s) {sorted(bad)}; use {', '.join(sorted(_RANKS))}")
    if cfg.digest.max_per_section < 1:
        raise ConfigError("`digest.max_per_section` must be at least 1")
    agg = cfg.aggregators
    if not 1 <= agg.himalayas_pages <= 10:
        raise ConfigError("`aggregators.himalayas_pages` must be 1 to 10 (20 postings a page)")
    if agg.remotive and not agg.remotive_category.strip():
        raise ConfigError("`aggregators.remotive_category` is empty; use `all`, or set `remotive: false`")
    if agg.himalayas and not agg.himalayas_queries:
        raise ConfigError("`aggregators.himalayas_queries` is empty; add a query, or set `himalayas: false`")
    cfg.tracking.resume_version = cfg.tracking.resume_version.strip()
    if not cfg.tracking.resume_version:
        raise ConfigError("`tracking.resume_version` is empty; name the resume you send, e.g. v1")
    names = []
    for entry in cfg.fit.terms + cfg.fit.extra_terms:
        name, _, pattern = entry.partition(": ") if ": " in entry else (entry, "", re.escape(entry))
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"`fit.terms` has a bad pattern for {name!r}: {exc}") from exc
        names.append(name.strip().casefold())
    if cfg.fit.llm_effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ConfigError(f"`fit.llm_effort` must be low, medium, high, xhigh or max (got {cfg.fit.llm_effort!r})")
    if cfg.fit.llm_max_per_run < 0 or cfg.fit.llm_monthly_budget < 0 or not 1 <= cfg.fit.llm_concurrency <= 16:
        raise ConfigError("`fit.llm_max_per_run` and `fit.llm_monthly_budget` can't be negative, and "
                          "`fit.llm_concurrency` must be 1 to 16")
    for key in ("strong", "basic", "extra_skills"):
        unknown = [t for t in getattr(cfg.fit, key) if t.casefold() not in names]
        if unknown:
            raise ConfigError(f"`fit.{key}` names {unknown} that aren't in `fit.terms`")
    cfg.tracking.weekly_day = cfg.tracking.weekly_day.strip().lower()
    if cfg.tracking.weekly_day not in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"):
        raise ConfigError(f"`tracking.weekly_day` must be a day of the week (got {cfg.tracking.weekly_day!r})")
    if cfg.tracking.backups_kept < 0:
        raise ConfigError("`tracking.backups_kept` can't be negative; 0 turns backups off")
    if cfg.tracking.follow_up_days < 1 or cfg.tracking.no_response_days < 1:
        raise ConfigError("`tracking.follow_up_days` and `tracking.no_response_days` must be at least 1")


def load_config(path: Path = DEFAULT_PATH) -> Config:
    """Read config.yaml; a missing file or section means the built-in defaults (the spec's rules)."""
    data: Any = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} isn't valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must be a mapping of sections")
    unknown = set(data) - set(_SECTIONS)
    if unknown:
        raise ConfigError(f"unknown section(s) {sorted(unknown)} in {path} "
                          f"(expected: {', '.join(_SECTIONS)})")
    cfg = Config(**{name: _section(name, cls, data.get(name)) for name, cls in _SECTIONS.items()})
    _validate(cfg)
    return cfg
