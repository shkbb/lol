import tomllib
from pathlib import Path

import pytest

from autopara.config import ConfigError, load_config, parse_config

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"


def test_example_config_is_valid():
    config = load_config(str(EXAMPLE))
    assert config.schedule.group == "ТЦР-32"
    assert config.join.zoom_mode == "auto"
    assert config.links[0].type == "Лк"
    assert config.links[-1].skip


@pytest.mark.parametrize(
    "toml, message",
    [
        ('[schedule]\nuniversity = "lnu"\n', "university"),
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


# Приклад профілю групи (вигадані посилання й пошти): справжні профілі лежать лише локально, не в git.
PROFILE = Path(__file__).with_name("data") / "profile.toml"


def test_group_profile_is_valid():
    from datetime import date, time

    from autopara.links import resolve
    from autopara.rozklad import Lesson

    config = load_config(str(PROFILE))
    assert (config.schedule.university, config.schedule.group) == ("duikt", "ТЦР-32")
    assert config.join.meet_account == "st1234567@stud.duikt.edu.ua"
    lecture = Lesson(date(2026, 10, 7), time(9, 30), time(10, 50), "Якість та тестування ПЗ", type="Лк")
    assert resolve(lecture, config.links).url.endswith("?authuser=denys.personal%40gmail.com")


def test_save_and_import_roundtrip(tmp_path):
    from autopara.config import import_config, read_config, save_config

    target = tmp_path / "user" / "config.toml"
    import_config(PROFILE, target)
    first = read_config(target)
    assert first == load_config(str(PROFILE))
    first.telegram.token = "123:abc"
    save_config(first, target)
    again = read_config(target)
    assert again.telegram.token == "123:abc" and again.links == first.links
    import_config(PROFILE, target)  # повторний імпорт — старе в .bak
    assert (tmp_path / "user" / "config.toml.bak").exists()


def test_user_config_dir_follows_the_platform(monkeypatch, tmp_path):
    from autopara import config as module

    monkeypatch.setattr(module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert module.user_config_path() == tmp_path / "autopara" / "config.toml"
    monkeypatch.setattr(module.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert module.user_config_path() == tmp_path / "Roaming" / "autopara" / "config.toml"


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



def test_saving_absorbs_the_local_overlay(tmp_path):
    """Після збереження з вікна config.local.toml уже злитий у файл — інакше правила задвоїлись би."""
    import os

    from autopara.config import read_config, save_config

    test_local_overlay_keeps_secrets_out_of_git(tmp_path)
    path = tmp_path / "config.toml"
    config = read_config(path)
    save_config(config, path)
    assert not (tmp_path / "config.local.toml").exists() and (tmp_path / "config.local.toml.merged").exists()
    again = read_config(path)
    assert again == config and len(again.links) == 2
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0  # токен бота читає лише власник


def test_import_carries_secrets_from_a_separate_local_file(tmp_path):
    from autopara.config import import_config, read_config

    local = tmp_path / "config.local.toml"
    local.write_text('[telegram]\ntoken = "123:abc"\nchat_id = 42\n', encoding="utf-8")
    target = import_config(PROFILE, tmp_path / "user" / "config.toml", local=local)
    config = read_config(target)
    assert (config.telegram.token, config.telegram.chat_id) == ("123:abc", 42)
    assert config.schedule.group == "ТЦР-32" and config.join.accounts == ["denys.personal@gmail.com"]

def test_schedule_date_formats():
    from datetime import date

    from autopara.cli import parse_day

    today = date(2026, 10, 2)
    assert parse_day("07.10", today) == parse_day("7.10.2026", today) == parse_day("2026-10-07", today) == date(2026, 10, 7)
    with pytest.raises(ConfigError):
        parse_day("сьомого", today)


def test_retired_sections_are_ignored():
    """Розділ «Навчання» прибрано — старий config.toml з [study] / [agent] однаково читається."""
    config = parse_config({"schedule": {"group": "ТЦР-32"}, "study": {"enabled": True}, "agent": {"command": "agy"}})
    assert config.schedule.group == "ТЦР-32" and not hasattr(config, "study")


def test_old_browser_settings_are_ignored():
    """Вибору браузера більше немає (завжди вбудований) — старі файли з ним однаково читаються."""
    config = parse_config({"join": {"browser_channel": "chrome", "browser_path": "/usr/bin/chromium", "meet_account": "a@b.c"}})
    assert config.join.meet_account == "a@b.c" and not hasattr(config.join, "browser_channel")
