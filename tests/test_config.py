from pathlib import Path

import pytest

from jobfeed.config import Config, ConfigError, load_config
from tests.conftest import REPO_ROOT


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_repo_config_matches_the_built_in_defaults():
    # config.yaml spells out the defaults, so deleting a line changes nothing surprising.
    assert load_config(REPO_ROOT / "config.yaml") == Config()


def test_missing_file_means_defaults(tmp_path):
    assert load_config(tmp_path / "nope.yaml") == Config()


def test_partial_sections_keep_other_defaults(tmp_path):
    cfg = load_config(_write(tmp_path, "salary:\n  floor_usd: 175000\n"))
    assert cfg.salary.floor_usd == 175_000
    assert cfg.salary.compare == "max"
    assert cfg.location.home_state == "WA"


def test_home_state_is_uppercased(tmp_path):
    assert load_config(_write(tmp_path, "location:\n  home_state: wa\n")).location.home_state == "WA"


@pytest.mark.parametrize("text,message", [
    ("salary:\n  flor_usd: 1\n", "unknown setting `salary.flor_usd`"),
    ("salaries:\n  floor_usd: 1\n", "unknown section"),
    ("salary:\n  floor_usd: lots\n", "must be a whole number"),
    ("salary:\n  keep_missing: maybe\n", "must be true or false"),
    ("salary:\n  compare: middle\n", "must be max or min"),
    ("location:\n  remote_regions: [us, mars]\n", "unknown region"),
    ("role:\n  exclude: ['(unclosed']\n", "bad pattern"),
    ("role:\n  include: []\n", "needs at least one pattern"),
    ("seniority:\n  prefer: [wizard]\n", "unknown level"),
    ("role: [a, b]\n", "must be a mapping"),
    ("role:\n  include: [a\n", "isn't valid YAML"),
])
def test_bad_configs_say_what_is_wrong(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_write(tmp_path, text))
