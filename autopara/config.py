"""Налаштування: де лежать, як читаються, перевіряються й зберігаються."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .links import LinkRule, normalize_url, platform_of

DEFAULT_DATA_DIR = Path.home() / ".autopara"
UNIVERSITIES = {"duikt": "ДУІКТ", "kpi": "КПІ ім. Ігоря Сікорського"}


def user_config_dir() -> Path:
    """Стандартна тека налаштувань програми для поточного користувача."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "autopara"


def user_config_path() -> Path:
    return user_config_dir() / "config.toml"


class ConfigError(Exception):
    pass


@dataclass
class ScheduleSettings:
    university: str = "duikt"  # звідки брати розклад: duikt | kpi
    group: str = ""
    faculty_id: int | None = None  # ДУІКТ: ID з `autopara find-group`, щоб не шукати групу щоразу
    course: int | None = None
    group_id: int | None = None
    group_uid: str = ""  # КПІ: ідентифікатор групи
    student: str = ""  # ДУІКТ: розклад конкретного студента (з підгрупами)
    days_ahead: int = 7
    refresh_minutes: int = 60
    use_schedule_links: bool = True
    base_url: str = ""  # порожньо — адреса за замовчуванням для університету


@dataclass
class JoinSettings:
    accounts: list = field(default_factory=list)  # інші Google-акаунти (для посилань, які не пускають з основного)
    minutes_before: int = 2
    mute_microphone: bool = True
    camera_off: bool = True
    mute_sound: bool = True
    auto_leave: bool = True
    smart_leave: bool = True
    max_overtime_minutes: int = 20
    leave_after_minutes: int = 0
    # "auto" — Zoom-клієнт, якщо вуха можуть його чути (Linux, віртуальний кабель), інакше Zoom у браузері,
    # а якщо там не пустять — таки клієнт; "app" — завжди клієнт; "browser" — завжди браузер.
    zoom_mode: str = "auto"
    display_name: str = ""
    meet_account: str = ""


@dataclass
class MeSettings:
    name: str = ""  # «Прізвище Ім'я» — щоб помічати, коли тебе кличуть, і відповідати на перекличку
    chat_reply: str = "auto"  # "auto" | "ask" | "off": чи писати в чат пари самому, лише питати чи ніколи
    # Що саме помічник може писати сам. Вимкнене — лише питає тебе (ввічливості — просто мовчить).
    roll_call: bool = True  # відмічатися на перекличці: «+», «присутній», прізвище
    answer_here: bool = True  # відповідати, коли викладач питає саме тебе «…, ви тут?»
    polite: bool = True  # дублювати за групою «дякую», «добрий день», «до побачення»
    present: str = "Присутній"  # що писати на перекличці, коли група відмічається «присутній», «тут»
    here: str = "Так, я тут"  # відповідь на «Ляшко, ви тут?»
    # Готові відповіді-кнопки в Telegram, коли тебе кличуть: натиснув — і це пішло в чат пари.
    quick_replies: list = field(default_factory=lambda: ["Присутній", "Так, я тут"])


@dataclass
class TelegramSettings:
    token: str = ""
    chat_id: int = 0
    commands: bool = True  # бот відповідає на команди й кнопки; false — лише надсилає сповіщення
    mentions: bool = True  # сповіщати, коли тебе кличуть
    clips: bool = True  # відеовирізка моменту, коли тебе згадали (потрібен ffmpeg; звук — якщо ввімкнені вуха)
    reports: bool = True  # звіт після пари
    joins: bool = True  # повідомляти, коли зайшов чи вийшов з пари (невдачі — завжди)
    desktop: bool = True  # дублювати сповіщення на робочий стіл (Linux)


