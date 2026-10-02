"""Командний рядок: ``autopara <команда>``."""

from __future__ import annotations

import argparse
import logging
import secrets
import signal
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config, ConfigError, find_config, import_config, load_config, read_config, user_config_path
from .links import platform_of, resolve
from .planner import Plan, Visit, build_plan
from .rozklad import Lesson, RozkladError
from .runner import Runner
from .sources import make_source, resolve_target
from .wiring import assistant_stack

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
PLATFORM_NAMES = {"meet": "Meet", "zoom": "Zoom", "other": "сайт"}

log = logging.getLogger("autopara")


def setup_logging(config: Config | None, verbose: bool) -> None:
    # Без консолі (pythonw на Windows) stderr немає — тоді лише у файл.
    handlers: list[logging.Handler] = [logging.StreamHandler()] if sys.stderr is not None else []
    if config is not None:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config.data_dir / "autopara.log", encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%d.%m %H:%M:%S",
        handlers=handlers,
    )


def _client(config: Config):
    return make_source(config)


def cmd_find_group(args, config: Config) -> None:
    client = _client(config)
    s = config.schedule
    print(f"Шукаю групу «{s.group}» на сайті розкладу...")
    target = client.find_group(s.group, s.faculty_id, s.course)
    print(
        f"Знайшов {target.group_name}: «{target.faculty_name or '—'}», {target.course} курс.\n"
        "Щоб не шукати щоразу, можна додати в [schedule]:\n"
        f"  faculty_id = {target.faculty_id}\n  course = {target.course}\n  group_id = {target.group_id}"
    )
    if args.students:
        print("\nСтуденти групи (для параметра student):")
        for student in client.students(target.faculty_id, target.course, target.group_id):
            print(f"  {student.name}")


def _status(lesson: Lesson, config: Config) -> str:
    resolution = resolve(lesson, config.links, config.schedule.use_schedule_links)
    if resolution.source == "skip":
        return "— пропускаю"
    if resolution.url is None:
        return "✗ немає посилання"
    where = PLATFORM_NAMES[platform_of(resolution.url)]
    origin = " (з розкладу)" if resolution.source == "schedule" else ""
    return f"✓ {where}{origin}: {resolution.url}"


def parse_day(text: str, today: date) -> date:
    """«07.10», «7.10.2026» або «2026-10-07». Без року — поточний."""
    text = text.strip()
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m"):
        try:
            parsed = datetime.strptime(text, pattern).date()
        except ValueError:
            continue
        return parsed.replace(year=today.year) if pattern == "%d.%m" else parsed
    raise ConfigError(f"не розумію дату «{text}» — напиши, наприклад, 07.10")


def cmd_schedule(args, config: Config) -> None:
    client = _client(config)
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    target = resolve_target(client, config)
    if args.date:
        first = last = parse_day(args.date, now.date())
    else:
        days = args.days if args.days is not None else config.schedule.days_ahead
        first, last = now.date(), now.date() + timedelta(days=days)
    lessons = client.schedule(target, first, last)
    who = target.student_name or target.group_name or config.schedule.group
    period = f"{WEEKDAYS[first.weekday()]} {first:%d.%m}" if first == last else f"{first:%d.%m} – {last:%d.%m}"
    print(f"Розклад: {who}, {period}\n")
    if not lessons:
        print("Пар немає.")
    previous_day = None
    for lesson in lessons:
        day = f"{WEEKDAYS[lesson.day.weekday()]} {lesson.day:%d.%m}" if lesson.day != previous_day else ""
        previous_day = lesson.day
        print(f"{day:<9} {lesson.start:%H:%M}–{lesson.end:%H:%M}  {lesson.title:<50.50} {_status(lesson, config)}")
    plan = build_plan(lessons, config.links, config.join, tz, now, config.schedule.use_schedule_links)
    for message in plan.conflicts:
        print(f"\n⚠ {message}")


