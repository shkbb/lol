"""Фоновий цикл: оновлює розклад, вчасно заходить на пари й виходить з них."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from .assistant import Action, Assistant
from .config import Config
from .leaving import LeaveDecider, Observation
from .planner import Plan, Visit, build_plan
from .power import SleepGuard
from .rozklad import RozkladClient, RozkladError, Target
from .sources import resolve_target
from .session import JoinError, Session

log = logging.getLogger(__name__)

# Якщо до кінця пари лишилось менше — вже не заходимо.
TOO_LATE = timedelta(minutes=5)
RETRY_JOIN_AFTER = timedelta(minutes=2)
MAX_JOIN_ATTEMPTS = 3
RETRY_REFRESH_AFTER = timedelta(minutes=5)
# Classroom відкриваємо лише тоді, коли до найближчої пари ще стільки часу (здача може тривати кілька хвилин).
STUDY_QUIET_BEFORE_CLASS = timedelta(minutes=15)


class Runner:
    def __init__(
        self,
        config: Config,
        client: RozkladClient,
        joiner,
        clock: Callable[[], datetime] | None = None,
        sleep_guard: SleepGuard | None = None,
        app_audio=None,
        assistant: Assistant | None = None,
        study=None,
    ):
        self.config = config
        self.assistant = assistant
        self.study = study  # StudyManager: Classroom між парами
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
        self.sleep_guard = sleep_guard or SleepGuard()
        self.app_audio = app_audio  # вуха для Zoom-клієнта: start() на початку пари, stop() після
        self.last_seen: dict[str, Observation] = {}
        self.extra_visits: list[Visit] = []  # тестові входи з вікна програми
        self.last_error = ""
        self._warned: set[str] = set()

    def notify(self, text: str, urgent: bool = False, routine: bool = False) -> None:
        """``routine`` — звичайне «зайшов/вийшов»: його можна вимкнути в налаштуваннях."""
        if routine and not self.config.telegram.joins:
            log.info(text)
        elif self.assistant is not None:
            self.assistant.notify(text, urgent=urgent)
        else:
            log.info(text)

    # Розклад

    def refresh(self, now: datetime) -> None:
        if self.target is None:
            self.target = resolve_target(self.client, self.config)
            who = self.target.student_name or self.target.group_name or self.config.schedule.group
            log.info("Тягну розклад для: %s", who)
            self._load_classmates()
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

    def _load_classmates(self) -> None:
        """Список групи з сайту розкладу — щоб помічник упізнавав перекличку голосом (і номер для варіантів)."""
        if self.assistant is None and self.study is None:
            return
        target = self.target
        try:
            students = self.client.students(target.faculty_id, target.course, target.group_id)
        except (RozkladError, AttributeError) as e:
            log.debug("Не вдалося взяти список групи: %s", e)
            return
        names = [student.name for student in students]
        if self.assistant is not None:
            self.assistant.set_classmates(names)
        if self.study is not None:
            self.study.set_classmates(names)
        log.info("Список групи: %d студентів", len(students))

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
                self.last_error = ""
            except RozkladError as e:
                log.error("Не вдалося оновити розклад: %s. Спробую ще раз за 5 хв", e)
                self.last_error = str(e)
                self.next_refresh = now + RETRY_REFRESH_AFTER

        for key, session in list(self.active.items()):
            if not self.joiner.alive(session):
                log.info("Вкладку «%s» закрито вручну", session.visit.title)
                self._forget(key)
                continue
            seen = self._look(key, session, now)
            if self.config.join.auto_leave and (reason := self._leave_reason(session, seen, now)):
                self._leave(key, reason)

        self.extra_visits = [visit for visit in self.extra_visits if visit.leave_at > now]
        for visit in self.plan.visits + self.extra_visits:
            if self._due(visit, now):
                self._join(visit, now)

        if self.assistant is not None:
            self.assistant.tick(now)
            for action in self.assistant.take_actions():
                self._act(action, now)

        self.sleep_guard.hold(self._lessons_left_today(now))

        if self.study is not None and self._free_for_study(now):
            self.study.browser_tick(now)

    def _free_for_study(self, now: datetime) -> bool:
        """Classroom — лише коли не на парі й найближча пара не скоро: браузер має бути вільний для входу."""
        if self.active:
            return False
        return not any(
            visit.key not in self.finished and visit.join_at - STUDY_QUIET_BEFORE_CLASS <= now < visit.leave_at
            for visit in self.plan.visits + self.extra_visits
        )

    def _look(self, key: str, session: Session, now: datetime) -> Observation:
        """Очі: що зараз у зустрічі (лише для вкладок браузера)."""
        if session.page is None:
            return Observation()
        seen = self.joiner.observe(session)
        self.last_seen[key] = seen
        if self.assistant is not None:
            self.assistant.on_participants(key, seen.participants)
            self.assistant.on_chat(key, self.joiner.read_chat(session), now)
        return seen

    def _leave_reason(self, session: Session, seen: Observation, now: datetime) -> str | None:
        if session.decider is not None:
            return session.decider.update(now, seen)
        return "пара скінчилась за розкладом" if now >= session.leave_at else None

    def _leave(self, key: str, reason: str) -> None:
        session = self.active[key]
        log.info("Виходжу з «%s»: %s", session.visit.title, reason)
        self.joiner.leave(session)
        self._forget(key)
        self.notify(f"👋 Вийшов з «{session.visit.title}»: {reason}", routine=True)

    def _forget(self, key: str) -> None:
        session = self.active.pop(key, None)
        if session is not None and session.page is None and self.app_audio is not None:
            self.app_audio.stop()
        self.last_seen.pop(key, None)
        if self.assistant is not None:
            self.assistant.detach(key, self.clock())

    def _act(self, action: Action, now: datetime) -> None:
        """Руки: виконуємо дії помічника тут, у головному потоці, — браузером керує лише він."""
        session = self.active.get(action.key)
        if session is None:
            return
        if action.kind == "chat":
            ok = self.joiner.send_chat(session, action.text)
            self.assistant.chat_done(action.key, action.text, ok, now, action.reason)
        elif action.kind == "leave":
            self._leave(action.key, action.reason)
        elif action.kind == "screenshot":
            if shot := self.joiner.screenshot(session):
                self.assistant.photo(shot, f"🖥 «{session.visit.title}», {now:%H:%M}")
            else:
                self.notify("Не вдалося зробити скріншот")

    def _lessons_left_today(self, now: datetime) -> bool:
        if self.active:
            return True
        return any(
            visit.join_at.date() == now.date() and visit.leave_at > now and visit.key not in self.finished
            for visit in self.plan.visits
        )

    def _window(self, visit: Visit) -> tuple[datetime, datetime]:
        return visit.lessons[0].starts_at(self.tz), visit.lessons[-1].ends_at(self.tz)

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
                if session.decider is not None:
                    session.decider.next_lesson(*self._window(visit))
                if self.assistant is not None:
                    self.assistant.next_lesson(session.visit.key, visit.title, *self._window(visit), now=now)
                self.finished.add(visit.key)
                log.info("Лишаюсь у тій самій зустрічі на «%s»", visit.title)
                return
        # Почалася наступна пара за іншим посиланням — у двох дзвінках одночасно не сидимо.
        if self.config.join.auto_leave:
            for key in list(self.active):
                self._leave(key, f"починається наступна пара «{visit.title}»")
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
                self.notify(f"❌ Не вдалося зайти на «{visit.title}»: {e}. Зайди сам: {visit.url}", urgent=True)
                self.finished.add(visit.key)
            return
        start, end = self._window(visit)
        if session.page is None:
            self.notify(f"✅ Відкрив «{visit.title}» у Zoom-клієнті (мікрофон і камера — за налаштуваннями Zoom)", routine=True)
            if self.app_audio is not None:
                self.app_audio.start()
        else:
            self.notify(f"✅ Зайшов на «{visit.title}», мікрофон і камера вимкнені", routine=True)
            if self.config.join.smart_leave:
                session.decider = LeaveDecider(start, end, self.config.join)
        if self.assistant is not None:
            # Писати в чат помічник уміє лише в Meet; в інших — підкаже тобі, що написати.
            can_chat = session.page is not None and visit.platform == "meet"
            self.assistant.attach(visit.key, visit.title, visit.lessons[0].teacher, start, end, now=now, can_chat=can_chat)
        self.finished.add(visit.key)
        self.active[visit.key] = session
        # Одразу дивимось у зустріч: те, що вже є в чаті, — історія, а все наступне — нове.
        self._look(visit.key, session, now)

    def run_forever(self, poll_seconds: float = 20) -> None:
        log.info("autopara запущено. Не вимикай комп'ютер і не давай йому заснути під час пар.")
        try:
            while True:
                self.tick()
                self.joiner.idle(poll_seconds)
        finally:
            self.sleep_guard.release()