@dataclass
class EarsSettings:
    enabled: bool = True
    model: str = "auto"  # наживо: "auto" — large-v3 з відеокартою NVIDIA, large-v3-turbo на потужному комп'ютері, інакше small
    # Відеокарта: "auto" — де допомагає (Whisper на NVIDIA, мовна модель на окремій карті); "cpu" — не використовувати
    # взагалі (ні для вух, ні для мовної моделі); "cuda" — Whisper на NVIDIA навіть з малою пам'яттю карти.
    device: str = "auto"
    # Друге, точніше розпізнавання після пари — для звіту й запису: "auto" — large-v3 на потужному
    # комп'ютері (інакше та сама модель, що наживо); "off" — не уточнювати, звіт одразу з живої розшифровки.
    refine_model: str = "auto"
    language: str = "uk"
    # Пари в браузері: "auto"/"browser" — звук лише з вкладки пари (будь-яка ОС); "pulse" — увесь звук
    # браузера програми через PipeWire/PulseAudio (Linux, запасний шлях).
    source: str = "auto"
    # Zoom-клієнт на Windows/macOS: пристрій запису віртуального кабелю, куди Zoom виводить звук
    # (наприклад, "CABLE Output" з VB-Cable чи "BlackHole"). На Linux не потрібен — там чуємо сам Zoom.
    app_device: str = ""


@dataclass
class BrainSettings:
    enabled: bool = True  # працює, щойно встановлена Ollama; модель програма завантажить сама
    model: str = "gemma3:4b"
    url: str = "http://127.0.0.1:11434"
    # Більша модель для розмов у Telegram поза парами (розумніша, повільніша; "" — та сама, що й model).
    chat_model: str = ""


@dataclass
class UpdateSettings:
    auto: bool = True  # самому ставити нові версії з GitHub (лише в копії з архіву; копію з git — ні)


@dataclass
class Config:
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    join: JoinSettings = field(default_factory=JoinSettings)
    links: list[LinkRule] = field(default_factory=list)
    me: MeSettings = field(default_factory=MeSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    ears: EarsSettings = field(default_factory=EarsSettings)
    brain: BrainSettings = field(default_factory=BrainSettings)
    updates: UpdateSettings = field(default_factory=UpdateSettings)
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


# Розділи й параметри з прибраних функцій: старий файл налаштувань з ними не має ламати запуск.
RETIRED_SECTIONS = {"study", "agent"}
RETIRED_KEYS = {"join": {"browser_channel", "browser_path"}}  # тепер завжди вбудований браузер програми


def parse_config(data: dict) -> Config:
    allowed = {"schedule", "join", "links", "me", "telegram", "ears", "brain", "updates", "timezone", "data_dir"}
    data = {key: value for key, value in data.items() if key not in RETIRED_SECTIONS}
    for section, keys in RETIRED_KEYS.items():
        if isinstance(data.get(section), dict):
            data[section] = {key: value for key, value in data[section].items() if key not in keys}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"невідомі розділи конфіга: {', '.join(sorted(unknown))}")

    schedule = _build(ScheduleSettings, data.get("schedule", {}), "schedule")
    if schedule.university not in UNIVERSITIES:
        raise ConfigError(f"[schedule] university має бути одним з: {', '.join(UNIVERSITIES)}")
    join = _build(JoinSettings, data.get("join", {}), "join")
    if join.zoom_mode not in ("auto", "app", "browser"):
        raise ConfigError('[join] zoom_mode має бути "auto", "app" або "browser"')
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
            if rule.account and platform_of(url) != "meet":
                raise ConfigError(f"[[links]] #{index} ({rule.subject}): account працює лише для Google Meet")
            rule = replace(rule, url=url)
        rules.append(rule)

    me = _build(MeSettings, data.get("me", {}), "me")
    if me.chat_reply not in ("auto", "ask", "off"):
        raise ConfigError('[me] chat_reply має бути "auto", "ask" або "off"')
    if not all(isinstance(reply, str) and reply.strip() for reply in me.quick_replies):
        raise ConfigError("[me] quick_replies — це список непорожніх рядків")
    telegram = _build(TelegramSettings, data.get("telegram", {}), "telegram")
    if telegram.token and ":" not in telegram.token:
        raise ConfigError("[telegram] token виглядає неправильно — візьми його в @BotFather")
    ears = _build(EarsSettings, data.get("ears", {}), "ears")
    if ears.source not in ("auto", "browser", "pulse"):
        raise ConfigError('[ears] source має бути "auto", "browser" або "pulse"')
    if ears.device not in ("auto", "cpu", "cuda"):
        raise ConfigError('[ears] device має бути "auto", "cpu" або "cuda"')
    config = Config(
        schedule=schedule,
        join=join,
        links=rules,
        me=me,
        telegram=telegram,
        ears=ears,
        brain=_build(BrainSettings, data.get("brain", {}), "brain"),
        updates=_build(UpdateSettings, data.get("updates", {}), "updates"),
    )
    if "timezone" in data:
        config.timezone = str(data["timezone"])
    if "data_dir" in data:
        config.data_dir = Path(str(data["data_dir"])).expanduser()
    return config


