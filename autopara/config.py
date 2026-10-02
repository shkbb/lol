"""Завантаження config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .links import LinkRule, normalize_url
from .rozklad import BASE_URL

DEFAULT_DATA_DIR = Path.home() / ".autopara"


class ConfigError(Exception):
    pass


@dataclass
class ScheduleSettings:
    group: str = ""
    faculty_id: int | None = None
    course: int | None = None
    group_id: int | None = None
    student: str = ""
    days_ahead: int = 7
    refresh_minutes: int = 60
    use_schedule_links: bool = True
    base_url: str = BASE_URL


@dataclass
class JoinSettings:
    minutes_before: int = 2
    mute_microphone: bool = True
    camera_off: bool = True
    mute_sound: bool = True
    auto_leave: bool = True
    smart_leave: bool = True
    max_overtime_minutes: int = 20
    leave_after_minutes: int = 0
    zoom_mode: str = "app"
    display_name: str = ""
    meet_account: str = ""
    browser_channel: str = "chrome"
    browser_path: str = ""


@dataclass
class Config:
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    join: JoinSettings = field(default_factory=JoinSettings)
    links: list[LinkRule] = field(default_factory=list)
    timezone: str = "Europe/Kyiv"
    data_dir: Path = DEFAULT_DATA_DIR

    @property
    def profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"


def _build(cls, data: Any, section: str):
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] має бути таблицею")
    known = {f.name: f for f in fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ConfigError(f"[{section}]: невідомі параметри {', '.join(sorted(unknown))}")
    values = {}
    for name, value in data.items():
        default = getattr(cls(), name)
        expected = type(default) if default is not None else int
        # bool — підклас int, тож перевіряємо окремо, щоб «course = true» не пройшло як 1.
        if isinstance(value, bool) != (expected is bool) or not isinstance(value, expected):
            raise ConfigError(f"[{section}] {name}: очікується {expected.__name__}, а не {value!r}")
        values[name] = value
    return cls(**values)


def parse_config(data: dict) -> Config:
    allowed = {"schedule", "join", "links", "timezone", "data_dir"}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"невідомі розділи конфіга: {', '.join(sorted(unknown))}")

    schedule = _build(ScheduleSettings, data.get("schedule", {}), "schedule")
    if not schedule.group and not schedule.group_id:
        raise ConfigError("[schedule] вкажи group, наприклад group = \"ТЦР-32\"")
    join = _build(JoinSettings, data.get("join", {}), "join")
    if join.zoom_mode not in ("app", "browser"):
        raise ConfigError('[join] zoom_mode має бути "app" або "browser"')
    if min(join.minutes_before, join.leave_after_minutes, join.max_overtime_minutes) < 0:
        raise ConfigError("[join] хвилини не можуть бути від'ємними")

    rules = []
    for index, raw in enumerate(data.get("links", []), start=1):
        rule = _build(LinkRule, raw, f"links #{index}")
        if not (rule.subject or rule.type or rule.teacher):
            raise ConfigError(f"[[links]] #{index}: вкажи хоча б subject, type або teacher")
        if not rule.skip and not rule.url:
            raise ConfigError(f"[[links]] #{index} ({rule.subject}): вкажи url або skip = true")
        if rule.url:
            rule = LinkRule(rule.subject, rule.type, rule.teacher, normalize_url(rule.url), rule.skip)
        rules.append(rule)

    config = Config(schedule=schedule, join=join, links=rules)
    if "timezone" in data:
        config.timezone = str(data["timezone"])
    if "data_dir" in data:
        config.data_dir = Path(str(data["data_dir"])).expanduser()
    return config


def find_config(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"файл конфіга {path} не знайдено")
        return path
    for path in (Path("config.toml"), DEFAULT_DATA_DIR / "config.toml"):
        if path.is_file():
            return path
    raise ConfigError(
        "не знайдено config.toml: скопіюй config.example.toml у config.toml і заповни його "
        f"(шукаю в поточній папці та в {DEFAULT_DATA_DIR})"
    )


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: помилка в TOML: {e}") from e
    try:
        return parse_config(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e
