"""Командний рядок: ``autopara <команда>``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime, timedelta
from pathlib import Path
from time import sleep
from zoneinfo import ZoneInfo

from .config import Config, ConfigError, load_config
from .links import platform_of, resolve
from .planner import Plan, Visit, build_plan
from .rozklad import Lesson, RozkladClient, RozkladError
from .runner import Runner, resolve_target
from .wiring import assistant_stack

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
PLATFORM_NAMES = {"meet": "Meet", "zoom": "Zoom", "other": "сайт"}

log = logging.getLogger("autopara")


def setup_logging(config: Config | None, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config is not None:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(config.data_dir / "autopara.log", encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%d.%m %H:%M:%S",
        handlers=handlers,
    )


def _client(config: Config) -> RozkladClient:
    return RozkladClient(config.schedule.base_url)


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


def cmd_schedule(args, config: Config) -> None:
    client = _client(config)
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    target = resolve_target(client, config)
    days = args.days if args.days is not None else config.schedule.days_ahead
    lessons = client.schedule(target, now.date(), now.date() + timedelta(days=days))
    who = target.student_name or target.group_name or config.schedule.group
    print(f"Розклад: {who}, {now:%d.%m} – {now.date() + timedelta(days=days):%d.%m}\n")
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
    with assistant_stack(config, clock) as (assistant, sink):
        joiner = Joiner(config.join, config.profile_dir, audio_sink=sink)
        runner = Runner(config, _client(config), joiner, clock=clock, assistant=assistant)
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
                sleep(5)
        except KeyboardInterrupt:
            pass
        finally:
            joiner.close()


def cmd_run(args, config: Config) -> None:
    from .joiner import Joiner

    # systemctl stop і kill шлють SIGTERM: перетворюємо його на звичайний вихід, щоб закрити браузер
    # і зняти заборону сну.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    clock = _clock(config)
    with assistant_stack(config, clock) as (assistant, sink):
        joiner = Joiner(config.join, config.profile_dir, audio_sink=sink)
        try:
            Runner(config, _client(config), joiner, clock=clock, assistant=assistant).run_forever()
        except KeyboardInterrupt:
            log.info("Зупинено")
        finally:
            joiner.close()


def cmd_telegram(args, config: Config) -> None:
    from .telegram import TelegramBot, TelegramError

    token = config.telegram.token
    if not token:
        print(
            "1. У Telegram напиши @BotFather → /newbot → придумай назву. Він дасть токен.\n"
            "2. Створи config.local.toml поруч з config.toml (він не потрапляє в git):\n\n"
            '   [telegram]\n   token = "123456:ABC..."\n\n'
            "3. Запусти `autopara telegram` ще раз."
        )
        return
    bot = TelegramBot(token, config.telegram.chat_id)
    try:
        if not config.telegram.chat_id:
            print("Напиши своєму боту будь-що в Telegram (наприклад, /start). Чекаю 2 хвилини...")
            for _ in range(4):
                for chat_id, _text in bot.updates(wait=30):
                    print(f"Знайшов! Додай у [telegram] у config.local.toml:\n   chat_id = {chat_id}")
                    bot.chat_id = chat_id
                    bot.send("👋 Знайшов тебе! Додай chat_id у config.local.toml — і я писатиму про пари сюди.")
                    return
            print("Не дочекався повідомлення. Спробуй ще раз.")
            return
        bot.send("👋 autopara на зв'язку. Напиши «допомога», щоб побачити команди.")
        print("Надіслав тестове повідомлення — перевір Telegram.")
    except TelegramError as e:
        print(f"Помилка: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_check(args, config: Config) -> None:
    """Перевіряє на твоєму комп'ютері все, що можна перевірити без пари."""
    import importlib.util
    import shutil

    from .brain import OllamaBrain
    from .ears import ears_available
    from .telegram import TelegramBot, TelegramError

    def line(ok: bool | None, text: str) -> None:
        print(f"{'✓' if ok else ('·' if ok is None else '✗')} {text}")

    try:
        target = resolve_target(_client(config), config)
        line(True, f"розклад: {target.student_name or target.group_name}")
    except RozkladError as e:
        line(False, f"розклад: {e}")
    browsers = [config.join.browser_path] if config.join.browser_path else ["google-chrome-stable", "google-chrome"]
    found = next((b for b in browsers if shutil.which(b) or (b.startswith("/") and Path(b).exists())), None)
    line(bool(found), f"браузер: {found or 'не знайдено (встанови google-chrome або вкажи browser_path)'}")
    line(config.profile_dir.exists(), f"профіль браузера: {config.profile_dir} (якщо немає — `autopara login`)")
    if config.join.zoom_mode == "app":
        line(bool(shutil.which("zoom")), "Zoom-клієнт" + ("" if shutil.which("zoom") else ": не знайдено (yay -S zoom)"))
    if config.telegram.token and config.telegram.chat_id:
        try:
            TelegramBot(config.telegram.token, config.telegram.chat_id).send("✓ autopara check")
            line(True, "Telegram: надіслав тестове повідомлення")
        except TelegramError as e:
            line(False, f"Telegram: {e}")
    else:
        line(None, "Telegram не налаштовано (`autopara telegram`)")
    if config.brain.enabled:
        problem = OllamaBrain(config.brain.url, config.brain.model).check()
        line(problem is None, f"мозок: {problem or config.brain.model}")
    else:
        line(None, "мозок вимкнено ([brain] enabled = false)")
    if config.ears.enabled:
        problem = ears_available()
        if problem is None and importlib.util.find_spec("faster_whisper") is None:
            problem = 'немає faster-whisper — pip install -e ".[ears]"'
        line(problem is None, f"вуха: {problem or 'готові'}")
    else:
        line(None, "вуха вимкнено ([ears] enabled = false)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopara",
        description="Автоматично заходить на пари ДУІКТ у Google Meet і Zoom за розкладом групи.",
    )
    parser.add_argument("-c", "--config", help="шлях до config.toml")
    parser.add_argument("-v", "--verbose", action="store_true", help="детальний лог")
    sub = parser.add_subparsers(dest="command", required=True, metavar="команда")

    find = sub.add_parser("find-group", help="знайти групу на сайті розкладу і показати її ID")
    find.add_argument("--students", action="store_true", help="показати список студентів групи")
    find.set_defaults(func=cmd_find_group)

    schedule = sub.add_parser("schedule", help="показати розклад і які посилання відкриються")
    schedule.add_argument("--days", type=int, help="на скільки днів уперед (за замовчуванням — з конфіга)")
    schedule.set_defaults(func=cmd_schedule)

    login = sub.add_parser("login", help="відкрити браузер, щоб один раз увійти в Google")
    login.set_defaults(func=cmd_login)

    join = sub.add_parser("join", help="зайти за посиланням прямо зараз (перевірка)")
    join.add_argument("url")
    join.add_argument("--minutes", type=int, default=90, help="скільки чекати, поки впустять")
    join.set_defaults(func=cmd_join)

    run = sub.add_parser("run", help="працювати у фоні й заходити на пари за розкладом")
    run.set_defaults(func=cmd_run)

    telegram = sub.add_parser("telegram", help="налаштувати Telegram-бота для сповіщень і команд")
    telegram.set_defaults(func=cmd_telegram)

    check = sub.add_parser("check", help="перевірити, що все налаштовано")
    check.set_defaults(func=cmd_check)
    return parser


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        setup_logging(config, args.verbose)
        args.func(args, config)
    except (ConfigError, RozkladError) as e:
        print(f"Помилка: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