def find_config(explicit: str | None) -> Path | None:
    """Де шукати налаштування: явний шлях → ./config.toml → тека користувача → стара ~/.autopara."""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"файл конфіга {path} не знайдено")
        return path
    for path in (Path("config.toml"), user_config_path(), DEFAULT_DATA_DIR / "config.toml"):
        if path.is_file():
            return path
    return None


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


def read_config(path: Path) -> Config:
    data = _read_toml(path)
    # Секрети (токен Telegram-бота тощо) можна тримати в config.local.toml поруч — він не в git.
    local = path.with_name("config.local.toml")
    if local.is_file():
        data = merge_config(data, _read_toml(local))
    try:
        return parse_config(data)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e


def load_config(explicit: str | None = None) -> Config:
    path = find_config(explicit)
    if path is None:
        raise ConfigError(
            f"налаштувань ще немає — запусти `autopara app` і заповни їх у вікні програми "
            f"(або `autopara import-config файл.toml`; зберігаються в {user_config_path()})"
        )
    return read_config(path)


def _clean(value: Any) -> Any:
    """Прибрати None (у TOML його немає) і привести шляхи до рядків."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def config_to_dict(config: Config) -> dict:
    data = _clean(asdict(config))
    # У правилах посилань пишемо лише заповнені поля — так файл читається людиною.
    data["links"] = [{key: value for key, value in rule.items() if value not in ("", False)} for rule in data["links"]]
    if config.data_dir == DEFAULT_DATA_DIR:
        data.pop("data_dir")
    return data


def save_config(config: Config, path: Path | None = None) -> Path:
    """Записати налаштування (атомарно: спершу в тимчасовий файл, потім підмінити)."""
    import tomli_w

    path = path or user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".toml.tmp")
    temporary.unlink(missing_ok=True)
    # Тут може бути токен Telegram-бота — файл читає лише власник.
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
        f.write(tomli_w.dumps(config_to_dict(config)))
    temporary.replace(path)
    local = path.with_name("config.local.toml")
    if local.is_file():
        # Його вміст уже злитий у збережений файл; якби він лишився, правила посилань задвоїлись би.
        local.replace(local.with_suffix(".toml.merged"))
    return path


def import_config(source: Path, target: Path | None = None, local: Path | None = None) -> Path:
    """Взяти налаштування з файла (наприклад, профілю) у теку користувача; старі — у .bak.

    ``local`` — окремий config.local.toml із секретами (токен бота), який накласти зверху.
    """
    config = read_config(source)
    if local is not None and local.is_file() and local != source.with_name("config.local.toml"):
        try:
            config = parse_config(merge_config(_read_toml(source), _read_toml(local)))
        except ConfigError as e:
            raise ConfigError(f"{local}: {e}") from e
    target = target or user_config_path()
    if target.exists():
        target.with_suffix(".toml.bak").write_bytes(target.read_bytes())
    return save_config(config, target)
