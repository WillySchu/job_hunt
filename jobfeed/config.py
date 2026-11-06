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
class Config:
    role: RoleRules = field(default_factory=RoleRules)
    location: LocationRules = field(default_factory=LocationRules)
    salary: SalaryRules = field(default_factory=SalaryRules)
    seniority: SeniorityRules = field(default_factory=SeniorityRules)
    digest: DigestRules = field(default_factory=DigestRules)
    aggregators: AggregatorRules = field(default_factory=AggregatorRules)


_SECTIONS = {"role": RoleRules, "location": LocationRules, "salary": SalaryRules,
             "seniority": SeniorityRules, "digest": DigestRules, "aggregators": AggregatorRules}
_REGIONS = {"us", "americas", "global", "other", "unknown"}
_RANKS = {"junior", "mid", "senior", "staff", "principal"}


def _check_type(where: str, value: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{where} must be true or false (got {value!r})")
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{where} must be a whole number (got {value!r})")
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
