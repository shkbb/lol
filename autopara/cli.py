"""Командний рядок: ``autopara <команда>``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config, ConfigError, load_config
from .links import platform_of, resolve
from .planner import Visit, build_plan
from .rozklad import Lesson, RozkladClient, RozkladError
from .runner import Runner, resolve_target
from .session import JoinError

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


def cmd_login(args, config: Config) -> None:
    from .joiner import Browser

    browser = Browser(
        config.profile_dir, config.join.browser_channel, mute_sound=False, executable_path=config.join.browser_path
    )
    try:
        browser.new_page().goto("https://accounts.google.com/")
        if config.join.zoom_mode == "browser":
            browser.new_page().goto("https://zoom.us/signin")
        input(
            "У вікні браузера увійди в Google-акаунт, з яким ходиш на пари "
            "(університетський, якщо пари там).\nКоли закінчиш — натисни Enter тут... "
        )
    finally:
        browser.close()
    print(f"Готово. Профіль браузера збережено в {config.profile_dir}")


def cmd_join(args, config: Config) -> None:
    from .joiner import Joiner

    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    leave_at = now + timedelta(minutes=args.minutes)
    lesson = Lesson(now.date(), now.time(), leave_at.time(), "Тестовий вхід")
    visit = Visit(args.url, platform_of(args.url), [lesson], now, leave_at)
    joiner = Joiner(config.join, config.profile_dir)
    try:
        session = joiner.join(visit)
        if session.page is not None:
            session.page.wait_for_timeout(3000)  # лічильник учасників з'являється не одразу
            seen = joiner.observe(session)
            if seen.participants:
                print(f"Бачу учасників: {seen.participants} — розумний вихід працюватиме.")
            else:
                print("Не бачу лічильника учасників — з цієї зустрічі програма виходитиме за розкладом.")
        input("Зайшов. Перевір мікрофон/камеру у вікні. Натисни Enter, щоб вийти... ")
    except JoinError as e:
        print(f"Не вдалося зайти: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        joiner.close()


def cmd_run(args, config: Config) -> None:
    from .joiner import Joiner

    # systemctl stop і kill шлють SIGTERM: перетворюємо його на звичайний вихід, щоб закрити браузер
    # і зняти заборону сну.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    joiner = Joiner(config.join, config.profile_dir)
    try:
        Runner(config, _client(config), joiner).run_forever()
    except KeyboardInterrupt:
        log.info("Зупинено")
    finally:
        joiner.close()


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
