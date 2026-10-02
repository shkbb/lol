"""Розклад КПІ ім. Ігоря Сікорського (https://schedule.kpi.ua).

Той самий публічний API, що й в офіційного сайту розкладу (github.com/kpi-ua/schedule.kpi.ua):
- ``/group/all`` — усі групи: ``[{id, name, faculty}]``;
- ``/schedule/lessons?groupId=…`` — розклад групи на два тижні:
  ``{scheduleFirstWeek: [{day: "Пн", pairs: [...]}, ...], scheduleSecondWeek: [...]}``;
- ``/time/current`` — ``{currentWeek: 1 | 2, ...}``: який з двох тижнів іде зараз.

Ідентифікатор групи — те саме число, що в посиланні ``schedule.kpi.ua/?groupId=4383``.
Пара: ``{name, time, tag: lec|lab|prac, type, lecturer: {id, name}, location: {uri, title}, dates: [...]}``.
Порожній ``dates`` — пара щотижня свого тижня; непорожній — лише в ці дати.
``location.uri`` буває посиланням на онлайн-пару.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta

import requests

from .rozklad import Lesson, RozkladError, Target, normalize_name

log = logging.getLogger(__name__)

API = "https://api.campus.kpi.ua"
DAYS = ["Пн", "Вв", "Ср", "Чт", "Пт", "Сб"]
TYPES = {"lec": "Лк", "lab": "Лб", "prac": "Пз"}
PAIR = timedelta(minutes=95)  # пара в КПІ: 2 × 45 хв + 5 хв перерви


def group_from_link(text: str) -> str | None:
    """«https://schedule.kpi.ua/?groupId=4383» → «4383»: так друзям простіше, ніж шукати групу за назвою."""
    match = re.search(r"[?&]groupId=([\w-]+)", text or "")
    return match.group(1) if match else None


def parse_time(text: str) -> time | None:
    match = re.match(r"\s*(\d{1,2})[.:](\d{2})", text or "")
    return time(int(match.group(1)), int(match.group(2))) if match else None


def parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for pattern, size in (("%Y-%m-%d", 10), ("%d.%m.%Y", 10)):
        try:
            return datetime.strptime(text[:size], pattern).date()
        except ValueError:
            continue
    return None


def to_lesson(pair: dict, day: date) -> Lesson | None:
    start = parse_time(pair.get("time", ""))
    if start is None:
        log.warning("КПІ: незрозумілий час пари %r — пропускаю", pair.get("time"))
        return None
    end = (datetime.combine(day, start) + PAIR).time()
    location = pair.get("location") or {}
    uri = (location.get("uri") or "").strip()
    raw_type = (pair.get("type") or "").strip()
    return Lesson(
        day=day,
        start=start,
        end=end,
        subject=(pair.get("name") or "").strip(),
        type=TYPES.get(pair.get("tag", ""), raw_type),
        teacher=((pair.get("lecturer") or {}).get("name") or "").strip(),
        room=(location.get("title") or "").strip(),
        link=uri if uri.startswith(("http://", "https://")) else None,
        notice=raw_type,
    )


class KpiClient:
    def __init__(self, base_url: str = API, session: requests.Session | None = None, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "autopara (KPI timetable)", "Accept": "application/json"})
        self.timeout = timeout
        self._groups: list[Target] | None = None

    def _get(self, path: str, **params):
        try:
            response = self.session.get(self.base_url + path, params=params or None, timeout=self.timeout)
        except requests.RequestException as e:
            raise RozkladError(f"розклад КПІ недоступний: {e}") from e
        if not 200 <= response.status_code < 300:
            raise RozkladError(f"розклад КПІ відповів HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as e:
            raise RozkladError("розклад КПІ відповів не JSON") from e

    # Групи

    def all_groups(self) -> list[Target]:
        if self._groups is None:
            rows = self._get("/group/all")
            if not isinstance(rows, list):
                raise RozkladError("розклад КПІ: неочікуваний список груп")
            self._groups = [
                Target(
                    0, 0, 0, group_name=row["name"].strip(), faculty_name=(row.get("faculty") or "").strip(), uid=str(row["id"])
                )
                for row in rows
                if row.get("id") and row.get("name")
            ]
        return self._groups

    def search_groups(self, query: str, limit: int = 30) -> list[Target]:
        wanted = normalize_name(query)
        return [group for group in self.all_groups() if wanted in normalize_name(group.group_name)][:limit]

    def group_by_uid(self, uid: str) -> Target:
        found = next((group for group in self.all_groups() if group.uid == str(uid)), None)
        return found or Target(0, 0, 0, uid=str(uid))

    def find_group(self, name: str, *_ignored) -> Target:
        if uid := group_from_link(name):
            return self.group_by_uid(uid)
        wanted = normalize_name(name)
        found = [group for group in self.all_groups() if normalize_name(group.group_name) == wanted]
        if not found:
            raise RozkladError(f"групу «{name}» у розкладі КПІ не знайдено")
        if len(found) > 1:
            faculties = ", ".join(group.faculty_name or "?" for group in found)
            raise RozkladError(f"груп «{name}» у КПІ кілька ({faculties}) — вибери свою у вікні програми")
        return found[0]

    # Розклад

    def current_week(self) -> int:
        week = self._get("/time/current").get("currentWeek")
        if week not in (1, 2):
            raise RozkladError(f"розклад КПІ: незрозумілий номер тижня {week!r}")
        return week

    def schedule(self, target: Target, start: date, end: date, today: date | None = None) -> list[Lesson]:
        if not target.uid:
            raise RozkladError("для КПІ потрібен ідентифікатор групи — вибери групу у вікні програми")
        data = self._get("/schedule/lessons", groupId=target.uid)
        weeks = {1: data.get("scheduleFirstWeek") or [], 2: data.get("scheduleSecondWeek") or []}
        today = today or date.today()
        this_week = self.current_week()
        monday = today - timedelta(days=today.weekday())
        lessons: list[Lesson] = []
        day = start
        while day <= end:
            if day.weekday() < len(DAYS):
                shift = (day - timedelta(days=day.weekday()) - monday).days // 7
                week = this_week if shift % 2 == 0 else 3 - this_week
                lessons += self._day(weeks, week, day)
            day += timedelta(days=1)
        return sorted(lessons, key=lambda lesson: (lesson.day, lesson.start, lesson.subject))

    @staticmethod
    def _day(weeks: dict, week: int, day: date) -> list[Lesson]:
        name = DAYS[day.weekday()]
        found, seen = [], set()
        for number, days in weeks.items():
            for entry in days:
                if entry.get("day") != name:
                    continue
                for pair in entry.get("pairs") or []:
                    dates = {parse_date(text) for text in pair.get("dates") or []} - {None}
                    # Звичайна пара — лише свого тижня; нерегулярна — у свої дати, хоч би де лежала.
                    if (dates and day not in dates) or (not dates and number != week):
                        continue
                    lesson = to_lesson(pair, day)
                    key = (lesson.start, lesson.subject, lesson.type) if lesson else None
                    if lesson is not None and key not in seen:
                        seen.add(key)
                        found.append(lesson)
        return found
