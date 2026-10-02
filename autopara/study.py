"""Навчання: завдання з Google Classroom робить агент, а здає програма — лише після твого «так».

Шлях кожного завдання:

    нове → (спитати «робити?») → агент працює ⇄ питання до тебе → перевірка й доробка → готово
         → твоє «✅ Так» → здано → (оцінено / повернуто → переробити)

- Тобі приходять лише ПОВНІСТЮ готові роботи: окремий прохід агента перевіряє результат пункт за
  пунктом проти умови й доробляє. Не вдалося довести до кінця за кілька спроб — пишемо чесно,
  що саме заважає, а не надсилаємо напівготове.
- Коли без тебе не обійтися (незрозумілий варіант, суперечлива умова, потрібен твій акаунт), агент
  не вгадує, а питає тебе через бота; відповідаєш — і він продовжує.
- Здає програма тільки після твого «так» (кнопкою в Telegram або у вікні програми). Перший раз —
  пробна здача: усе, крім натискання «Здати», зі скріном, щоб ти побачив, що все на місці.
- Тести (Google Forms), захисти й контрольні агент не чіпає — лише нагадує про них.
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
from .classroom import Assignment, Attachment, Classroom, ClassroomError, download, task_url
from .config import Config

log = logging.getLogger(__name__)

STATUSES = {
    "new": "нове",
    "proposed": "чекає, чи робити",
    "working": "агент працює",
    "question": "агент питає тебе",
    "waiting": "чекає на агента",
    "stuck": "не вдалося довести до кінця",
    "ready": "готово — чекає на твоє «так»",
    "approved": "здаю",
    "submitted": "здано",
    "returning": "забираю з Classroom",
    "returned": "викладач повернув",
    "declined": "не здавати",
    "gone": "уже не в списку незданих (здано без мене чи прибрано)",
    "skipped": "не для агента",
    "failed": "не вдалося здати",
}
WAITING_ON_YOU = ("proposed", "question", "ready", "returned")
DONE = ("submitted", "declined", "skipped", "gone")
UNIVERSITY_NAMES = {
    "duikt": "Державний університет інформаційно-комунікаційних технологій",
    "kpi": "Національний технічний університет України «Київський політехнічний інститут імені Ігоря Сікорського»",
}
# Не робота для агента: тести, захисти, контрольні.
# Лише за початком назви завдання: «Тест 2», «Захист ЛР», «МКР 1». Не за текстом сторінки — там є назва
# курсу («Якість та тестування ПЗ»), і не за серединою назви («ЛР 3. Unit-тести» — звичайна лаба).
NOT_FOR_AGENT_RE = re.compile(
    r"^\W*(тест|тести|захист\w*|контрольн\w*|мкр|модульн\w*|екзамен\w*|залік\w*|опитуван\w*|quiz|exam|test)\b", re.I
)
SHORT_ANSWERS = {"так", "yes", "ок", "ok", "+", "здавай", "здати", "давай", "роби", "ні", "no", "не треба"}
STARTS = {"так", "yes", "ок", "ok", "+", "давай", "роби"}
STATUS_RE = re.compile(r"STATUS:\s*(COMPLETE|INCOMPLETE)\s*:?\s*(.*)", re.I)
MAX_NOTES_CHARS = 14_000
RETRY_CLASSROOM_AFTER = timedelta(minutes=20)
MAX_PAGE_FAILURES = 3
TELEGRAM_FILE_LIMIT = 45 * 1024 * 1024  # боти Telegram не надсилають файли понад 50 МБ
EXAMPLES_LIMIT = 30 * 1024 * 1024
EXAMPLE_SUFFIXES = {".pdf", ".docx", ".doc", ".odt", ".txt", ".md", ".png", ".jpg", ".jpeg", ".pptx", ".xlsx"}
WATCH_GRADES_FOR = timedelta(days=30)
DECIDE = "Виріши сам, як вважаєш найправильнішим, і поясни своє рішення в RESULT.md."
PAUSED = "пауза — агента зупинено кнопкою «Пауза» (продовжити: «▶️ Продовжити» або «продовжуй» боту)"
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
    "питання — що агент питає в тебе\n"
    "перевір — заглянути в Classroom найближчим часом\n"
    "пауза / продовжуй — зупинити агента чи продовжити\n"
    "↩️ відповідь на повідомлення про завдання — відповісти агентові чи переробити роботу з твоїм зауваженням"
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
ASK_INSTRUCTION = (
    " Якщо без відповіді студента роботу правильно не зробити, не вгадуй: запиши питання в QUESTION.md і "
    "зупинись (див. TASK.md)."
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
    questions: list[dict] = field(default_factory=list)  # [{"q": питання агента, "a": твоя відповідь}]
    started: bool = False  # ти вже сказав «роби» — більше не питаємо
    rework: bool = False  # переробка після повернення викладачем: сторінку перечитати, хоч її й здано
    attached: list[str] = field(default_factory=list)  # які файли прикріплені в Classroom після здачі
    grade: str = ""
    grade_checked: str = ""
    reminded: str = ""  # про який термін уже нагадали

    @property
    def label(self) -> str:
        return f"№{self.number} «{self.title}»" + (f" ({self.course})" if self.course else "")

    @property
    def open_question(self) -> str:
        return self.questions[-1]["q"] if self.questions and not self.questions[-1].get("a") else ""

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


def file_safe(text: str, limit: int = 120) -> str:
    """Назва файлу: лише прибрати те, що заборонено у Windows, і пробіли."""
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text).strip("_.")[:limit] or "robota"


# Тип роботи за назвою завдання → скорочення для тек і назв файлів. Порядок важливий: «Курсовий проєкт» — курсова.
WORK_KINDS = [
    (r"лаборатор\w*|\bлр\b|\bлаб\w*|\blab\w*", "Лб"),
    (r"практичн\w*|практикум\w*|\bпр\b|\bпз\b|practic\w*", "Пр"),
    (r"курсов\w*|course ?work|course ?project", "Курсова"),
    (r"самостійн\w*|\bсрс\b|\bср\b", "СР"),
    (r"семінар\w*|seminar\w*", "Сем"),
    (r"розрахунков\w*|\bргр\b", "РГР"),
    (r"індивідуальн\w*|\bідз\b|\bіз\b", "ІЗ"),
    (r"домашн\w*|\bдз\b|homework", "ДЗ"),
    (r"реферат\w*", "Реф"),
    (r"\bесе\b|essay", "Есе"),
    (r"презентац\w*|presentation", "Презентація"),
    (r"проєкт\w*|проект\w*|project", "Проєкт"),
]
WORK_NUMBER_RE = re.compile(r"(?<![\d.])(\d{1,2}(?:[.,]\d{1,2})?)(?![\d])")
FILLER_RE = re.compile(r"\b(робот\w*|заняття|завдання|work|assignment|task)\b", re.I)


def work_kind(title: str) -> tuple[str, str, str]:
    """«ПР № 1.2 Запити вибірки» → ("Пр", "1.2", "Запити вибірки"); «Лабораторна робота_1» → ("Лб", "1", "")."""
    rest, kind = title, "Завдання"
    for pattern, short in WORK_KINDS:
        if match := re.search(pattern, rest, re.I):
            kind, rest = short, rest[: match.start()] + " " + rest[match.end():]
            break
    if kind != "Завдання":  # «Курсовий проєкт»: друге слово типу — теж не тема
        for pattern, _ in WORK_KINDS:
            rest = re.sub(pattern, " ", rest, flags=re.I)
    number = ""
    if match := WORK_NUMBER_RE.search(rest):
        number, rest = match.group(1).replace(",", "."), rest[: match.start()] + " " + rest[match.end():]
    rest = FILLER_RE.sub(" ", rest.replace("_", " "))
    topic = re.sub(r"\s+", " ", re.sub(r"[№#]+", " ", rest)).strip(" .,:;-–—()[]")
    return kind, number, topic if len(topic) >= 3 else ""


def dir_name(text: str, limit: int = 80) -> str:
    """Назва теки, зручна людині: пробіли лишаються, прибрано лише заборонене у Windows і роки на кшталт «2026»."""
    text = re.sub(r'[\\/:*?"<>|]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text[:limit].strip(" .") or "Інше"


def course_dir(course: str) -> str:
    """«2026 Бази даних» → «Бази даних», «ЯТПЗ - 31/2026» → «ЯТПЗ - 31»."""
    return dir_name(re.sub(r"\b20\d\d\b", " ", course))


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


def in_quiet_hours(hour: int, start: int, end: int) -> bool:
    if start == end:
        return False
    return start <= hour < end if start < end else hour >= start or hour < end


def notes_index(root: Path) -> list[dict]:
    """Конспекти: предмети → пари (найсвіжіші першими) з першими рядками вижимки."""
    subjects = []
    if not root.is_dir():
        return subjects
    for subject in sorted(path for path in root.iterdir() if path.is_dir()):
        lectures = []
        for folder in sorted((path for path in subject.iterdir() if path.is_dir()), reverse=True):
            summary = folder / "SUMMARY.md"
            text = summary.read_text(encoding="utf-8", errors="replace") if summary.is_file() else ""
            preview = next((line.strip("#*- ").strip() for line in text.splitlines() if line.strip("#*- ").strip()), "")
            lectures.append({"id": f"{subject.name}/{folder.name}", "date": folder.name, "ready": bool(text), "preview": preview[:160]})
        subjects.append({"name": subject.name, "lectures": lectures})
    return subjects


def read_note(root: Path, note_id: str) -> str | None:
    """Вижимка однієї пари; None — немає такої (або шлях веде за межі теки конспектів)."""
    folder = (root / note_id).resolve()
    if root.resolve() not in folder.parents:
        return None
    if (folder / "SUMMARY.md").is_file():
        return (folder / "SUMMARY.md").read_text(encoding="utf-8", errors="replace")
    if (folder / "LECTURE.md").is_file():  # вижимки ще немає (агент на паузі чи недоступний) — хоч розшифровка
        text = (folder / "LECTURE.md").read_text(encoding="utf-8", errors="replace")
        transcript = text.split("## Розшифровка", 1)[-1]
        return "_Вижимки ще немає — нижче розшифровка пари._\n\n## Розшифровка" + transcript
    return None


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
        self._next_remind: datetime | None = None
        self._current: StudyTask | None = None  # з яким завданням зараз працює браузер
        self._restarted: list[int] = []
        self._missing: dict[str, int] = {}  # скільки перевірок поспіль завдання немає серед незданих
        self._awaiting_feedback: dict[int, str] = {}  # повідомлення «напиши, що змінити» → завдання
        self.activity: dict | None = None  # що агент робить просто зараз — для вікна
        self.state = self._load()

    # --- Стан ----------------------------------------------------------------------

    @property
    def state_file(self) -> Path:
        return self.folder / "tasks.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            known = set(StudyTask.__dataclass_fields__)
            data["tasks"] = [StudyTask(**{k: v for k, v in task.items() if k in known}) for task in data.get("tasks", [])]
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

    def _queue_work(self, task: StudyTask, **changes) -> None:
        self._set(task, "working", rework=False, **changes)
        self._jobs.put(("work", task.number))

    @property
    def paused(self) -> bool:
        return bool(self.state.get("paused"))

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

    def pause(self, on: bool) -> str:
        with self._lock:
            self.state["paused"] = on
        self._save()
        if on:
            return "⏸ Агента на паузі: нових робіт не беру (готові можна здавати). «продовжуй» — і продовжу."
        self._next_agent_check = None  # продовжити одразу на наступному кроці
        return "▶️ Продовжую: агент знову бере роботи."

    # --- Браузер (потік контролера, між парами) ------------------------------------

    def browser_tick(self, now: datetime) -> None:
        """Крок між парами (потік контролера): Classroom — по одній дії за раз, щоб автовхід лишався чуйним."""
        self._resume_waiting(now)
        self._remind(now)
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
        if self.config.study.watch_grades and (task := self._grade_due(now)) is not None:
            self._current = task
            self._watch_grade(task, now)

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
        """Агент знову доступний (встановили, увійшли, новий день після ліміту, зняли паузу) — продовжуємо."""
        if self.paused or (self._next_agent_check is not None and now < self._next_agent_check):
            return
        self._next_agent_check = now + timedelta(minutes=10)
        waiting = [task for task in self._ordered() if task.status == "waiting"]
        if waiting and self.agent.problem() is None:
            for task in waiting:
                self._queue_work(task, error="")

    def _rule(self, course: str) -> dict:
        """Правило з налаштувань для цього предмета (варіант, побажання, режим) — або порожнє."""
        low = course.casefold()
        for rule in self.config.study.courses:
            if any(part.strip() and part.strip().casefold() in low for part in str(rule.get("course", "")).split("|")):
                return rule
        return {}

    def _skips_course(self, course: str) -> bool:
        low = course.casefold()
        skip = [part.strip().casefold() for part in self.config.study.skip_courses if part.strip()]
        return any(part in low for part in skip) or self._rule(course).get("mode") == "skip"

    def _check_classroom(self, now: datetime) -> None:
        items = self.classroom.todo()
        self._cookies = self.classroom.cookies()
        first = not self.state.get("last_check") and not self.tasks
        self.state["last_check"] = now.isoformat(timespec="minutes")
        self.state.pop("last_error", None)
        new = []
        too_old = 0
        for item in items:
            due = due_of(item.listing, now)
            if (task := self._by_key(item.key)) is not None:
                self._seen_again(task, item, due)
                continue
            if self._skips_course(item.course):
                continue
            if due is not None and now - due > timedelta(days=self.config.study.overdue_days):
                too_old += 1
                continue
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
        log.info("Classroom: незданих завдань %d, нових %d, давно прострочених пропущено %d", len(items), len(new), too_old)
        if first and new:
            lines = [f"📚 Знайшов у Classroom незданих завдань: {len(new)}. Беруся з найтерміновіших:"]
            lines += [f"{task.label}" + (f" — до {datetime.fromisoformat(task.due):%d.%m}" if task.due else "")
                      for task in self._ordered() if task in new][:15]
            if too_old:
                lines.append(f"Ще {too_old} прострочені давніше ніж на {self.config.study.overdue_days} дн. — їх не чіпаю.")
            lines.append("Кожну роботу пришлю повністю готовою — здам лише після твого «✅ Так».")
            self._tell("\n".join(lines))

    def _seen_again(self, task: StudyTask, item: Assignment, due: datetime | None) -> None:
        """Завдання знову в списку незданих: термін могли перенести, а здану роботу — повернути."""
        with self._lock:
            task.listing = item.listing or task.listing
            task.due = due.isoformat(timespec="minutes") if due else task.due
        if task.status == "gone":
            self._set(task, "new")  # список минулого разу просто не довантажився
        elif task.status == "submitted":
            self._set(task, "returned")
            link = task_url(task.assignment(), self._account())
            self._tell(
                f"↩️ {task.label}: роботу повернули (викладач на доопрацювання або здачу скасовано).\n{link}\n\n"
                "Переробити з урахуванням коментарів викладача?",
                buttons=[[("✏️ Переробити", f"study:rework:{task.number}"), ("❌ Не треба", f"study:no:{task.number}")]],
                task=task,
            )

    def _forget_gone(self, listed: set[str]) -> None:
        """Завдання, якого двічі поспіль немає серед незданих, ти здав сам (або його прибрали) — агент його не робить."""
        if not listed:
            return  # порожній список підозрілий (сторінка не довантажилась) — нічого не прибираємо
        for task in self.tasks:
            if task.key in listed or task.status not in ("new", "proposed", "question", "waiting", "stuck", "ready", "failed"):
                self._missing.pop(task.key, None)
                continue
            self._missing[task.key] = self._missing.get(task.key, 0) + 1
            if self._missing[task.key] >= 2:
                with self._lock:
                    task.status = "gone"

    def _fetch_details(self, task: StudyTask) -> None:
        item = self.classroom.details(task.assignment())
        if item.status != "assigned" and not task.rework:
            self._set(task, "submitted", submitted=task.submitted or "раніше", grade=item.grade)
            return
        if item.is_quiz or NOT_FOR_AGENT_RE.search(task.title):
            self._set(task, "skipped")
            due = f" Термін — {datetime.fromisoformat(task.due):%d.%m}." if task.due else ""
            self._tell(f"📋 {task.label} — схоже на тест чи захист. Агент таке не робить, це на тобі.{due}", task=task)
            return
        folder = Path(task.folder) if task.folder else self._new_folder(task)
        (folder / "source").mkdir(parents=True, exist_ok=True)
        (folder / "source" / "assignment.txt").write_text(item.text, encoding="utf-8")  # після повернення — з коментарями
        (folder / "source" / "attachments.json").write_text(
            json.dumps([asdict(a) for a in item.attachments], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        if (reason := self._ask_first(task)) and not task.started:
            self._set(task, "proposed", folder=str(folder))
            snippet = re.sub(r"\n{3,}", "\n\n", item.text).strip()[:700]
            due = f", до {datetime.fromisoformat(task.due):%d.%m}" if task.due else ""
            self._tell(
                f"🆕 {task.label}{due} ({reason}).\n\n{snippet}\n\nРобити?",
                buttons=[[("▶️ Роби", f"study:start:{task.number}"), ("❌ Не треба", f"study:no:{task.number}")]],
                task=task,
            )
            return
        self._queue_work(task, folder=str(folder))

    @property
    def works_root(self) -> Path:
        return self.folder / "Роботи"

    def _new_folder(self, task: StudyTask) -> Path:
        """Роботи/<курс>/<«Лб 3 Колекції»> — окрема тека на кожен курс, у ній — по теці на роботу."""
        kind, number, topic = work_kind(task.title)
        known = kind != "Завдання" and (number or topic)
        name = " ".join(part for part in (kind, number, topic) if part) if known else task.title
        base = self.works_root / course_dir(task.course) / dir_name(name)
        taken = {other.folder for other in self.tasks if other is not task and other.folder}
        folder = base
        if str(folder) in taken or folder.exists():
            folder = base.with_name(f"{base.name} ({task.number})")
        return folder

    def _ask_first(self, task: StudyTask) -> str:
        """Чому спершу спитати «робити?» (порожньо — братися одразу)."""
        study = self.config.study
        if self._rule(task.course).get("mode") == "ask":
            return "для цього предмета ти просив спершу питати"
        title = task.title.casefold()
        if any(word.strip() and word.strip().casefold() in title for word in study.ask_first):
            return "велика робота — питаю спершу"
        if study.start_mode == "ask":
            return "ти просив питати перед кожною роботою"
        return ""

    def _submit(self, task: StudyTask) -> None:
        files = [Path(path) for path in task.files if Path(path).exists()]
        if not files:
            self._set(task, "failed", error="файли роботи зникли з диска")
            self._tell(f"⚠️ {task.label}: не знайшов файлів роботи — не здаю.", task=task)
            return
        dry_run = self.state.get("real_submissions", 0) == 0 and not self.state.get("dry_run_done")
        result = self.classroom.submit(task.assignment(), files, dry_run=dry_run, remove=list(task.attached))
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
        self._set(
            task, "submitted", submitted=self.clock().isoformat(timespec="minutes"),
            attached=[path.name for path in files], grade="", grade_checked="",
        )
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

    def _grade_due(self, now: datetime) -> StudyTask | None:
        """Здана нами робота, яку давно не перевіряли (раз на добу, перші 30 днів після здачі)."""
        for task in self.tasks:
            if task.status != "submitted" or task.submitted in ("", "раніше"):
                continue
            if now - datetime.fromisoformat(task.submitted) > WATCH_GRADES_FOR:
                continue
            if task.grade_checked and now - datetime.fromisoformat(task.grade_checked) < timedelta(hours=24):
                continue
            return task
        return None

    def _watch_grade(self, task: StudyTask, now: datetime) -> None:
        item = self.classroom.details(task.assignment())
        changes = {"grade_checked": now.isoformat(timespec="minutes")}
        if item.status in ("graded", "returned"):
            grade = item.grade or ("оцінено" if item.status == "graded" else "повернуто")
            if grade != task.grade:
                changes["grade"] = grade
                link = task_url(task.assignment(), self._account())
                what = f"оцінка {item.grade}" if item.grade else grade
                self._tell(f"📬 {task.label}: {what}.\n{link}", task=task)
        self._set(task, task.status, **changes)

    # --- Нагадування -------------------------------------------------------------------

    def _remind(self, now: datetime) -> None:
        """Термін близько, а щось чекає на тебе (чи не готове) — нагадуємо один раз."""
        hours = self.config.study.remind_hours
        if not hours or (self._next_remind is not None and now < self._next_remind):
            return
        self._next_remind = now + timedelta(minutes=30)
        for task in self._ordered():
            if not task.due or task.reminded == task.due:
                continue
            due = datetime.fromisoformat(task.due)
            if not now <= due <= now + timedelta(hours=hours):
                continue
            when = f"{due:%d.%m %H:%M}"
            if task.status == "ready":
                self._ask(task, f"⏰ {task.label}: термін {when}, а готова робота чекає на твоє «так».")
            elif task.status == "question":
                self._tell(
                    f"⏰ {task.label}: термін {when}, а агент чекає на відповідь:\n\n{task.open_question}",
                    buttons=self._question_buttons(task), task=task,
                )
            elif task.status == "proposed":
                self._tell(
                    f"⏰ {task.label}: термін {when}. Робити?",
                    buttons=[[("▶️ Роби", f"study:start:{task.number}"), ("❌ Не треба", f"study:no:{task.number}")]],
                    task=task,
                )
            elif task.status == "skipped":
                self._tell(f"⏰ Нагадую: {task.label} — до {when}. Це на тобі.", task=task)
            elif task.status in ("stuck", "failed", "waiting"):
                self._tell(f"⏰ {task.label}: термін {when}, а робота не готова — {task.error[:300]}", task=task)
            else:
                continue
            with self._lock:
                task.reminded = task.due
            self._save()

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
            finally:
                self.activity = None

    def _busy(self, what: str, task: StudyTask | None = None, title: str = "") -> None:
        self.activity = {
            "what": what,
            "number": task.number if task else 0,
            "title": task.title if task else title,
            "attempt": task.attempts if task else 0,
            "since": self.clock().isoformat(timespec="seconds"),
        }

    def _instruction(self, base: str) -> str:
        return base + (ASK_INSTRUCTION if self.config.study.ask_questions else "")

    def _work(self, number: int) -> None:
        task = self.task(number)
        if task is None or task.status != "working":
            return
        if self.paused:
            self._set(task, "waiting", error=PAUSED)
            return
        folder = Path(task.folder)
        self._download_materials(folder)
        self._copy_examples(folder)
        (folder / "out").mkdir(exist_ok=True)
        (folder / "QUESTION.md").unlink(missing_ok=True)
        (folder / "TASK.md").write_text(self._task_text(task, folder), encoding="utf-8")
        reason = ""
        for _ in range(self.config.agent.attempts):
            task.attempts += 1
            instruction = self._instruction(WORK_INSTRUCTION) + (f" Попередня перевірка сказала: {reason}" if reason else "")
            try:
                self._busy("виконує", task)
                done = self.agent.run(instruction, folder)
                if self._stop.is_set():
                    return  # лишається «агент працює» — доробимо після перезапуску
                if self._asked(task, folder):
                    return
                if not done.ok:
                    reason = done.error
                    continue
                self._forget_verdict(folder)  # вердикт має бути саме цієї перевірки, не попередньої
                self._busy("перевіряє", task)
                checked = self.agent.run(self._instruction(CHECK_INSTRUCTION), folder)
                if self._stop.is_set():
                    return
                if self._asked(task, folder):
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
            f"Що заважає: {reason[:600]}\n\nМожеш відповісти на це повідомлення підказкою — і агент спробує з нею.",
            buttons=[[("🔁 Спробувати ще", f"study:retry:{task.number}"), ("❌ Не робити", f"study:no:{task.number}")]],
            task=task,
        )

    def _asked(self, task: StudyTask, folder: Path) -> bool:
        """Агент залишив питання (QUESTION.md) — пересилаємо тобі й чекаємо відповіді."""
        path = folder / "QUESTION.md"
        question = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if not question:
            return False
        path.unlink()
        with self._lock:
            task.questions.append({"q": question[:3000], "a": ""})
        self._set(task, "question")
        self._tell(
            f"❓ {task.label} — агент питає:\n\n{question[:2500]}\n\nВідповідай на це повідомлення — і він продовжить.",
            buttons=self._question_buttons(task),
            task=task,
        )
        return True

    @staticmethod
    def _question_buttons(task: StudyTask) -> list:
        return [[("🤷 Виріши сам", f"study:decide:{task.number}"), ("❌ Не робити", f"study:no:{task.number}")]]

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

    def _copy_examples(self, folder: Path) -> None:
        """Зразки твоїх робіт — у теку завдання (не більше 30 МБ), щоб агент оформлював так само."""
        source = Path(self.config.study.examples).expanduser() if self.config.study.examples else None
        target = folder / "examples"
        if source is None or not source.is_dir() or target.exists():
            return
        target.mkdir()
        total = 0
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in EXAMPLE_SUFFIXES:
                continue
            size = path.stat().st_size
            if total + size > EXAMPLES_LIMIT:
                break
            shutil.copy2(path, target / path.name)
            total += size

    def _account(self) -> str:
        return self.config.study.account or self.config.join.meet_account

    def _student(self) -> dict:
        study, schedule = self.config.study, self.config.schedule
        name = study.full_name or self.config.me.name
        return {
            "name": name,
            "group": study.group or schedule.group,
            "year": study.year or schedule.course or 0,
            "university": study.university or UNIVERSITY_NAMES.get(schedule.university, ""),
        }

    def report_name(self, task: StudyTask) -> str:
        student = self._student()
        name = student["name"]
        kind, number, _ = work_kind(task.title)
        fields = {
            "short": short_name(name),
            "surname": name.split()[0] if name.split() else "Студент",
            "name": name,
            "group": student["group"],
            "title": task.title[:60],
            "course": course_dir(task.course)[:40],
            "kind": kind,
            "n": number,
            "work": f"{kind}{number}" if number else task.title[:60],  # «Лб3», «Пр1.2», а без номера — назва
            "number": task.number,
        }
        return f"{file_safe(self.config.study.file_name.format(**fields))}.{self.config.study.file_format}"

    def _previous_works(self, task: StudyTask) -> list[str]:
        """Попередні роботи з цього ж курсу — ця лаба часто продовжує попередню, а оформлення має бути однаковим."""
        done = [
            other for other in self.tasks
            if other is not task and other.course == task.course and other.folder
            and other.status in ("ready", "approved", "submitted", "returned", "gone") and (Path(other.folder) / "out").is_dir()
        ]
        return [f"- «{other.title}»: {Path(other.folder) / 'out'}" for other in sorted(done, key=lambda t: -t.number)[:8]]

    def _task_text(self, task: StudyTask, folder: Path) -> str:
        study, now = self.config.study, self.clock()
        student = self._student()
        name, group = student["name"], student["group"]
        rule = self._rule(task.course)
        notes = self._course_notes(task.course)
        materials = sorted(p.name for p in (folder / "materials").glob("*")) if (folder / "materials").exists() else []
        examples = sorted(p.name for p in (folder / "examples").glob("*")) if (folder / "examples").exists() else []
        previous = self._previous_works(task)
        feedback = "\n".join(f"- {comment}" for comment in task.feedback)
        answered = [item for item in task.questions if item.get("a")]
        due = f"{datetime.fromisoformat(task.due):%d.%m.%Y %H:%M}" if task.due else "не вказано"
        unknown_hint = (
            "спитай студента (QUESTION.md)" if study.ask_questions else "вибери найімовірніше й напиши про це в RESULT.md"
        )

        sections = [
            f"""# Завдання з Google Classroom

