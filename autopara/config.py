"""Завантаження config.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .links import LinkRule, meet_url, normalize_url, platform_of
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
class MeSettings:
    name: str = ""  # «Прізвище Ім'я» — щоб помічати, коли тебе кличуть, і відповідати на перекличку
    chat_reply: str = "auto"  # "auto" | "ask" | "off": чи відповідати в чат, коли група масово пише «+»
    present: str = "Присутній"  # що писати, коли група відмічається словами «присутній», «тут»


@dataclass
class TelegramSettings:
    token: str = ""
    chat_id: int = 0


@dataclass
class EarsSettings:
    enabled: bool = False
    model: str = "small"
    language: str = "uk"


@dataclass
class BrainSettings:
    enabled: bool = False
    model: str = "gemma3:4b"
    url: str = "http://127.0.0.1:11434"


@dataclass
class Config:
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    join: JoinSettings = field(default_factory=JoinSettings)
    links: list[LinkRule] = field(default_factory=list)
    me: MeSettings = field(default_factory=MeSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    ears: EarsSettings = field(default_factory=EarsSettings)
    brain: BrainSettings = field(default_factory=BrainSettings)
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
    allowed = {"schedule", "join", "links", "me", "telegram", "ears", "brain", "timezone", "data_dir"}
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
            url = normalize_url(rule.url)
            if rule.account:
                if platform_of(url) != "meet":
                    raise ConfigError(f"[[links]] #{index} ({rule.subject}): account працює лише для Google Meet")
                url = meet_url(url, rule.account)
            rule = replace(rule, url=url)
        rules.append(rule)

    me = _build(MeSettings, data.get("me", {}), "me")
    if me.chat_reply not in ("auto", "ask", "off"):
        raise ConfigError('[me] chat_reply має бути "auto", "ask" або "off"')
    telegram = _build(TelegramSettings, data.get("telegram", {}), "telegram")
    if telegram.token and ":" not in telegram.token:
        raise ConfigError("[telegram] token виглядає неправильно — візьми його в @BotFather")
    config = Config(
        schedule=schedule,
        join=join,
        links=rules,
        me=me,
        telegram=telegram,
        ears=_build(EarsSettings, data.get("ears", {}), "ears"),
        brain=_build(BrainSettings, data.get("brain", {}), "brain"),
    )
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


def merge_config(base: dict, local: dict) -> dict:
    """config.local.toml поверх config.toml: розділи зливаються по ключах, його [[links]] — пріоритетніші."""
    merged = dict(base)
    for key, value in local.items():
        if key == "links":
            merged["links"] = list(value) + list(base.get("links", []))
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            merged[key] = {**base[key], **value}
        else:
            merged[key] = value
    return merged


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: помилка в TOML: {e}") from e


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    data = _read_toml(path)
    # Секрети (токен Telegram-бота тощо) — у config.local.toml поруч, який не потрапляє в git.
    local = path.with_name("config.local.toml")
    if local.is_file():
        data = merge_config(data, _read_toml(local))
    try:
        return parse_config(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e
