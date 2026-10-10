"""Підсумок тижня в Telegram — у неділю ввечері: що пройшли з кожного предмета, завдання й дедлайни, і що чекає
наступного тижня. Складається прямо з підсумків пар (без мовної моделі — швидко й без вигадок). Вимикається в
налаштуваннях («Telegram» → «Що бот робить» → «Підсумок тижня»).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable

from .helper import NOTHING_RE, Library, LessonDoc, _shorten

log = logging.getLogger(__name__)

SEND_AT = time(19, 0)  # неділя, з цієї години
LATEST = time(23, 59)  # пропустили неділю (ноутбук був вимкнений) — уже не надсилаємо, тиждень почався
CHECK_SECONDS = 600


def week_of(day: date) -> tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


def digest(docs: list[LessonDoc], now: datetime, upcoming: str = "") -> str:
    """Текст підсумку тижня, у який потрапляє ``now``."""
    monday, sunday = week_of(now.date())
    week = [doc for doc in docs if monday <= doc.day <= sunday]
    lines = [f"🗓 Підсумок тижня {monday:%d.%m}–{sunday:%d.%m}"]
    if not week:
        lines.append("Цього тижня записів пар немає — програма не була на парах або нічого не чула.")
    subjects: dict[str, list[LessonDoc]] = {}
    for doc in sorted(week, key=lambda d: (d.day, d.start)):
        subjects.setdefault(doc.subject, []).append(doc)
    tasks = []
    for subject, lessons in subjects.items():
        when = ", ".join(f"{doc.day:%d.%m} {doc.kind}".strip() for doc in lessons)
        lines.append(f"\n📚 {lessons[0].short} — {len(lessons)} {_lessons_word(len(lessons))} ({when})")
        for doc in lessons:
            about = doc.parts.get("Про що", "")
            if about and not NOTHING_RE.match(about):
                lines.append(f"  • {doc.day:%d.%m}: {_shorten(about, 160)}")
            task = doc.parts.get("Завдання й дедлайни", "")
            if task and not NOTHING_RE.match(task):
                tasks.append(f"• {doc.short} ({doc.day:%d.%m}): {_shorten(task, 220)}")
    if tasks:
        lines.append("\n📝 Завдання й дедлайни, що прозвучали:")
        lines.extend(tasks)
    if upcoming.strip():
        lines.append("\n📅 Наступного тижня:")
        lines.append(upcoming.strip())
    if week:
        lines.append("\n(з автоматичних підсумків пар — дати й номери краще звір у Classroom)")
    return "\n".join(lines)


def _lessons_word(count: int) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return "пара"
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return "пари"
    return "пар"


class WeeklyDigest:
    """Раз на тиждень, у неділю після 19:00, надсилає підсумок (пам'ятає, за який тиждень уже надіслав)."""

    def __init__(self, library: Library, send: Callable[[str], None], clock: Callable[[], datetime],
                 upcoming: Callable[[datetime], str], state: Path, enabled: Callable[[], bool] = lambda: True):
        self.library = library
        self.send = send
        self.clock = clock
        self.upcoming = upcoming
        self.state = state
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="weekly", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception:  # noqa: BLE001 — підсумок тижня не має валити програму
                log.warning("Підсумок тижня не вийшов", exc_info=True)
            self._stop.wait(CHECK_SECONDS)

    def _sent(self) -> str:
        try:
            return json.loads(self.state.read_text(encoding="utf-8")).get("week", "")
        except (OSError, ValueError):
            return ""

    def step(self) -> bool:
        """Надіслати, якщо час (неділя ввечері) і за цей тиждень ще ні. True — надіслали."""
        now = self.clock()
        if not self.enabled() or now.weekday() != 6 or not (SEND_AT <= now.time() <= LATEST):
            return False
        week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        if self._sent() == week:
            return False
        self.library.refresh()
        try:
            upcoming = self.upcoming(now + timedelta(hours=12))  # з ранку понеділка
        except Exception:  # noqa: BLE001
            upcoming = ""
        self.send(digest(self.library.docs, now, upcoming))
        try:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            self.state.write_text(json.dumps({"week": week}), encoding="utf-8")
        except OSError as e:
            log.debug("Не вдалося запам'ятати підсумок тижня: %s", e)
        log.info("Надіслав підсумок тижня %s", week)
        return True