Курс: {task.course}
Завдання: {task.title}
У списку завдань: {task.listing}
Термін: {due}

## Умова (текст сторінки завдання в Classroom; там може бути зайвий текст інтерфейсу — ігноруй його)

{(folder / "source" / "assignment.txt").read_text(encoding="utf-8")}

## Вкладені матеріали — у теці materials/

{chr(10).join(f"- {m}" for m in materials) or "- (немає)"}

## Студент

- ПІБ: {name}
- Група: {group}
- Курс: {student["year"] or "визнач з назви групи чи матеріалів"}
- Університет: {student["university"] or "візьми з матеріалів курсу"}
- Факультет / інститут: {study.faculty or "візьми з матеріалів курсу, якщо потрібен на титулці"}
- Кафедра: {study.department or "візьми з матеріалів курсу чи попередніх робіт"}
- Спеціальність: {study.specialty or "візьми з матеріалів курсу чи попередніх робіт; якщо ніде немає — пропусти цей рядок"}
- Номер залікової книжки: {study.student_id or "невідомо"}
- Місто й рік: {study.city} – {now.year}

## Варіант

- Номер у списку групи: {self.list_number or "невідомо"}
- Загальне правило студента: {study.variant_rule or "якщо в умові не сказано інакше — варіант дорівнює номеру в списку групи"}"""
            + (f"\n- Для цього предмета студент сам вказав варіант: **{rule['variant']}** — використовуй саме його." if rule.get("variant") else "")
            + f"""