def _google_accounts(config: Config) -> list[str]:
    accounts = [config.join.meet_account] + [rule.account for rule in config.links if rule.account]
    return list(dict.fromkeys(account for account in accounts if account))


def cmd_login(args, config: Config) -> None:
    from .joiner import Browser

    browser = Browser(
        config.profile_dir, config.join.browser_channel, mute_sound=False, executable_path=config.join.browser_path
    )
    accounts = _google_accounts(config)
    try:
        browser.new_page().goto("https://accounts.google.com/")
        if config.join.zoom_mode == "browser":
            browser.new_page().goto("https://zoom.us/signin")
        if accounts:
            print("Увійди у вікні браузера в усі ці Google-акаунти (наступний — через «Додати обліковий запис»):")
            for account in accounts:
                print(f"  • {account}")
        else:
            print("Увійди у вікні браузера в Google-акаунт, з яким ходиш на пари.")
        input("Коли закінчиш — натисни Enter тут... ")
    finally:
        browser.close()
    print(f"Готово. Профіль браузера збережено в {config.profile_dir}")


def _clock(config: Config):
    tz = ZoneInfo(config.timezone)
    return lambda: datetime.now(tz)


def cmd_join(args, config: Config) -> None:
    """Тестовий вхід: працює так само, як на справжній парі, і показує, що програма бачить."""
    from .joiner import Joiner

    clock = _clock(config)
    now = clock()
    leave_at = now + timedelta(minutes=args.minutes)
    lesson = Lesson(now.date(), now.time(), leave_at.time(), "Тестовий вхід")
    visit = Visit(args.url, platform_of(args.url), [lesson], now, leave_at)
    logging.getLogger("autopara.ears").setLevel(logging.DEBUG)  # показувати розпізнану мову
    with assistant_stack(config, clock) as stack:
        joiner = Joiner(config.join, config.profile_dir, audio_sink=stack.audio_sink, frames=stack.frames)
        runner = Runner(config, _client(config), joiner, clock=clock, assistant=stack.assistant)
        runner.plan = Plan(visits=[visit])
        runner.next_refresh = leave_at + timedelta(days=1)  # розклад не потрібен
        print("Заходжу. Ctrl+C — вийти.")
        shown = None
        try:
            while True:
                runner.tick()
                if visit.key in runner.finished and visit.key not in runner.active:
                    break
                seen = runner.last_seen.get(visit.key)
                if seen is not None and seen.participants != shown:
                    shown = seen.participants
                    if shown:
                        print(f"👥 Бачу учасників: {shown} — розумний вихід працює.")
                    else:
                        print("👥 Не бачу лічильника учасників — з такої зустрічі виходитиму за розкладом.")
                joiner.idle(5)
        except KeyboardInterrupt:
            pass
        finally:
            joiner.close()


def _app_config(explicit: str | None) -> tuple[Config, Path]:
    """Для програми налаштування необов'язкові: без них відкриється вікно, де їх і заповнять."""
    path = find_config(explicit)
    if path is None:
        return Config(), user_config_path()
    return read_config(path), path


def _serve(args, open_ui: bool) -> None:
    from .app import serve

    config, path = _app_config(args.config)
    setup_logging(config, args.verbose)
    # systemctl stop і kill шлють SIGTERM: перетворюємо його на звичайний вихід, щоб закрити браузер
    # і зняти заборону сну.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    serve(config, path, open_ui=open_ui)


def cmd_app(args) -> None:
    """Відкрити вікно програми; якщо вона ще не працює — запустити."""
    from .app import open_window, running_instance

    if instance := running_instance():
        print("autopara вже працює — відкриваю її вікно.")
        path = find_config(args.config)
        open_window(instance["url"], read_config(path) if path else None)
        return
    _serve(args, open_ui=True)


def cmd_run(args) -> None:
    """Працювати у фоні (для автозапуску). Вікно можна відкрити будь-коли: autopara app."""
    from .app import running_instance

    if instance := running_instance():
        raise ConfigError(f"autopara вже працює (процес {instance.get('pid')}) — відкрий її вікно: autopara app")
    _serve(args, open_ui=False)


