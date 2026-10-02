import tomllib
from pathlib import Path

import pytest

from autopara.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def test_example_config_is_valid():
    config = load_config(str(EXAMPLE))
    assert config.schedule.group == "ТЦР-32"
    assert config.join.zoom_mode == "app"
    assert config.links[0].type == "Лк"
    assert config.links[-1].skip


@pytest.mark.parametrize(
    "toml, message",
    [
        ("[schedule]\n", "group"),
        ('[schedule]\ngroup = "ТЦР-32"\ngruop_id = 1\n', "невідомі параметри gruop_id"),
        ('[schedule]\ngroup = "ТЦР-32"\ncourse = "3"\n', "course"),
        ('[schedule]\ngroup = "ТЦР-32"\ncourse = true\n', "course"),
        ('[schedule]\ngroup = "ТЦР-32"\n[join]\nzoom_mode = "web"\n', "zoom_mode"),
        ('[schedule]\ngroup = "ТЦР-32"\n[[links]]\nsubject = "БД"\n', "url або skip"),
        ('[schedule]\ngroup = "ТЦР-32"\n[[links]]\nurl = "https://meet.google.com/a"\n', "subject"),
    ],
)
def test_config_errors(toml, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(tomllib.loads(toml))


def test_links_are_normalized():
    config = parse_config(
        tomllib.loads('[schedule]\ngroup = "ТЦР-32"\n[[links]]\nsubject = "БД"\nurl = " meet.google.com/aaa-bbbb-ccc "\n')
    )
    assert config.links[0].url == "https://meet.google.com/aaa-bbbb-ccc"


REPO_CONFIG = EXAMPLE.with_name("config.toml")


def test_repo_config_is_valid():
    config = load_config(str(REPO_CONFIG))
    yatpz_lectures = next(rule for rule in config.links if rule.type == "Лк")
    assert yatpz_lectures.url.endswith("?authuser=denys.personal%40gmail.com")
    assert config.join.meet_account == "st1234567@stud.duikt.edu.ua"


def test_account_only_for_meet():
    toml = '[schedule]\ngroup = "ТЦР-32"\n[[links]]\nsubject = "БД"\naccount = "a@b.c"\nurl = "https://zoom.us/j/123456789"\n'
    with pytest.raises(ConfigError, match="лише для Google Meet"):
        parse_config(tomllib.loads(toml))


def test_local_overlay_keeps_secrets_out_of_git(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[schedule]\ngroup = "ТЦР-32"\n[telegram]\nchat_id = 1\n'
        '[[links]]\nsubject = "БД"\nurl = "https://meet.google.com/aaa-bbbb-ccc"\n',
        encoding="utf-8",
    )
    (tmp_path / "config.local.toml").write_text(
        '[telegram]\ntoken = "123:abc"\n[[links]]\nsubject = "БД"\nskip = true\n', encoding="utf-8"
    )
    config = load_config(str(tmp_path / "config.toml"))
    assert (config.telegram.token, config.telegram.chat_id) == ("123:abc", 1)
    assert config.links[0].skip  # правила з local — пріоритетніші


def test_schedule_date_formats():
    from datetime import date

    from autopara.cli import parse_day

    today = date(2026, 10, 2)
    assert parse_day("07.10", today) == parse_day("7.10.2026", today) == parse_day("2026-10-07", today) == date(2026, 10, 7)
    with pytest.raises(ConfigError):
        parse_day("сьомого", today)