- Якщо в умові варіант рахується інакше (за формулою, остачею від ділення, літерою прізвища, номером
  залікової) — порахуй за умовою й коротко покажи розрахунок у звіті. Якщо даних не вистачає — {unknown_hint}.""",
        ]
        if study.style.strip():
            sections.append(f"## Побажання студента до всіх робіт — дотримуйся\n\n{study.style.strip()}")
        if rule.get("notes", "").strip():
            sections.append(f"## Побажання до робіт з цього предмета (викладач, студент) — дотримуйся\n\n{rule['notes'].strip()}")
        sections.append(
            "## Що казали на парах з цього предмета (вижимки лекцій; тут бувають побажання викладача до робіт)\n\n"
            + (notes or "(вижимок ще немає)")
        )
        if previous:
            sections.append(
                "## Попередні роботи з цього предмета (можна читати)\n\nЯкщо ця робота продовжує попередню (той самий "
                "проєкт, код, база даних) — бери за основу звідти. Оформлення тримай однаковим.\n\n" + "\n".join(previous)
            )
        if examples:
            sections.append(
                "## Зразки робіт студента — у теці examples/\n\nОформлюй так само: титулка, шрифти, структура, стиль.\n\n"
                + "\n".join(f"- {name}" for name in examples)
            )
        if answered:
            sections.append(
                "## Твої питання до студента і його відповіді — обов'язково врахуй\n\n"
                + "\n\n".join(f"Питання: {item['q']}\nВідповідь: {item['a']}" for item in answered)
            )
        if feedback:
            sections.append(f"## Зауваження студента до попередньої версії — обов'язково врахуй\n\n{feedback}")

        ask = (
            """6. Дрібниці вирішуй сам і згадай про них у RESULT.md. Але якщо без відповіді студента роботу НЕ МОЖНА
   зробити правильно (незрозуміло, який варіант; умова суперечлива; потрібен доступ до його акаунта, пристрою
   чи даних, яких немає; потрібне те, що може зробити лише він) — не вгадуй: запиши коротке питання в
   QUESTION.md (1–3 речення; якщо можна — з варіантами відповіді) і заверши роботу. Студент відповість,
   і тебе запустять знову з його відповіддю в цьому файлі. Не питай про те, що вже є в цьому файлі."""
            if study.ask_questions
            else """6. Не став студентові питань: якщо чогось бракує, вибери найімовірніший варіант і чесно опиши це
   в RESULT.md."""
        )
        sections.append(f"""## Як виконати

