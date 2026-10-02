"""Навчання: завдання з Google Classroom робить агент, а здає програма — лише після твого «так».

Шлях кожного завдання:

    нове → агент працює → (перевірка проти умови, доробка) → готово → твоє «✅ Так» → здано

- Тобі приходять лише ПОВНІСТЮ готові роботи: окремий прохід агента перевіряє результат пункт за
  пунктом проти умови й доробляє. Не вдалося довести до кінця за кілька спроб — пишемо чесно,
  що саме заважає, а не надсилаємо напівготове.
- Здає програма тільки після твого «так» (кнопкою в Telegram або у вікні програми). Перший раз —
  пробна здача: усе, крім натискання «Здати», зі скріном, щоб ти побачив, що все на місці.
- Тести (Google Forms) і захисти агент не чіпає.
- До кожної роботи — шпаргалка до захисту.
- Після пари агент робить детальну вижимку лекції; вижимки складаються в конспект курсу, і агент
  бачить його, коли виконує завдання з цього предмета (зокрема побажання викладача).

Браузер (Classroom) — лише в потоці контролера і лише між парами (``browser_tick``). Агент
працює в окремому фоновому потоці, бо одна робота може тривати десятки хвилин.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .agent import Agent, AgentError
from .classroom import Assignment, Attachment, Classroom, ClassroomError, download
from .config import Config

log = logging.getLogger(__name__)

STATUSES = {
    "new": "нове",
    "working": "агент працює",
    "waiting": "чекає, поки агент буде доступний",
    "stuck": "не вдалося довести до кінця",
    "ready": "готово — чекає на твоє «так»",
    "approved": "здаю",
    "submitted": "здано",
    "returning": "забираю з Classroom",
    "declined": "не здавати",
    "gone": "уже не в списку незданих (здано без мене чи прибрано)",
    "skipped": "не для агента",
    "failed": "не вдалося здати",
}
# Не робота для агента: тести, захисти, контрольні.
# Лише за початком назви завдання: «Тест 2», «Захист ЛР», «МКР 1». Не за текстом сторінки — там є назва
# курсу («Якість та тестування ПЗ»), і не за серединою назви («ЛР 3. Unit-тести» — звичайна лаба).
NOT_FOR_AGENT_RE = re.compile(
    r"^\W*(тест|тести|захист\w*|контрольн\w*|мкр|модульн\w*|екзамен\w*|залік\w*|опитуван\w*|quiz|exam|test)\b", re.I
)
SHORT_ANSWERS = {"так", "yes", "ок", "ok", "+", "здавай", "здати", "давай", "ні", "no", "не треба"}
STATUS_RE = re.compile(r"STATUS:\s*(COMPLETE|INCOMPLETE)\s*:?\s*(.*)", re.I)
MAX_NOTES_CHARS = 14_000
RETRY_CLASSROOM_AFTER = timedelta(minutes=20)
MAX_PAGE_FAILURES = 3
MONTHS = {
    "січ": 1, "лют": 2, "бер": 3, "квіт": 4, "трав": 5, "черв": 6,
    "лип": 7, "серп": 8, "вер": 9, "жовт": 10, "лист": 11, "груд": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = "|".join(MONTHS)
DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTH})[a-zа-яіїєґ]*\.?", re.I)
MONTH_DAY_RE = re.compile(rf"\b({_MONTH})[a-z]*\.?\s+(\d{{1,2}})\b", re.I)
STUDY_HELP = (
    "завдання — що зараз із завданнями з Classroom\n"
    "↩️ відповідь на повідомлення про завдання — переробити його з твоїм зауваженням"
)

WORK_INSTRUCTION = (
    "Прочитай TASK.md у поточній теці й виконай завдання повністю, сам, від початку до кінця. "
    "Усе, що треба здати, поклади в теку out/. Наприкінці онови RESULT.md і DEFENSE.md, як описано в TASK.md."
)
CHECK_INSTRUCTION = (
    "Ти — суворий перевіряльник. Прочитай TASK.md і перевір усе в теці out/ пункт за пунктом проти умови "
    "завдання: чи виконано кожну вимогу, чи справжні скріншоти й результати, чи правильний варіант, чи є "
    "титульна сторінка й висновок, чи файли відкриваються. Усе, чого бракує чи що зроблено погано, — "
    "виправ сам. Потім допиши в кінець RESULT.md окремим рядком: «STATUS: COMPLETE», якщо робота повністю "
    "готова до здачі, або «STATUS: INCOMPLETE: <що саме і чому не вдалося>»."
)
LECTURE_INSTRUCTION = (
    "Прочитай LECTURE.md у поточній теці й виконай те, що там написано. Результат запиши в SUMMARY.md."
)


@dataclass
class StudyTask:
    key: str
    number: int  # короткий номер — для кнопок і команд
    course: str
    title: str
    listing: str = ""
    status: str = "new"
    folder: str = ""
    files: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    feedback: list[str] = field(default_factory=list)
    attempts: int = 0
    messages: list[int] = field(default_factory=list)  # наші повідомлення в Telegram про це завдання
    submitted: str = ""
    updated: str = ""
    due: str = ""  # термін зі списку завдань (ISO), якщо є
    version: int = 0  # яка за ліком готова версія — «так» стосується саме тієї, яку ти бачив

    @property
    def label(self) -> str:
        return f"№{self.number} «{self.title}»" + (f" ({self.course})" if self.course else "")

    def assignment(self) -> Assignment:
        course_id, item_id = self.key.split("/", 1)
        return Assignment(course_id, item_id, self.title, self.course, self.listing)


def due_of(listing: str, now: datetime) -> datetime | None:
    """Термін зі списку завдань: «До 2 жовт.», «Due Oct 2», «Завтра»… Без терміну — None."""
    low = listing.casefold()
    if "сьогодні" in low or "today" in low:
        return now.replace(hour=23, minute=59)
    if "завтра" in low or "tomorrow" in low:
        return (now + timedelta(days=1)).replace(hour=23, minute=59)
    if match := DAY_MONTH_RE.search(low):
        day, month = int(match.group(1)), MONTHS[match.group(2)]
    elif match := MONTH_DAY_RE.search(low):
        month, day = MONTHS[match.group(1)], int(match.group(2))
    else:
        return None
    try:
        due = now.replace(month=month, day=day, hour=23, minute=59)
    except ValueError:
        return None
    if due - now > timedelta(days=200):  # «12 груд.» у вересні — цього року, а в січні — минулого
        due = due.replace(year=due.year - 1)
    elif now - due > timedelta(days=165):
        due = due.replace(year=due.year + 1)
    return due


def safe_name(text: str, limit: int = 60) -> str:
    return re.sub(r"[^\w\-.]+", "_", text, flags=re.UNICODE).strip("_")[:limit] or "task"


def short_name(full_name: str) -> str:
    """«Ляшко Денис Олегович» → «Ляшко_Д.О.» — як ти називаєш файли звітів."""
    parts = full_name.split()
    if not parts:
        return "Студент"
    return parts[0] + "_" + "".join(f"{p[0]}." for p in parts[1:3])


def subject_of(title: str) -> str:
    """«Бази даних (Лк)» → «Бази даних»."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()


