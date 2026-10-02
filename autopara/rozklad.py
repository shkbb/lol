"""Клієнт розкладу ДУІКТ (https://e-rozklad.duikt.edu.ua).

Публічного JSON API в сайту немає: це Yii-форма з CSRF-токеном.
Вибір факультету / курсу / групи — POST тієї ж форми з ``?type=0`` (у відповіді
приходить HTML з оновленими <select>), а сам розклад — POST з ``?type=1``:
у відповіді він вшитий у <script> як ``var events = {...}``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, tzinfo
from time import sleep

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = "https://e-rozklad.duikt.edu.ua"
SOURCE_DATE = "%d.%m.%Y"
EVENTS_MARKER = "var events ="
FILTER_FORM_RE = re.compile(r"id\s*=\s*['\"]filter-form['\"]", re.I)
URL_RE = re.compile(r"https?://[^\s<>\"']+")
RETRYABLE = {408, 429, 500, 502, 503, 504}

# Поля форми для кожної сторінки сайту (порядок як у браузері).
FORM_FIELDS = {
    "group": ("facultyId", "course", "groupId"),
    "student": ("facultyId", "course", "groupId", "studentId"),
}


class RozkladError(Exception):
    """Сайт розкладу недоступний або повернув щось неочікуване."""


@dataclass(frozen=True)
class Option:
    id: int
    name: str


@dataclass(frozen=True)
class Lesson:
    day: date
    start: time
    end: time
    subject: str
    type: str = ""
    teacher: str = ""
    room: str = ""
    groups: str = ""
    link: str | None = None  # посилання, яке вписали прямо в розклад (поле info)
    notice: str = ""
    short_subject: str = ""  # скорочена назва з розкладу, наприклад «ЯТПЗ»

    def starts_at(self, tz: tzinfo) -> datetime:
        return datetime.combine(self.day, self.start, tz)

    def ends_at(self, tz: tzinfo) -> datetime:
        return datetime.combine(self.day, self.end, tz)

    @property
    def title(self) -> str:
        return f"{self.subject} ({self.type})" if self.type else self.subject


@dataclass(frozen=True)
class Target:
    """Чий розклад тягнемо: група, або конкретний студент групи (з урахуванням підгруп)."""

    faculty_id: int
    course: int
    group_id: int
    group_name: str = ""
    faculty_name: str = ""
    student_id: int | None = None
    student_name: str = ""
    uid: str = ""  # ідентифікатор групи там, де він не числовий набір (КПІ)


# --- Розбір HTML -------------------------------------------------------------


def _form(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(id="filter-form") is None:
        raise RozkladError("у відповіді немає форми розкладу (сайт змінився або лежить?)")
    return soup


def parse_csrf(html: str) -> str:
    field = _form(html).find("input", attrs={"name": "_csrf-frontend"})
    value = (field.get("value") or "").strip() if field else ""
    if not value:
        raise RozkladError("у формі розкладу немає CSRF-токена")
    return value


def parse_options(html: str, field: str) -> list[Option]:
    select = _form(html).find("select", id=f"timetableform-{field.lower()}")
    if select is None:
        raise RozkladError(f"у формі розкладу немає списку {field}")
    options = []
    for option in select.find_all("option"):
        value = (option.get("value") or "").strip()
        if not value:
            continue  # плейсхолдер «Оберіть...»
        try:
            option_id = int(value)
        except ValueError:
            raise RozkladError(f"некоректний ID «{value}» у списку {field}") from None
        options.append(Option(option_id, option.get_text(" ", strip=True)))
    return options


def extract_events(html: str) -> list[dict]:
    if not FILTER_FORM_RE.search(html):
        raise RozkladError("у відповіді немає форми розкладу (сайт змінився або лежить?)")
    marker = html.find(EVENTS_MARKER)
    if marker < 0:
        raise RozkladError("у відповіді немає даних розкладу (var events)")
    start = marker + len(EVENTS_MARKER)
    while start < len(html) and html[start].isspace():
        start += 1
    if start >= len(html) or html[start] not in "{[":
        raise RozkladError("дані розкладу (var events) — не JSON-колекція")
    raw = html[start : _balanced_end(html, start) + 1]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RozkladError(f"некоректний JSON розкладу: {e}") from e
    items = data.values() if isinstance(data, dict) else data
    return [item for item in items if isinstance(item, dict)]


def _balanced_end(text: str, start: int) -> int:
    """Індекс дужки, що закриває колекцію з ``text[start]`` (з урахуванням рядків)."""
    open_char = text[start]
    close_char = "}" if open_char == "{" else "]"
    depth = 0
    quoted = escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
    raise RozkladError("JSON розкладу обірвався")


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def link_from_html(fragment: str | None) -> str | None:
    """Перше http(s)-посилання з HTML-фрагмента або простого тексту."""
    if not fragment:
        return None
    soup = BeautifulSoup(fragment, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.startswith(("http://", "https://")):
            return href
    match = URL_RE.search(soup.get_text(" "))
    return match.group(0).rstrip(".,;)") if match else None


def to_lesson(event: dict) -> Lesson:
    try:
        day = datetime.strptime(event["date"], SOURCE_DATE).date()
        start = datetime.strptime(event["timeStart"], "%H:%M").time()
        end = datetime.strptime(event["timeEnd"], "%H:%M").time()
    except (KeyError, TypeError, ValueError) as e:
        raise RozkladError(f"некоректне заняття в розкладі: {e!r}") from e
    subject = _clean(event.get("disciplineFullName")) or _clean(event.get("disciplineShortName"))
    room = _clean(event.get("classroom"))
    if room.startswith("ауд."):
        room = room[len("ауд.") :].strip()
    return Lesson(
        day=day,
        start=start,
        end=end,
        subject=subject,
        type=_clean(event.get("typeStr")),
        teacher=_clean(event.get("teachersNameFull")) or _clean(event.get("teachersName")),
        room=room,
        groups=_clean(event.get("groups")),
        link=link_from_html(event.get("info")) or link_from_html(event.get("notice")),
        notice=_clean(event.get("notice")),
        short_subject=_clean(event.get("disciplineShortName")),
    )


# --- Пошук групи / студента --------------------------------------------------

# Латинські літери, схожі на кириличні: «TЦP-32», набране з англійської розкладки.
_LOOKALIKES = str.maketrans("abcehikmoptxy", "авсенікмортху")


def normalize_name(name: str) -> str:
    folded = re.sub(r"[\s‐-―_-]+", "", name.casefold())
    return folded.translate(_LOOKALIKES)


def course_hint(group_name: str) -> int | None:
    """Номер курсу зазвичай — перша цифра в назві групи: ТЦР-32 → 3."""
    match = re.search(r"\d", group_name)
    return int(match.group(0)) if match else None


def _name_words(name: str) -> list[str]:
    return name.casefold().replace("’", "'").replace("ʼ", "'").split()


def find_student(students: list[Option], query: str) -> Option:
    wanted = _name_words(query)
    found = [
        s
        for s in students
        if all(any(word.startswith(q) for word in _name_words(s.name)) for q in wanted)
    ]
    if len(found) == 1:
        return found[0]
    if not found:
        raise RozkladError(f"студента «{query}» у групі не знайдено")
    names = ", ".join(s.name for s in found)
    raise RozkladError(f"«{query}» підходить кільком студентам: {names} — уточни ім'я")


# --- HTTP-клієнт -------------------------------------------------------------


class RozkladClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        session: requests.Session | None = None,
        timeout: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "autopara/0.1 (DUIKT timetable)", "Accept-Language": "uk-UA,uk;q=0.9"}
        )
        self.timeout = timeout
        self._csrf: dict[str, str] = {}
        self._groups: list[Target] | None = None

    # Довідники

    def faculties(self) -> list[Option]:
        return parse_options(self._open("group"), "facultyId")

    def courses(self, faculty_id: int) -> list[int]:
        html = self._post("group", 0, {"facultyId": faculty_id})
        return [o.id for o in parse_options(html, "course") if o.id > 0]

    def groups(self, faculty_id: int, course: int) -> list[Option]:
        html = self._post("group", 0, {"facultyId": faculty_id, "course": course})
        return parse_options(html, "groupId")

    def students(self, faculty_id: int, course: int, group_id: int) -> list[Option]:
        values = {"facultyId": faculty_id, "course": course, "groupId": group_id}
        return parse_options(self._post("student", 0, values), "studentId")

    def all_groups(self) -> list[Target]:
        """Усі групи всіх факультетів і курсів (для пошуку у вікні програми; кешується)."""
        if self._groups is None:
            groups = []
            for faculty in self.faculties():
                for course in self.courses(faculty.id):
                    groups += [
                        Target(faculty.id, course, group.id, group_name=group.name, faculty_name=faculty.name)
                        for group in self.groups(faculty.id, course)
                    ]
            self._groups = groups
        return self._groups

    def search_groups(self, query: str, limit: int = 30) -> list[Target]:
        wanted = normalize_name(query)
        return [group for group in self.all_groups() if wanted in normalize_name(group.group_name)][:limit]

    def find_group(self, name: str, faculty_id: int | None = None, course: int | None = None) -> Target:
        wanted = normalize_name(name)
        faculties = [Option(faculty_id, "")] if faculty_id else self.faculties()

        def search(fac: Option, c: int) -> Target | None:
            try:
                groups = self.groups(fac.id, c)
            except RozkladError:
                return None
            for group in groups:
                if normalize_name(group.name) == wanted:
                    return Target(fac.id, c, group.id, group_name=group.name, faculty_name=fac.name)
            return None

        # Спершу пробуємо «очевидний» курс на кожному факультеті, потім усе підряд.
        first = course or course_hint(name)
        if first:
            for fac in faculties:
                if found := search(fac, first):
                    return found
        if not course:
            for fac in faculties:
                for c in self.courses(fac.id):
                    if c != first and (found := search(fac, c)):
                        return found
        raise RozkladError(f"групу «{name}» на сайті розкладу не знайдено")

    def resolve_student(self, target: Target, student: str) -> Target:
        found = find_student(self.students(target.faculty_id, target.course, target.group_id), student)
        return Target(
            target.faculty_id,
            target.course,
            target.group_id,
            group_name=target.group_name,
            faculty_name=target.faculty_name,
            student_id=found.id,
            student_name=found.name,
        )

    # Розклад

    def schedule(self, target: Target, start: date, end: date) -> list[Lesson]:
        values = {"facultyId": target.faculty_id, "course": target.course, "groupId": target.group_id}
        kind = "group"
        if target.student_id:
            kind = "student"
            values["studentId"] = target.student_id
        html = self._post(kind, 1, values, dates=(start, end))
        lessons = [to_lesson(event) for event in extract_events(html)]
        return sorted(lessons, key=lambda lesson: (lesson.day, lesson.start, lesson.subject))

    # Мережа

    def _open(self, kind: str) -> str:
        params = {"type": 0} if kind == "student" else None
        html = self._text(self._send("GET", f"/time-table/{kind}", params=params))
        self._csrf[kind] = parse_csrf(html)
        return html

    def _post(
        self,
        kind: str,
        type_: int,
        values: dict[str, int],
        dates: tuple[date, date] | None = None,
        retry: bool = True,
    ) -> str:
        if kind not in self._csrf:
            self._open(kind)
        data = {"_csrf-frontend": self._csrf[kind]}
        for name in FORM_FIELDS[kind]:
            value = values.get(name)
            data[f"TimeTableForm[{name}]"] = "" if value is None else str(value)
        if dates:
            data["TimeTableForm[dateStart]"] = dates[0].strftime(SOURCE_DATE)
            data["TimeTableForm[dateEnd]"] = dates[1].strftime(SOURCE_DATE)
            data["TimeTableForm[indicationDays]"] = "5"
            data["time-table-type"] = "1"
        response = self._send("POST", f"/time-table/{kind}", params={"type": type_}, data=data)
        # Yii відповідає 400/403, коли CSRF-токен або сесія протухли: беремо нові й пробуємо ще раз.
        if response.status_code in (400, 403) and retry:
            log.debug("CSRF/сесію відхилено (%s), оновлюю", response.status_code)
            self._csrf.pop(kind, None)
            return self._post(kind, type_, values, dates, retry=False)
        html = self._text(response)
        self._csrf[kind] = parse_csrf(html)
        return html

    def _send(self, method: str, path: str, **kwargs) -> requests.Response:
        url = self.base_url + path
        for attempt in range(3):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as e:
                if attempt == 2:
                    raise RozkladError(f"сайт розкладу недоступний: {e}") from e
            else:
                if response.status_code not in RETRYABLE or attempt == 2:
                    return response
            sleep(0.5 * 2**attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _text(response: requests.Response) -> str:
        if not 200 <= response.status_code < 300:
            raise RozkladError(f"сайт розкладу відповів HTTP {response.status_code}")
        return response.text