1. Виконай роботу ПОВНІСТЮ сам. Код має справді запускатися — запусти його. Скріншоти мають бути справжніми
   (зніми їх із запущеної програми чи консолі), відео — якщо умова його вимагає (запиши екран симуляції).
   Якщо потрібна програма з інтерфейсом чи вебсервіс (Tinkercad, Excel тощо) — скористайся браузером та
   інструментами, які маєш; якщо щось недоступне — знайди рівноцінний спосіб і чесно опиши його в RESULT.md.
2. Звіт — мовою: {study.language}, як у студента в попередніх роботах: титульна сторінка (університет;
   факультет і кафедра, якщо відомі; дисципліна «…»; «Звіт з лабораторної/практичної роботи №…»; тема;
   «Виконав: студент {student["year"] or "…"} курсу, групи {group}, {name}»; спеціальність, якщо відома;
   «{study.city} – {now.year}»), далі «Мета», «Варіант» (якщо є), «Хід роботи» з поясненнями й
   скріншотами, «Висновок». Пиши просто й по суті, як студент.
3. Поклади в out/ ЛИШЕ те, що треба здати: звіт `{self.report_name(task)}` і, якщо треба, код (архів чи
   файли), відео тощо. Чернетки й службові файли — поза out/. Кожен файл до 45 МБ.