def _stems(text: str) -> set[str]:
    words = re.findall(r"[^\W\d_]+", text.casefold())
    return {word[:5] for word in words if len(word) > 3}


def _abbreviation(text: str) -> str:
    return "".join(word[0] for word in re.findall(r"[^\W\d_]+", text) if len(word) > 2).upper()


def match_subject(course: str, subjects: list[str]) -> str | None:
    """Який предмет з розкладу — це курс Classroom («ЯТПЗ - 31/2026» ↔ «Якість та тестування ПЗ»)."""
    best, best_score = None, 0.0
    course_stems = _stems(course)
    course_upper = course.upper()
    for subject in subjects:
        score = len(course_stems & _stems(subject))
        abbreviation = _abbreviation(subject)
        if len(abbreviation) >= 3 and abbreviation in course_upper:
            score += 3
        if score > best_score:
            best, best_score = subject, score
    return best if best_score > 0 else None


class StudyManager:
    def __init__(self, config: Config, agent: Agent, notifier, clock: Callable[[], datetime], folder: Path):
        self.config = config
        self.agent = agent
        self.notifier = notifier  # Notifier: текст, кнопки, документи, фото
        self.clock = clock
        self.folder = folder
        self.classroom: Classroom | None = None  # під'єднує контролер (браузер програми)
        self.list_number = config.study.list_number
        self._lock = threading.RLock()
        self._jobs: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="study", daemon=True)
        self._cookies: list[dict] = []
        self._retry_at: datetime | None = None  # після помилки Classroom не смикаємо щохвилини
        self._page_failures: dict[str, int] = {}
        self._next_agent_check: datetime | None = None
        self._current: StudyTask | None = None  # з яким завданням зараз працює браузер
        self._restarted: list[int] = []
        self._missing: dict[str, int] = {}  # скільки перевірок поспіль завдання немає серед незданих
        self._awaiting_feedback: dict[int, str] = {}  # повідомлення «напиши, що змінити» → завдання
        self.state = self._load()

    # --- Стан ----------------------------------------------------------------------

    @property
    def state_file(self) -> Path:
        return self.folder / "tasks.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            data["tasks"] = [StudyTask(**task) for task in data.get("tasks", [])]
            return data
        except (OSError, ValueError, TypeError):
            return {"tasks": [], "next_number": 1, "last_check": "", "real_submissions": 0}

    def _save(self) -> None:
        with self._lock:
            data = {**self.state, "tasks": [asdict(task) for task in self.state["tasks"]]}
            self.folder.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(self.state_file)

    @property
    def tasks(self) -> list[StudyTask]:
        return self.state["tasks"]

    def task(self, number: int) -> StudyTask | None:
        return next((task for task in self.tasks if task.number == number), None)

    def _by_key(self, key: str) -> StudyTask | None:
        return next((task for task in self.tasks if task.key == key), None)

    def _set(self, task: StudyTask, status: str, **changes) -> None:
        with self._lock:
            task.status = status
            for name, value in changes.items():
                setattr(task, name, value)
            task.updated = self.clock().isoformat(timespec="minutes")
        self._save()

    # --- Життєвий цикл ---------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()
        # Те, що агент не доробив через перезапуск програми, доробимо — щойно будуть кукі для матеріалів.
        self._restarted = [task.number for task in self.tasks if task.status == "working"]

    def stop(self) -> None:
        """Програму закривають чи перезапускають: агента зупиняємо, недороблене доробимо після старту."""
        self._stop.set()
        self._jobs.put(None)
        self.agent.cancel()

    def set_classmates(self, names: list[str]) -> None:
        """Номер у списку групи — для варіантів «згідно списку групи»."""
        if self.list_number:
            return
        me = (self.config.study.full_name or self.config.me.name).casefold().split()
        for index, name in enumerate(names, 1):
            words = name.casefold().split()
            if me and words[: len(me[:2])] == me[:2]:
                self.list_number = index
                log.info("Навчання: твій номер у списку групи — %d (для варіантів)", index)
                return

    # --- Браузер (потік контролера, між парами) ------------------------------------

    def browser_tick(self, now: datetime) -> None:
        """Крок між парами (потік контролера): Classroom — по одній дії за раз, щоб автовхід лишався чуйним."""
        self._resume_waiting(now)
        if self.classroom is None or (self._retry_at is not None and now < self._retry_at):
            return
        self._current = None
        try:
            if self._restarted:
                self._cookies = self._cookies or self.classroom.cookies()
                for number in self._restarted:
                    self._jobs.put(("work", number))
                self._restarted = []
                return
            self._browser_step(now)
        except ClassroomError as e:
            self._retry_at = now + RETRY_CLASSROOM_AFTER
            if self.state.get("last_error") != str(e):
                self._tell(f"⚠️ Classroom: {e}")
            self.state["last_error"] = str(e)
            self._save()
        except Exception as e:  # noqa: BLE001 — Classroom не має валити автовхід на пари
            log.exception("Classroom: помилка")
            self._retry_at = now + RETRY_CLASSROOM_AFTER
            if self._current is not None:
                self._page_failed(self._current, e)

    def _page_failed(self, task: StudyTask, error: Exception) -> None:
        """Сторінка цього завдання раз у раз не опрацьовується — далі не мучимо, кажемо чесно."""
        failures = self._page_failures[task.key] = self._page_failures.get(task.key, 0) + 1
        if failures >= MAX_PAGE_FAILURES:
            self._set(task, "failed", error=f"Classroom: {str(error)[:200]}")
            self._tell(f"⚠️ {task.label}: сторінку завдання в Classroom не вдалося опрацювати — глянь сам.", task=task)

    def _browser_step(self, now: datetime) -> None:
        last = self.state.get("last_check")
        if not last or now - datetime.fromisoformat(last) >= timedelta(hours=self.config.study.check_hours):
            self._check_classroom(now)
            return
        for status, step in (("approved", self._submit), ("returning", self._unsubmit), ("new", self._fetch_details)):
            waiting = [task for task in self._ordered() if task.status == status]
            if waiting:
                self._current = waiting[0]
                step(waiting[0])
                return

    def _ordered(self) -> list[StudyTask]:
        """Спершу — з найближчим терміном, далі прострочені (свіжіші першими), далі без терміну."""
        now = self.clock()

        def priority(task: StudyTask):
            if not task.due:
                return (2, 0.0, task.number)
            due = datetime.fromisoformat(task.due)
            if due >= now:
                return (0, due.timestamp(), task.number)
            return (1, -due.timestamp(), task.number)

        with self._lock:
            return sorted(self.tasks, key=priority)

    def _resume_waiting(self, now: datetime) -> None:
        """Агент знову доступний (встановили, увійшли, новий день після ліміту) — продовжуємо."""
        if self._next_agent_check is not None and now < self._next_agent_check:
            return
        self._next_agent_check = now + timedelta(minutes=10)
        waiting = [task for task in self._ordered() if task.status == "waiting"]
        if waiting and self.agent.problem() is None:
            for task in waiting:
                self._set(task, "working")
                self._jobs.put(("work", task.number))

    def _check_classroom(self, now: datetime) -> None:
        items = self.classroom.todo()
        self._cookies = self.classroom.cookies()
        first = not self.state.get("last_check") and not self.tasks
        self.state["last_check"] = now.isoformat(timespec="minutes")
        self.state.pop("last_error", None)
        skip = [part.casefold() for part in self.config.study.skip_courses if part.strip()]
        new = []
        for item in items:
            if self._by_key(item.key) is not None:
                continue
            if any(part in item.course.casefold() for part in skip):
                continue
            due = due_of(item.listing, now)
            with self._lock:
                number = self.state["next_number"]
                self.state["next_number"] = number + 1
                task = StudyTask(
                    item.key, number, item.course, item.title or "без назви", item.listing,
                    due=due.isoformat(timespec="minutes") if due else "",
                )
                self.tasks.append(task)
            new.append(task)
        self._forget_gone({item.key for item in items})
        self._save()
        log.info("Classroom: незданих завдань %d, нових %d", len(items), len(new))
        if first and new:
            lines = [f"📚 Знайшов у Classroom незданих завдань: {len(new)}. Беруся з найтерміновіших:"]
            lines += [f"{task.label}" + (f" — до {datetime.fromisoformat(task.due):%d.%m}" if task.due else "")
                      for task in self._ordered() if task in new][:15]
            lines.append("Кожну роботу пришлю повністю готовою — здам лише після твого «✅ Так».")
            self._tell("\n".join(lines))

    def _forget_gone(self, listed: set[str]) -> None:
        """Завдання, якого двічі поспіль немає серед незданих, ти здав сам (або його прибрали) — агент його не робить."""
        if not listed:
            return  # порожній список підозрілий (сторінка не довантажилась) — нічого не прибираємо
        for task in self.tasks:
            if task.key in listed or task.status not in ("new", "waiting", "stuck", "ready", "failed"):
                self._missing.pop(task.key, None)
                continue
            self._missing[task.key] = self._missing.get(task.key, 0) + 1
            if self._missing[task.key] >= 2:
                with self._lock:
                    task.status = "gone"

    def _fetch_details(self, task: StudyTask) -> None:
        item = self.classroom.details(task.assignment())
        if item.status == "turned_in":
            self._set(task, "submitted", submitted="раніше")
            return
        if item.is_quiz or NOT_FOR_AGENT_RE.search(task.title):
            self._set(task, "skipped")
            self._tell(f"📋 {task.label} — схоже на тест чи захист. Агент таке не робить, це на тобі.", task=task)
            return
        folder = self.folder / "tasks" / f"{task.number:03d}_{safe_name(task.title)}"
        (folder / "source").mkdir(parents=True, exist_ok=True)
        (folder / "source" / "assignment.txt").write_text(item.text, encoding="utf-8")
        (folder / "source" / "attachments.json").write_text(
            json.dumps([asdict(a) for a in item.attachments], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        self._set(task, "working", folder=str(folder))
        self._jobs.put(("work", task.number))

    def _submit(self, task: StudyTask) -> None:
        files = [Path(path) for path in task.files if Path(path).exists()]
        if not files:
            self._set(task, "failed", error="файли роботи зникли з диска")
            self._tell(f"⚠️ {task.label}: не знайшов файлів роботи — не здаю.", task=task)
            return
        dry_run = self.state.get("real_submissions", 0) == 0 and not self.state.get("dry_run_done")
        result = self.classroom.submit(task.assignment(), files, dry_run=dry_run)
        if not result.ok:
            self._set(task, "failed", error=result.note)
            self._tell(f"⚠️ {task.label}: {result.note}. Здай, будь ласка, сам.", photo=result.screenshot, task=task)
            return
        if dry_run:
            self.state["dry_run_done"] = True
            self._set(task, "ready")
            self._ask(
                task,
                f"🧪 Пробна здача {task.label}: файли прикріплені, «Здати» не натискав — ось як це виглядає. "
                "Якщо все на місці, натисни «✅ Здати» ще раз — і я здам по-справжньому.",
                photo=result.screenshot,
            )
            return
        self.state["real_submissions"] = self.state.get("real_submissions", 0) + 1
        self._set(task, "submitted", submitted=self.clock().isoformat(timespec="minutes"))
        self._tell(
            f"✅ Здано: {task.label}. Передумав — можна забрати, поки не оцінили.",
            photo=result.screenshot,
            buttons=[[("↩️ Скасувати здачу", f"study:back:{task.number}")]],
            task=task,
        )

    def _unsubmit(self, task: StudyTask) -> None:
        result = self.classroom.unsubmit(task.assignment())
        if result.ok:
            self._set(task, "ready", submitted="")
        else:
            self._set(task, "submitted")
        self._tell(f"↩️ {task.label}: {result.note}", photo=result.screenshot, task=task)

    # --- Агент (фоновий потік) -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._jobs.get()
            if job is None:
                return
            kind, payload = job
            try:
                if kind == "work":
                    self._work(payload)
                elif kind == "lecture":
                    self._lecture(*payload)
            except Exception:  # noqa: BLE001 — одне завдання не має зупиняти решту
                log.exception("Навчання: помилка в завданні %s", payload)

    def _work(self, number: int) -> None:
        task = self.task(number)
        if task is None or task.status != "working":
            return
        folder = Path(task.folder)
        self._download_materials(folder)
        (folder / "out").mkdir(exist_ok=True)
        (folder / "TASK.md").write_text(self._task_text(task, folder), encoding="utf-8")
        reason = ""
        for attempt in range(1, self.config.agent.attempts + 1):
            task.attempts += 1
            instruction = WORK_INSTRUCTION if attempt == 1 and not task.feedback else (
                WORK_INSTRUCTION + (f" Попередня перевірка сказала: {reason}" if reason else "")
            )
            try:
                done = self.agent.run(instruction, folder)
                if self._stop.is_set():
                    return  # лишається «агент працює» — доробимо після перезапуску
                if not done.ok:
                    reason = done.error
                    continue
                self._forget_verdict(folder)  # вердикт має бути саме цієї перевірки, не попередньої
                checked = self.agent.run(CHECK_INSTRUCTION, folder)
                if self._stop.is_set():
                    return
            except AgentError as e:
                self._set(task, "waiting", error=str(e))
                if self.state.get("agent_notice") != str(e):  # одне повідомлення на причину, не на кожне завдання
                    self.state["agent_notice"] = str(e)
                    self._tell(f"⏸ Агент: {e}. Завдання чекають і продовжаться самі.")
                return
            status, reason = self._verdict(folder, checked.text)
            if checked.ok and status == "COMPLETE" and self._deliverables(folder):
                self.state.pop("agent_notice", None)
                return self._ready(task, folder)
            reason = reason or checked.error or "перевірка не підтвердила, що все готово"
        self._set(task, "stuck", error=reason)
        self._tell(
            f"🧱 {task.label}: не вдалося довести до повністю готового стану за {self.config.agent.attempts} спроби. "
            f"Що заважає: {reason[:600]}",
            buttons=[[("🔁 Спробувати ще", f"study:retry:{task.number}")]],
            task=task,
        )

    @staticmethod
    def _forget_verdict(folder: Path) -> None:
        result = folder / "RESULT.md"
        if result.exists():
            text = result.read_text(encoding="utf-8")
            result.write_text("\n".join(line for line in text.splitlines() if not STATUS_RE.search(line)), encoding="utf-8")

    @staticmethod
    def _verdict(folder: Path, text: str) -> tuple[str, str]:
        result = (folder / "RESULT.md").read_text(encoding="utf-8") if (folder / "RESULT.md").exists() else ""
        matches = STATUS_RE.findall(result + "\n" + text)
        if not matches:
            return "", ""
        status, reason = matches[-1]
        return status.upper(), reason.strip()

    def _deliverables(self, folder: Path) -> list[Path]:
        return sorted(path for path in (folder / "out").rglob("*") if path.is_file() and path.stat().st_size > 0)

    def _ready(self, task: StudyTask, folder: Path) -> None:
        files = self._deliverables(folder)
        result = (folder / "RESULT.md").read_text(encoding="utf-8") if (folder / "RESULT.md").exists() else ""
        summary = STATUS_RE.sub("", result).strip()
        self._set(task, "ready", files=[str(path) for path in files], summary=summary[:3000], error="", version=task.version + 1)
        names = ", ".join(path.name for path in files)
        self._ask(
            task,
            f"📘 Готово: {task.label}\n\n{summary[:1500]}\n\n📎 {names}\n\nЗдати?",
            files=files,
            extra=[folder / "DEFENSE.md"],
        )

    def _download_materials(self, folder: Path) -> None:
        listing = folder / "source" / "attachments.json"
        target = folder / "materials"
        if not listing.exists() or target.exists():
            return
        target.mkdir()
        account = self._account()
        for raw in json.loads(listing.read_text(encoding="utf-8")):
            attachment = Attachment(**raw)
            source = attachment.download_url(account)
            if source is None:
                continue
            url, extension = source
            name = safe_name(attachment.title or attachment.file_id) + extension
            try:
                download(url, target, self._cookies, name)
            except Exception as e:  # noqa: BLE001 — без одного файла агент усе одно спробує
                log.warning("Не вдалося завантажити «%s»: %s", attachment.title, e)
                (target / f"{name}.link.txt").write_text(attachment.url, encoding="utf-8")

    def _account(self) -> str:
        return self.config.study.account or self.config.join.meet_account

    def _task_text(self, task: StudyTask, folder: Path) -> str:
        study, now = self.config.study, self.clock()
        name = study.full_name or self.config.me.name
        group = study.group or self.config.schedule.group
        report_name = f"{short_name(name)}_{safe_name(task.title, 40)}.{study.file_format}"
        notes = self._course_notes(task.course)
        materials = sorted(p.name for p in (folder / "materials").glob("*")) if (folder / "materials").exists() else []
        feedback = "\n".join(f"- {comment}" for comment in task.feedback)
        return f"""# Завдання з Google Classroom

Курс: {task.course}
Завдання: {task.title}
У списку завдань: {task.listing}

## Умова (текст сторінки завдання в Classroom; там може бути зайвий текст інтерфейсу — ігноруй його)

{(folder / "source" / "assignment.txt").read_text(encoding="utf-8")}

## Вкладені матеріали — у теці materials/

{chr(10).join(f"- {m}" for m in materials) or "- (немає)"}

## Студент

- ПІБ: {name}
- Група: {group}
- Спеціальність: {study.specialty}
- Університет: {study.university}
- Кафедра: {study.department or "візьми з матеріалів курсу; якщо не знайдеш — «Кафедра інженерії програмного забезпечення»"}
- Номер у списку групи (для вибору варіанта): {self.list_number or "невідомо — якщо варіант за списком, напиши про це в RESULT.md"}
- Місто й рік: {study.city} – {now.year}

## Що казали на парах з цього предмета (вижимки лекцій; тут бувають побажання викладача до робіт)

{notes or "(вижимок ще немає)"}

{"## Зауваження студента до попередньої версії — обов'язково врахуй" + chr(10) + chr(10) + feedback if feedback else ""}

## Як виконати

1. Виконай роботу ПОВНІСТЮ сам. Код має справді запускатися — запусти його. Скріншоти мають бути справжніми
   (зніми їх із запущеної програми чи консолі), відео — якщо умова його вимагає (запиши екран симуляції).
   Якщо потрібна програма з інтерфейсом чи вебсервіс (Tinkercad, Excel тощо) — скористайся браузером та
   інструментами, які маєш; якщо щось недоступне — знайди рівноцінний спосіб і чесно опиши його в RESULT.md.
2. Звіт — українською, як у студента в попередніх роботах: титульна сторінка (університет; кафедра;
   дисципліна «…»; «Звіт з лабораторної/практичної роботи №…»; тема; «Виконав: {name}»; «Група: {group}»;
   «Спеціальність: {study.specialty}»; «{study.city} – {now.year}»), далі «Мета», «Варіант» (якщо є),
   «Хід роботи» з поясненнями й скріншотами, «Висновок». Пиши просто й по суті, як студент 3 курсу.
3. Поклади в out/ ЛИШЕ те, що треба здати: звіт `{report_name}` і, якщо треба, код (архів чи файли),
   відео тощо. Чернетки й службові файли — поза out/.
4. Запиши RESULT.md: коротко, що зроблено, які файли в out/ і що в кожному, як перевірити, на що звернути
   увагу. Без води.
5. Запиши DEFENSE.md — шпаргалку до захисту: суть роботи простими словами, як воно працює, ключові
   поняття, і 8–12 імовірних питань викладача з короткими відповідями.
"""

    # --- Вижимки лекцій ---------------------------------------------------------------

    def lecture_record(self, record) -> None:
        """Звіт про пару від помічника (LessonRecord) → вижимка лекції."""
        transcript = "\n".join(f"[{t:%H:%M}] {text}" for t, text in record.transcript)
        chat = "\n".join(f"[{t:%H:%M}] {m.author or '?'}: {m.text}" for t, m in record.chat)
        self.lecture(record.title, record.teacher, record.start, transcript, chat)

    def lecture(self, title: str, teacher: str, when: datetime, transcript: str, chat: str = "") -> None:
        """Пара скінчилась — агент робить детальну вижимку (у фоні) і кладе її в конспект курсу."""
        if not self.config.study.lecture_notes or len(transcript) < 400:
            return
        self._jobs.put(("lecture", (title, teacher, when, transcript, chat)))

    def _lecture(self, title: str, teacher: str, when: datetime, transcript: str, chat: str) -> None:
        subject = subject_of(title)
        folder = self.folder / "notes" / safe_name(subject) / f"{when:%Y-%m-%d_%H-%M}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "LECTURE.md").write_text(
            f"""# Вижимка пари

Пара: {title}, {when:%d.%m.%Y %H:%M}, викладач: {teacher or "?"}

Нижче — автоматична розшифровка пари (з помилками розпізнавання; «[?]» — розпізнано невпевнено) і чат.
Зроби для студента, якого не було на парі, детальну вижимку українською в SUMMARY.md:

- Тема й головне за 3–5 речень.
- Ключові поняття й означення (по пунктах, з короткими поясненнями).
- Що показували чи розбирали (приклади, код, задачі) — так, щоб можна було відтворити.
- Завдання, дедлайни, оголошення — окремим списком, дослівно, якщо можна.
- Вимоги й побажання викладача до робіт, захистів, оформлення — окремим списком.
- Що варто повторити чи дочитати.

Не вигадуй того, чого не було в розшифровці.

## Розшифровка

{transcript}

## Чат

{chat or "(порожньо)"}
""",
            encoding="utf-8",
        )
        try:
            result = self.agent.run(LECTURE_INSTRUCTION, folder, timeout_minutes=20)
        except AgentError as e:
            log.info("Вижимку лекції не зроблено: %s", e)
            return
        summary_file = folder / "SUMMARY.md"
        summary = summary_file.read_text(encoding="utf-8") if summary_file.exists() else result.text
        if not result.ok or not summary.strip():
            log.warning("Агент не зробив вижимку «%s»: %s", title, result.error)
            return
        summary_file.write_text(summary, encoding="utf-8")
        self._tell(f"📚 Вижимка: «{title}», {when:%d.%m %H:%M}\n\n{summary[:3500]}")
        if len(summary) > 3500:
            self.notifier.document(summary.encode("utf-8"), f"Вижимка_{safe_name(subject)}_{when:%d.%m}.md", title, on_sent=self._mine)

    def _course_notes(self, course: str) -> str:
        root = self.folder / "notes"
        if not root.exists():
            return ""
        subjects = {path.name.replace("_", " "): path for path in root.iterdir() if path.is_dir()}
        match = match_subject(course, list(subjects))
        if match is None:
            return ""
        parts, size = [], 0
        for folder in sorted(subjects[match].iterdir(), reverse=True):  # свіжі — першими
            summary = folder / "SUMMARY.md"
            if not summary.exists():
                continue
            text = f"### {folder.name}\n{summary.read_text(encoding='utf-8')}"
            if size + len(text) > MAX_NOTES_CHARS:
                break
            parts.append(text)
            size += len(text)
        return "\n\n".join(parts)

    # --- Ти: Telegram і вікно ------------------------------------------------------

    def on_command(self, text: str, reply_to: int = 0) -> str | None:
        """Відповідь на повідомлення про навчання; None — це не до нас (хай розбирає помічник пари)."""
        low = text.strip().casefold()
        if low.startswith("study:"):
            _, action, number, version = (low.split(":") + ["", "", ""])[:4]
            return self.act(int(number) if number.isdigit() else 0, action, version=int(version) if version.isdigit() else None)
        if reply_to and reply_to in self._awaiting_feedback:
            return self.act(self._task_number(self._awaiting_feedback.pop(reply_to)), "redo", text.strip())
        # Відповіді на наші повідомлення — нам, а не в чат пари (туди помічник пересилає відповіді на свої).
        if reply_to and (task := self._task_by_message(reply_to)) is not None:
            if low in SHORT_ANSWERS:
                return "Щоб здати чи відмовитись, натисни кнопку під останньою версією роботи — так точно не здам не те."
            if task.status in ("ready", "stuck", "failed"):
                return self.act(task.number, "redo", text.strip())
            return f"{task.label} зараз: {STATUSES[task.status]}. У чат пари це не пересилаю."
        if reply_to and reply_to in self.state.get("own_messages", []):
            return (
                "Це повідомлення про навчання — у чат пари його не пересилаю. Щоб переробити роботу, "
                "відповідай на повідомлення з нею."
            )
        if low in ("завдання", "/tasks", "навчання"):
            return self.overview()
        return None

    def _task_number(self, key: str) -> int:
        task = self._by_key(key)
        return task.number if task else 0

    def _task_by_message(self, message_id: int) -> StudyTask | None:
        return next((task for task in self.tasks if message_id in task.messages), None)

    def act(self, number: int, action: str, comment: str = "", version: int | None = None) -> str:
        task = self.task(number)
        if task is None:
            return "Такого завдання вже немає."
        if action == "yes":
            if task.status not in ("ready", "failed") or not task.files:
                return f"{task.label} зараз у стані «{STATUSES[task.status]}» — здавати нічого."
            if version != task.version:
                return f"Це кнопка від старішої версії {task.label}. Глянь останню й натисни «✅» під нею."
            self._set(task, "approved")
            return f"Добре, здаю {task.label} між парами й напишу, коли буде здано."
        if action == "no":
            if task.status in ("approved", "ready", "stuck", "failed"):
                self._set(task, "declined")
            return f"Не здаю {task.label}. Файли лишаються в {task.folder}."
        if action == "redo":
            if not comment:
                self._ask_feedback(task)
                return ""
            with self._lock:
                task.feedback.append(comment)
            self._set(task, "working")
            self._jobs.put(("work", task.number))
            return f"Переробляю {task.label} з урахуванням: «{comment[:200]}». Пришлю, коли буде повністю готово."
        if action == "retry":
            if not task.folder:  # сторінку завдання ще не прочитали — почнемо з неї
                self._page_failures.pop(task.key, None)
                self._set(task, "new")
                return f"Ще раз відкрию {task.label} у Classroom між парами."
            self._set(task, "working")
            self._jobs.put(("work", task.number))
            return f"Пробую ще раз: {task.label}."
        if action == "back":
            if task.status != "submitted":
                return f"{task.label} не здано — скасовувати нічого."
            self._set(task, "returning")
            return f"Заберу {task.label} з Classroom між парами."
        return "Не зрозумів."

    def overview(self) -> str:
        active = [task for task in self.tasks if task.status not in ("submitted", "declined", "skipped", "gone")]
        if not active:
            return "📚 Незданих завдань немає (або Classroom ще не перевіряв)."
        lines = ["📚 Завдання:"]
        lines += [f"{task.label} — {STATUSES[task.status]}" for task in active[:25]]
        return "\n".join(lines)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "tasks": [
                    {**asdict(task), "status_text": STATUSES.get(task.status, task.status)}
                    for task in self._ordered()  # у тому ж порядку, що й агент: найтерміновіші першими
                ],
                "last_check": self.state.get("last_check", ""),
                "last_error": self.state.get("last_error", ""),
                "list_number": self.list_number,
                "agent_problem": self.agent.problem(),
                "agent_used": self.agent.used_today(),
                "agent_limit": self.config.agent.daily_limit,
                "dry_run_done": bool(self.state.get("dry_run_done")),
            }

    def check_now(self) -> None:
        self.state["last_check"] = ""
        self._retry_at = None

    # --- Повідомлення ------------------------------------------------------------------

    def _ask(self, task: StudyTask, text: str, files: list[Path] = (), extra: list[Path] = (), photo: bytes | None = None) -> None:
        buttons = [
            [("✅ Так, здати", f"study:yes:{task.number}:{task.version}"), ("✏️ Переробити", f"study:redo:{task.number}")],
            [("❌ Не здавати", f"study:no:{task.number}")],
        ]
        remember = lambda message_id: self._remember(task, message_id)  # noqa: E731
        if photo:
            self.notifier.photo(photo, f"{task.label}", on_sent=remember)
        for path in list(files) + [p for p in extra if p.exists()]:
            caption = "📒 Шпаргалка до захисту" if path.name == "DEFENSE.md" else path.name
            self.notifier.document(path.read_bytes(), path.name, caption, on_sent=remember)
        self.notifier.ask(text, buttons, urgent=False, on_sent=remember)

    def _ask_feedback(self, task: StudyTask) -> None:
        def remember(message_id: int) -> None:
            self._awaiting_feedback[message_id] = task.key
            self._remember(task, message_id)

        self.notifier.ask(f"✏️ Що змінити в {task.label}? Відповідай на це повідомлення.", [], urgent=False, on_sent=remember)

    def _remember(self, task: StudyTask, message_id: int) -> None:
        with self._lock:
            task.messages = (task.messages + [message_id])[-20:]
        self._save()

    def _mine(self, message_id: int) -> None:
        """Наше повідомлення не про конкретне завдання (вижимка, огляд) — щоб відповідь на нього не пішла в чат пари."""
        with self._lock:
            self.state["own_messages"] = (self.state.get("own_messages", []) + [message_id])[-300:]
        self._save()

    def _tell(self, text: str, photo: bytes | None = None, buttons=None, task: StudyTask | None = None) -> None:
        remember = (lambda message_id: self._remember(task, message_id)) if task is not None else self._mine
        if photo:
            self.notifier.photo(photo, text[:200], on_sent=remember)
        self.notifier.ask(text, buttons or [], urgent=False, on_sent=remember)

    def cleanup(self) -> None:
        """Завдання, яких більше немає в Classroom і які вже не потрібні, — прибрати з диска не поспішаємо."""
        shutil.rmtree(self.folder / "tmp", ignore_errors=True)
