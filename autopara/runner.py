"""Фоновий цикл: оновлює розклад, вчасно заходить на пари й виходить з них."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .assistant import Action, Assistant
from .config import Config
from .leaving import LeaveDecider, Observation
from .planner import Plan, Visit, build_plan
from .power import SleepGuard
from .rozklad import Lesson, RozkladClient, RozkladError, Target
from .sources import resolve_target
from .session import JoinError, Session

log = logging.getLogger(__name__)

SLEEP_GUARD_AHEAD = timedelta(hours=24)  # за скільки до пари вже не даємо комп'ютеру заснути

# Якщо до кінця пари лишилось менше — вже не заходимо.
TOO_LATE = timedelta(minutes=5)
RETRY_JOIN_AFTER = timedelta(minutes=2)
MAX_JOIN_ATTEMPTS = 3
RETRY_REFRESH_AFTER = timedelta(minutes=5)


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
        visited_file: Path | None = None,
        schedule_cache: Path | None = None,
    ):
        self.config = config
        self.assistant = assistant
        self.client = client
        self.joiner = joiner
        self.tz = ZoneInfo(config.timezone)
        self.clock = clock or (lambda: datetime.now(self.tz))
        self.plan = Plan()
        self.target: Target | None = None
        self.active: dict[str, Session] = {}  # visit.key -> Session
        # Пари, на які вже ходили (чи на які зайти не вдалося). Зберігаються у файл, щоб після перезапуску програма
        # не зайшла вдруге на пару, з якої вже вийшла (наприклад, «зустріч завершилась» раніше за розклад).
        self.visited_file = visited_file
        # Останній розклад із сайту — на диску: якщо сайт розкладу ляже (наживо: не відповідав DNS), а програму
        # саме перезапустили, вона однаково зайде на пари за збереженим розкладом.
        self.schedule_cache = schedule_cache
        self.finished: set[str] = self._load_visited()
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
        self._save_schedule(lessons)
        self._use(lessons, now)

    def _use(self, lessons: list[Lesson], now: datetime) -> None:
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

    def _schedule_owner(self) -> str:
        return f"{self.config.schedule.university}|{self.config.schedule.group}|{self.config.schedule.student}"

    def _save_schedule(self, lessons: list[Lesson]) -> None:
        if self.schedule_cache is None:
            return
        rows = []
        for lesson in lessons:
            row = asdict(lesson)
            row.update(day=lesson.day.isoformat(), start=lesson.start.isoformat(), end=lesson.end.isoformat())
            rows.append(row)
        try:
            self.schedule_cache.parent.mkdir(parents=True, exist_ok=True)
            self.schedule_cache.write_text(json.dumps({"owner": self._schedule_owner(), "lessons": rows},
                                                      ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log.debug("Не вдалося зберегти розклад: %s", e)

    def _cached_lessons(self) -> list[Lesson]:
        if self.schedule_cache is None:
            return []
        try:
            data = json.loads(self.schedule_cache.read_text(encoding="utf-8"))
            if data.get("owner") != self._schedule_owner():
                return []  # розклад іншої групи (налаштування змінили)
            return [
                Lesson(**{**row, "day": date.fromisoformat(row["day"]), "start": time.fromisoformat(row["start"]),
                          "end": time.fromisoformat(row["end"])})
                for row in data.get("lessons", [])
            ]
        except (OSError, ValueError, TypeError, KeyError):
            return []

    def _load_classmates(self) -> None:
        """Список групи з сайту розкладу — щоб помічник упізнавав перекличку голосом."""
        if self.assistant is None:
            return
        target = self.target
        try:
            students = self.client.students(target.faculty_id, target.course, target.group_id)
        except (RozkladError, AttributeError) as e:
            log.debug("Не вдалося взяти список групи: %s", e)
            return
        self.assistant.set_classmates([student.name for student in students])
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
                if not self.plan.visits and (cached := self._cached_lessons()):
                    log.warning("Сайт розкладу недоступний — заходжу на пари за збереженим розкладом")
                    self._use(cached, now)

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

        self.sleep_guard.hold(self._lesson_soon(now))

    def finish_all(self) -> None:
        """Програму закривають посеред пари — запис пари однаково має зберегтися."""
        for key in list(self.active):
            self._forget(key)

    def act_now(self, now: datetime | None = None) -> None:
        """Дії помічника («+» на перекличці, «напиши …», кнопка в Telegram) — одразу, а не на наступному
        кроці (раз на 20 с): на перекличці відповідь, що прийшла через півхвилини, нікому не потрібна."""
        if self.assistant is None:
            return
        now = now or self.clock()
        for action in self.assistant.take_actions():
            self._act(action, now)

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
        self._save_visited()
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

    def today_note(self, now: datetime) -> str:
        """Пари на сьогодні з часом і станом — щоб мозок не плутав, яка пара остання, а яка далі."""
        visits = sorted((v for v in list(self.plan.visits) if v.join_at.date() == now.date()), key=lambda v: v.join_at)
        if not visits:
            return "Сьогодні пар за розкладом більше немає."
        lines = ["Пари сьогодні за розкладом:"]
        for visit in visits:
            start, end = self._window(visit)
            if visit.key in self.active:
                state = "іде зараз, програма на ній"
            elif visit.key in self.finished or end <= now:
                state = "уже була"
            else:
                state = "ще буде"
            lines.append(f"- {start:%H:%M}–{end:%H:%M} «{visit.title}» — {state}")
        return "\n".join(lines)

    def quiet(self, now: datetime, ahead: timedelta = timedelta(minutes=15)) -> bool:
        """Пари немає й найближчі ``ahead`` не буде — можна братися за важку фонову роботу (уточнення
        розшифровки). Викликається з іншого потоку, тож будь-яка несподіванка — «ні»."""
        try:
            if self.active:
                return False
            return not any(
                visit.join_at - ahead <= now < visit.leave_at and visit.key not in self.finished
                for visit in list(self.plan.visits)
            )
        except Exception:  # noqa: BLE001
            return False

    def _lesson_soon(self, now: datetime) -> bool:
        """Чи не можна зараз засинати: іде пара або наступна — менш ніж за добу. Тоді ноутбук, залишений на ніч,
        уранці сам зайде на пару, а на вихідних (коли до пари більше доби) може спати."""
        if self.active:
            return True
        return any(
            visit.leave_at > now and visit.join_at - now <= SLEEP_GUARD_AHEAD and visit.key not in self.finished
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
                self._save_visited()
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

    def _save_visited(self) -> None:
        """Записати пари, з яких уже вийшли (чи зайти на які не вдалося). Ті, що йдуть зараз, — ні: якщо програму
        перезапустять посеред пари, вона має зайти на неї знову."""
        if self.visited_file is None:
            return
        # Ключ пари починається з дати («2026-10-05|11:10:00|…») — старіші за вчора не тримаємо.
        keep_from = f"{self.clock().date() - timedelta(days=1)}"
        keys = sorted(k for k in self.finished - set(self.active) if k[:10] >= keep_from)
        try:
            self.visited_file.parent.mkdir(parents=True, exist_ok=True)
            self.visited_file.write_text(json.dumps(keys, ensure_ascii=False), encoding="utf-8")
        except OSError as e:
            log.warning("Не вдалося запам'ятати відвідану пару: %s", e)

    def _load_visited(self) -> set[str]:
        if self.visited_file is None:
            return set()
        try:
            return set(json.loads(self.visited_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return set()

    def run_forever(self, poll_seconds: float = 20) -> None:
        log.info("autopara запущено. Не вимикай комп'ютер і не давай йому заснути під час пар.")
        try:
            while True:
                self.tick()
                self.joiner.idle(poll_seconds)
        finally:
            self.sleep_guard.release()