4. Запиши RESULT.md: коротко, що зроблено, які файли в out/ і що в кожному, як перевірити, на що звернути
   увагу. Без води.
5. Запиши DEFENSE.md — шпаргалку до захисту: суть роботи простими словами, як воно працює, ключові
   поняття, і 8–12 імовірних питань викладача з короткими відповідями.
{ask}
""")
        return "\n\n".join(sections)

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
        folder = self.notes_root / dir_name(subject) / f"{when:%Y-%m-%d %H-%M}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "LECTURE.md").write_text(
            f"""# Вижимка пари

Пара: {title}, {when:%d.%m.%Y %H:%M}, викладач: {teacher or "?"}

Нижче — автоматична розшифровка пари (з помилками розпізнавання; «[?]» — розпізнано невпевнено) і чат.
Зроби для студента, якого не було на парі, детальну вижимку мовою «{self.config.study.language}» у SUMMARY.md:

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
        if self.paused:
            log.info("Вижимку «%s» не роблю: агент на паузі (розшифровка — у %s)", title, folder)
            return
        try:
            self._busy("робить вижимку", title=title)
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
            self.notifier.document(
                summary.encode("utf-8"), f"Вижимка_{safe_name(subject)}_{when:%d.%m}.md", title,
                on_sent=self._mine, silent=self._silent(),
            )

    @property
    def notes_root(self) -> Path:
        return self.folder / "Конспекти"

    def _course_notes(self, course: str) -> str:
        root = self.notes_root
        if not root.exists():
            return ""
        subjects = {path.name: path for path in root.iterdir() if path.is_dir()}
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
            if task.status == "question":
                return self.act(task.number, "answer", text.strip())
            if task.status == "proposed" and low in STARTS:
                return self.act(task.number, "start")
            if low in SHORT_ANSWERS:
                return "Натисни кнопку під останнім повідомленням про це завдання — так точно не зроблю не те."
            if task.status in ("ready", "stuck", "failed", "returned"):
                return self.act(task.number, "redo", text.strip())
            return f"{task.label} зараз: {STATUSES[task.status]}. У чат пари це не пересилаю."
        if reply_to and reply_to in self.state.get("own_messages", []):
            return (
                "Це повідомлення про навчання — у чат пари його не пересилаю. Щоб переробити роботу, "
                "відповідай на повідомлення з нею."
            )
        if low in ("завдання", "/tasks", "навчання"):
            return self.overview()
        if low in ("питання", "/questions"):
            return self.questions_overview()
        if low in ("перевір", "перевір classroom", "/check"):
            self.check_now()
            return "Перевірю Classroom найближчим часом (між парами)."
        if low in ("пауза", "/pause"):
            return self.pause(True)
        if low in ("продовжуй", "продовжити", "/resume"):
            return self.pause(False)
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
            if task.status in ("approved", "ready", "stuck", "failed", "proposed", "question", "returned", "waiting", "new"):
                self._set(task, "declined")
            return f"Добре, {task.label} не роблю й не здаю." + (f" Файли лишаються в {task.folder}." if task.folder else "")
        if action == "start":
            if task.status != "proposed":
                return f"{task.label} зараз: {STATUSES[task.status]}."
            task.started = True
            self._queue_work(task)
            return f"Беруся за {task.label}. Пришлю, коли буде повністю готово."
        if action in ("answer", "decide"):
            if task.status != "question":
                return f"{task.label} зараз нічого не питає."
            answer = DECIDE if action == "decide" or not comment else comment
            with self._lock:
                task.questions[-1]["a"] = answer
            self._queue_work(task)
            return f"Передав агентові: «{answer[:200]}». Продовжує {task.label}."
        if action == "redo":
            if task.status == "question":
                return self.act(number, "answer", comment)
            if not comment:
                self._ask_feedback(task)
                return ""
            with self._lock:
                task.feedback.append(comment)
            if task.status == "returned":
                return self.act(number, "rework")
            self._queue_work(task)
            return f"Переробляю {task.label} з урахуванням: «{comment[:200]}». Пришлю, коли буде повністю готово."
        if action == "rework":
            if task.status != "returned":
                return f"{task.label} зараз: {STATUSES[task.status]}."
            with self._lock:
                task.feedback.append(
                    "Викладач повернув роботу. Його коментарі й оцінка — в умові вище (сторінку завдання перечитано "
                    "після повернення). Виправ усе, на що він вказав."
                )
            self._set(task, "new", rework=True, started=True)  # спершу перечитати сторінку з коментарями
            return f"Перечитаю {task.label} з коментарями викладача й перероблю."
        if action == "retry":
            if not task.folder:  # сторінку завдання ще не прочитали — почнемо з неї
                self._page_failures.pop(task.key, None)
                self._set(task, "new")
                return f"Ще раз відкрию {task.label} у Classroom між парами."
            self._queue_work(task)
            return f"Пробую ще раз: {task.label}."
        if action == "back":
            if task.status != "submitted":
                return f"{task.label} не здано — скасовувати нічого."
            self._set(task, "returning")
            return f"Заберу {task.label} з Classroom між парами."
        return "Не зрозумів."

    def overview(self) -> str:
        active = [task for task in self._ordered() if task.status not in DONE]
        if not active:
            return "📚 Незданих завдань немає (або Classroom ще не перевіряв)."
        first = [task for task in active if task.status in WAITING_ON_YOU] + [t for t in active if t.status not in WAITING_ON_YOU]
        lines = ["📚 Завдання" + (" (агент на паузі)" if self.paused else "") + ":"]
        for task in first[:25]:
            due = f", до {datetime.fromisoformat(task.due):%d.%m}" if task.due else ""
            lines.append(f"{'👉 ' if task.status in WAITING_ON_YOU else ''}{task.label}{due} — {STATUSES[task.status]}")
        return "\n".join(lines)

    def questions_overview(self) -> str:
        open_ones = [task for task in self.tasks if task.status == "question"]
        if not open_ones:
            return "Агент зараз нічого не питає."
        for task in open_ones:  # кожне питання — окремим повідомленням, щоб на нього можна було відповісти
            self._tell(f"❓ {task.label}:\n\n{task.open_question}", buttons=self._question_buttons(task), task=task)
        return ""

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "tasks": [self._task_view(task) for task in self._ordered()],  # найтерміновіші першими, як і агент
                "activity": self.activity,
                "last_check": self.state.get("last_check", ""),
                "last_error": self.state.get("last_error", ""),
                "list_number": self.list_number,
                "agent_problem": self.agent.problem(),
                "agent_used": self.agent.used_today(),
                "agent_limit": self.config.agent.daily_limit,
                "dry_run_done": bool(self.state.get("dry_run_done")),
                "paused": self.paused,
                "folder": str(self.folder),
                "works_folder": str(self.works_root),
                "notes_folder": str(self.notes_root),
                "waiting_on_you": sum(task.status in WAITING_ON_YOU for task in self.tasks),
            }

    def attention(self) -> dict:
        """Коротко для «Сьогодні»: що чекає на тебе і що робить агент."""
        with self._lock:
            waiting = [task for task in self._ordered() if task.status in WAITING_ON_YOU]
            return {
                "waiting": [
                    {"number": task.number, "title": task.title, "status": task.status, "due": task.due} for task in waiting[:6]
                ],
                "waiting_count": len(waiting),
                "activity": self.activity,
                "paused": self.paused,
                "problem": self.agent.problem(),
            }

    def _task_view(self, task: StudyTask) -> dict:
        return {
            **asdict(task),
            "status_text": STATUSES.get(task.status, task.status),
            "question": task.open_question,
            "url": task_url(task.assignment(), self._account()),
        }

    def task_details(self, number: int) -> dict | None:
        """Усе про одне завдання для вікна: умова, що зроблено, шпаргалка, файли."""
        task = self.task(number)
        if task is None:
            return None
        folder = Path(task.folder) if task.folder else None

        def read(name: str, limit: int = 80_000) -> str:
            path = folder / name if folder else None
            return path.read_text(encoding="utf-8", errors="replace")[:limit] if path and path.is_file() else ""

        def files(paths) -> list[dict]:
            return [{"name": path.name, "size": path.stat().st_size} for path in paths if path.is_file()]

        materials = sorted((folder / "materials").iterdir()) if folder and (folder / "materials").is_dir() else []
        return {
            **self._task_view(task),
            "assignment": read("source/assignment.txt"),
            "result": STATUS_RE.sub("", read("RESULT.md")).strip(),
            "defense": read("DEFENSE.md"),
            "files_info": files(Path(path) for path in task.files),
            "materials": files(materials),
            "report_name": self.report_name(task),
        }

    def check_now(self) -> None:
        self.state["last_check"] = ""
        self._retry_at = None

    # --- Повідомлення ------------------------------------------------------------------

    def _silent(self) -> bool:
        study = self.config.study
        return in_quiet_hours(self.clock().hour, study.quiet_from, study.quiet_to)

    def _ask(self, task: StudyTask, text: str, files: list[Path] = (), extra: list[Path] = (), photo: bytes | None = None) -> None:
        buttons = [
            [("✅ Так, здати", f"study:yes:{task.number}:{task.version}"), ("✏️ Переробити", f"study:redo:{task.number}")],
            [("❌ Не здавати", f"study:no:{task.number}")],
        ]
        remember = lambda message_id: self._remember(task, message_id)  # noqa: E731
        silent = self._silent()
        if photo:
            self.notifier.photo(photo, f"{task.label}", on_sent=remember, silent=silent)
        too_big = []
        for path in list(files) + [p for p in extra if p.exists()]:
            if path.stat().st_size > TELEGRAM_FILE_LIMIT:
                too_big.append(f"{path.name} ({path.stat().st_size // 2**20} МБ)")
                continue
            caption = "📒 Шпаргалка до захисту" if path.name == "DEFENSE.md" else path.name
            self.notifier.document(path.read_bytes(), path.name, caption, on_sent=remember, silent=silent)
        if too_big:
            text += "\n\n📦 Завеликі для Telegram — переглянь у вікні програми («Навчання» → «📂 Файли»): " + ", ".join(too_big)
        self.notifier.ask(text, buttons, urgent=False, on_sent=remember, silent=silent)

    def _ask_feedback(self, task: StudyTask) -> None:
        def remember(message_id: int) -> None:
            self._awaiting_feedback[message_id] = task.key
            self._remember(task, message_id)

        self.notifier.ask(
            f"✏️ Що змінити в {task.label}? Відповідай на це повідомлення.", [], urgent=False, on_sent=remember,
            silent=self._silent(),
        )

    def _remember(self, task: StudyTask, message_id: int) -> None:
        with self._lock:
            task.messages = (task.messages + [message_id])[-30:]
        self._save()

    def _mine(self, message_id: int) -> None:
        """Наше повідомлення не про конкретне завдання (вижимка, огляд) — щоб відповідь на нього не пішла в чат пари."""
        with self._lock:
            self.state["own_messages"] = (self.state.get("own_messages", []) + [message_id])[-300:]
        self._save()

    def _tell(self, text: str, photo: bytes | None = None, buttons=None, task: StudyTask | None = None) -> None:
        remember = (lambda message_id: self._remember(task, message_id)) if task is not None else self._mine
        silent = self._silent()
        if photo:
            self.notifier.photo(photo, text[:200], on_sent=remember, silent=silent)
        self.notifier.ask(text, buttons or [], urgent=False, on_sent=remember, silent=silent)