def cmd_import_config(args) -> None:
    from .app import push_config, running_instance

    source = Path(args.file).expanduser()
    if not source.is_file():
        raise ConfigError(f"файл {source} не знайдено")
    target = Path(args.config).expanduser() if args.config else user_config_path()
    # Секрети (токен бота) з config.local.toml у поточній теці теж переносимо — щоб нічого не загубилось.
    local = Path("config.local.toml")
    local = local if local.is_file() else None
    path = import_config(source, target, local=local)
    print(f"Готово: налаштування з {source} збережено в {path}")
    if local is not None:
        print(f"Секрети з {local} (токен бота тощо) теж перенесено.")
    if instance := running_instance():
        result = push_config(instance, read_config(path))
        print("autopara вже працювала — " + {
            "after_lesson": "нові налаштування застосуються, щойно скінчиться поточна пара.",
            "started": "налаштування застосовано, автоматичний вхід на пари увімкнено.",
            "saved": "налаштування застосовано (автоматичний вхід вимкнено — увімкни його у вікні).",
        }.get(result, "нові налаштування застосовано."))


def cmd_telegram(args, config: Config) -> None:
    from .telegram import TelegramBot, TelegramError, wait_for_code

    token = config.telegram.token
    if not token:
        print(
            "Найпростіше — у вікні програми (`autopara app`), вкладка «Telegram». Або вручну:\n"
            "1. У Telegram напиши @BotFather → /newbot → придумай назву. Він дасть токен.\n"
            "2. Допиши його у файл налаштувань (або в config.local.toml поруч, він не потрапляє в git):\n\n"
            '   [telegram]\n   token = "123456:ABC..."\n\n'
            "3. Запусти `autopara telegram` ще раз."
        )
        return
    try:
        bot = TelegramBot(token, config.telegram.chat_id)
        username = bot.me().get("username", "")
        if not config.telegram.chat_id:
            code = f"{secrets.randbelow(10**6):06d}"
            print(
                f"Відкрий https://t.me/{username}?start={code} і натисни «Start»\n"
                f"(або знайди @{username} у Telegram і надішли йому код {code}). Чекаю 3 хвилини..."
            )
            chat_id = wait_for_code(token, code)
            if chat_id is None:
                print("Не дочекався коду. Спробуй ще раз.")
                return
            print(f"Підключено! Допиши в [telegram]:\n   chat_id = {chat_id}")
            return
        bot.send("👋 autopara на зв'язку. Напиши «допомога», щоб побачити команди.")
        print(f"Надіслав тестове повідомлення від @{username} — перевір Telegram.")
    except TelegramError as e:
        print(f"Помилка: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_check(args, config: Config) -> None:
    """Перевіряє на твоєму комп'ютері все, що можна перевірити без пари."""
    from .checks import run_checks

    for check in run_checks(config):
        mark = "✓" if check.ok else ("·" if check.ok is None else "✗")
        print(f"{mark} {check.area}: {check.text}")


def cmd_listen_test(args, config: Config) -> None:
    """Порівняти моделі розпізнавання на справжньому звуці, що зараз грає на ноуті."""
    from time import monotonic

    from .ears import RATE, EarsError, WhisperTranscriber, clean_text, ears_available, record

    if problem := ears_available():
        print(f"Помилка: {problem}", file=sys.stderr)
        sys.exit(1)
    models = [model.strip() for model in (args.models or config.ears.model).split(",") if model.strip()]
    print(f"Записую {args.seconds} с того, що грає на ноуті. Увімкни, наприклад, лекцію українською на YouTube...")
    pcm = record("@DEFAULT_MONITOR@", args.seconds)
    seconds = len(pcm) / (RATE * 2)
    if seconds < 1:
        print("Нічого не записалось — перевір, що звук грає.", file=sys.stderr)
        sys.exit(1)
    hint = config.me.name
    for model in models:
        try:
            transcriber = WhisperTranscriber(model, config.ears.language, hints=lambda: hint)
        except EarsError as e:
            print(f"Помилка: {e}", file=sys.stderr)
            sys.exit(1)
        started = monotonic()
        heard = transcriber(pcm)
        took = monotonic() - started
        speed = seconds / took if took else float("inf")
        verdict = "встигає" if speed >= 2 else ("ледь встигає" if speed >= 1.2 else "НЕ встигає")
        print(f"\n=== {model}: {took:.1f} с на {seconds:.0f} с звуку — у {speed:.1f} раза швидше за мову, {verdict} ===")
        for item in heard:
            if text := clean_text(item.text):
                print(("   " if item.confident else "[?] ") + text)
    print(
        "\n[?] — модель сама не впевнена в цьому шматку. Такі шматки ніколи не підтверджують дій, "
        "лише потрапляють у сповіщення й звіт з позначкою.\n"
        "Для живої пари модель має бути щонайменше вдвічі швидшою за мову."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopara",
        description="Автоматично заходить на пари (ДУІКТ, КПІ) у Google Meet і Zoom за розкладом групи. "
        "Почни з `autopara app` — відкриється вікно з усіма налаштуваннями.",
    )
    parser.add_argument("-c", "--config", help="шлях до config.toml")
    parser.add_argument("-v", "--verbose", action="store_true", help="детальний лог")
    sub = parser.add_subparsers(dest="command", required=True, metavar="команда")

    find = sub.add_parser("find-group", help="знайти групу на сайті розкладу і показати її ID")
    find.add_argument("--students", action="store_true", help="показати список студентів групи")
    find.set_defaults(func=cmd_find_group)

    schedule = sub.add_parser("schedule", help="показати розклад і які посилання відкриються")
    schedule.add_argument("--days", type=int, help="на скільки днів уперед (за замовчуванням — з конфіга)")
    schedule.add_argument("--date", help="лише один день, наприклад 07.10")
    schedule.set_defaults(func=cmd_schedule)

    login = sub.add_parser("login", help="відкрити браузер, щоб один раз увійти в Google")
    login.set_defaults(func=cmd_login)

    join = sub.add_parser("join", help="зайти за посиланням прямо зараз (перевірка)")
    join.add_argument("url")
    join.add_argument("--minutes", type=int, default=90, help="скільки чекати, поки впустять")
    join.set_defaults(func=cmd_join)

    app = sub.add_parser("app", help="відкрити вікно програми (налаштування, стан, журнал)")
    app.set_defaults(func=cmd_app, needs_config=False)

    run = sub.add_parser("run", help="працювати у фоні без вікна (для автозапуску)")
    run.set_defaults(func=cmd_run, needs_config=False)

    importing = sub.add_parser("import-config", help="взяти налаштування з файла, наприклад profiles/group.toml")
    importing.add_argument("file")
    importing.set_defaults(func=cmd_import_config, needs_config=False)

    telegram = sub.add_parser("telegram", help="налаштувати Telegram-бота для сповіщень і команд")
    telegram.set_defaults(func=cmd_telegram)

    check = sub.add_parser("check", help="перевірити, що все налаштовано")
    check.set_defaults(func=cmd_check)

    listen = sub.add_parser("listen-test", help="порівняти моделі розпізнавання мови на звуці з ноута")
    listen.add_argument("--seconds", type=int, default=30, help="скільки секунд записати")
    listen.add_argument("--models", help="моделі через кому, наприклад small,large-v3-turbo")
    listen.set_defaults(func=cmd_listen_test)
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        if not getattr(args, "needs_config", True):
            args.func(args)
            return
        config = load_config(args.config)
        setup_logging(config, args.verbose)
        args.func(args, config)
    except (ConfigError, RozkladError) as e:
        print(f"Помилка: {e}", file=sys.stderr)
        sys.exit(1)


def window() -> None:
    """`autopara-window`: те саме, що `autopara app`, але на Windows без чорного вікна консолі."""
    main(["app"])


if __name__ == "__main__":
    main()
