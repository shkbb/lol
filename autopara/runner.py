"""Фоновий цикл: оновлює розклад, вчасно заходить на пари й виходить з них."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from time import sleep
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config
from .planner import Plan, Visit, build_plan
from .rozklad import RozkladClient, RozkladError, Target
from .session import JoinError, Session

log = logging.getLogger(__name__)

# Якщо до кінця пари лишилось менше — вже не заходимо.
TOO_LATE = timedelta(minutes=5)
RETRY_JOIN_AFTER = timedelta(minutes=2)
MAX_JOIN_ATTEMPTS = 3
RETRY_REFRESH_AFTER = timedelta(minutes=5)


def resolve_target(client: RozkladClient, config: Config) -> Target:
    s = config.schedule
    if s.faculty_id and s.course and s.group_id:
        target = Target(s.faculty_id, s.course, s.group_id, group_name=s.group)
    else:
        target = client.find_group(s.group, s.faculty_id, s.course)
    if s.student:
        target = client.resolve_student(target, s.student)
    return target


class Runner:
    def __init__(self, config: Config, client: RozkladClient, joiner, clock: Callable[[], datetime] | None = None):
        self.config = config
        self.client = client
        self.joiner = joiner
        self.tz = ZoneInfo(config.timezone)
        self.clock = clock or (lambda: datetime.now(self.tz))
        self.plan = Plan()
        self.target: Target | None = None
        self.active: dict[str, Session] = {}  # visit.key -> Session
        self.finished: set[str] = set()
        self.attempts: dict[str, int] = {}
        self.retry_at: dict[str, datetime] = {}
        self.next_refresh: datetime | None = None
        self._warned: set[str] = set()

    # Розклад

    def refresh(self, now: datetime) -> None:
        if self.target is None:
            self.target = resolve_target(self.client, self.config)
            who = self.target.student_name or self.target.group_name or self.config.schedule.group
            log.info("Тягну розклад для: %s", who)
        today = now.date()
        lessons = self.client.schedule(self.target, today, today + timedelta(days=self.config.schedule.days_ahead))
        self.plan = build_plan(
            lessons, self.config.links, self.config.join, self.tz, now, self.config.schedule.use_schedule_links
        )
        for message in self.plan.conflicts:
            self._warn_once(message, message)
        for lesson in self.plan.unlinked:
            self._warn_once(
                f"{lesson.day}|{lesson.start}|{lesson.subject}",
                f"Немає посилання для «{lesson.title}» {lesson.day:%d.%m} о {lesson.start:%H:%M} — "
                "додай його в [[links]] у config.toml",
            )
        upcoming = [v for v in self.plan.visits if v.key not in self.finished]
        if upcoming:
            v = upcoming[0]
            log.info("Розклад оновлено. Найближча пара: %s — зайду %s", v.title, v.join_at.strftime("%d.%m о %H:%M"))
        else:
            log.info("Розклад оновлено. Найближчими %d днями пар з посиланнями немає", self.config.schedule.days_ahead)

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message)

    # Один крок циклу

    def tick(self, now: datetime | None = None) -> None:
        now = now or self.clock()
        if self.next_refresh is None or now >= self.next_refresh:
            try:
                self.refresh(now)
                self.next_refresh = now + timedelta(minutes=self.config.schedule.refresh_minutes)
            except RozkladError as e:
                log.error("Не вдалося оновити розклад: %s. Спробую ще раз за 5 хв", e)
                self.next_refresh = now + RETRY_REFRESH_AFTER

        for key, session in list(self.active.items()):
            if not self.joiner.alive(session):
                log.info("Вкладку «%s» закрито вручну", session.visit.title)
                del self.active[key]
            elif self.config.join.auto_leave and now >= session.leave_at:
                log.info("Пара «%s» скінчилась — виходжу", session.visit.title)
                self.joiner.leave(session)
                del self.active[key]

        for visit in self.plan.visits:
            if self._due(visit, now):
                self._join(visit, now)

    def _due(self, visit: Visit, now: datetime) -> bool:
        if visit.key in self.finished or visit.key in self.active:
            return False
        if now < self.retry_at.get(visit.key, visit.join_at):
            return False
        return now < visit.leave_at - TOO_LATE

    def _join(self, visit: Visit, now: datetime) -> None:
        # Та сама зустріч уже відкрита (наприклад, друга пара поспіль) — просто продовжуємо її.
        for session in self.active.values():
            if session.url == visit.url:
                session.leave_at = max(session.leave_at, visit.leave_at)
                self.finished.add(visit.key)
                return
        log.info("Заходжу на «%s» (%s): %s", visit.title, visit.platform, visit.url)
        try:
            session = self.joiner.join(visit)
        except JoinError as e:
            attempt = self.attempts.get(visit.key, 0) + 1
            self.attempts[visit.key] = attempt
            if e.retry and attempt < MAX_JOIN_ATTEMPTS:
                log.error("Не вдалося зайти на «%s»: %s. Спробую ще раз за 2 хв", visit.title, e)
                self.retry_at[visit.key] = now + RETRY_JOIN_AFTER
            else:
                log.error("Не вдалося зайти на «%s»: %s", visit.title, e)
                self.finished.add(visit.key)
            return
        if session.page is None:
            log.info("Відкрив «%s» у Zoom-клієнті (мікрофон і камера — за налаштуваннями Zoom)", visit.title)
        else:
            log.info("Зайшов на «%s»", visit.title)
        self.finished.add(visit.key)
        self.active[visit.key] = session

    def run_forever(self, poll_seconds: float = 20) -> None:
        log.info("autopara запущено. Не вимикай комп'ютер і не давай йому заснути під час пар.")
        while True:
            self.tick()
            sleep(poll_seconds)
