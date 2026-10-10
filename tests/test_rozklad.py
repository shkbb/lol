import json
from datetime import date, time
from urllib.parse import urlsplit

import pytest

from autopara.rozklad import (
    Option,
    RozkladClient,
    RozkladError,
    Target,
    extract_events,
    find_student,
    normalize_name,
    parse_csrf,
    parse_options,
    to_lesson,
)


def page(selects=None, csrf="tok", events=None, events_raw=None):
    html = f'<html><body><form id="filter-form"><input type="hidden" name="_csrf-frontend" value="{csrf}">'
    for field, options in (selects or {}).items():
        html += f'<select id="timetableform-{field.lower()}"><option value="">Оберіть</option>'
        html += "".join(f'<option value="{i}">{name}</option>' for i, name in options)
        html += "</select>"
    html += "</form>"
    if events is not None:
        events_raw = json.dumps(events, ensure_ascii=False)
    if events_raw is not None:
        html += f"<script>$(function(){{ var events = {events_raw}; init(events); }});</script>"
    return html + "</body></html>"


EVENT = {
    "r1": 370653,
    "disciplineFullName": "Об'єктно-орієнтоване програмування",
    "disciplineShortName": "ООП",
    "classroom": "ауд. 325",
    "timeStart": "09:30",
    "timeEnd": "10:50",
    "teachersNameFull": "Коваль Олександр Вікторович",
    "teachersName": "Коваль О.В.",
    "typeStr": "Лк",
    "groups": "ТЦР-32",
    "notice": "",
    "info": '<p>Посилання: <a href="https://meet.google.com/abc-defg-hij">тут</a></p>',
    "date": "13.10.2026",
    "unknownFutureField": 42,
}


# --- Розбір ------------------------------------------------------------------


def test_parse_csrf_and_options():
    html = page({"facultyId": [(1, "ННІ ІТ"), (2, "ННІ ЦТ")]}, csrf="abc")
    assert parse_csrf(html) == "abc"
    assert parse_options(html, "facultyId") == [Option(1, "ННІ ІТ"), Option(2, "ННІ ЦТ")]


@pytest.mark.parametrize(
    "html",
    [
        "<html>Технічні роботи</html>",
        '<form id="filter-form"></form>',
        '<form id="filter-form"><select id="timetableform-groupid"><option value="x">Г</option></select></form>',
    ],
)
def test_broken_pages_raise_instead_of_empty_lists(html):
    with pytest.raises(RozkladError):
        parse_options(html, "groupId")


def test_extract_events_object_and_lesson_mapping():
    lesson = to_lesson(extract_events(page(events={"1": EVENT}))[0])
    assert lesson.day == date(2026, 10, 13)
    assert (lesson.start, lesson.end) == (time(9, 30), time(10, 50))
    assert lesson.subject == "Об'єктно-орієнтоване програмування"
    assert lesson.type == "Лк"
    assert lesson.room == "325"
    assert lesson.teacher == "Коваль Олександр Вікторович"
    assert lesson.link == "https://meet.google.com/abc-defg-hij"
    assert lesson.title == "Об'єктно-орієнтоване програмування (Лк)"


def test_extract_events_list_empty_and_php_escaping():
    # PHP json_encode екранує «/» і не-ASCII символи — так віддає справжній сайт.
    raw = (
        '[{"disciplineFullName":"\\u0411\\u0430\\u0437\\u0438 \\u0434\\u0430\\u043d\\u0438\\u0445",'
        '"timeStart":"8:00","timeEnd":"9:20","date":"01.10.2026",'
        '"info":"Zoom: https:\\/\\/us05web.zoom.us\\/j\\/12345678901?pwd=x. Код {1}"}]'
    )
    lesson = to_lesson(extract_events(page(events_raw=raw))[0])
    assert lesson.subject == "Бази даних"
    assert lesson.start == time(8, 0)
    assert lesson.link == "https://us05web.zoom.us/j/12345678901?pwd=x"
    assert extract_events(page(events={})) == []
    assert extract_events(page(events=[])) == []


@pytest.mark.parametrize("raw", ["{", '[{"a": }]', "null"])
def test_malformed_events_raise(raw):
    with pytest.raises(RozkladError):
        extract_events(page(events_raw=raw))


def test_missing_events_raise():
    with pytest.raises(RozkladError):
        extract_events(page())


def test_brackets_inside_strings_do_not_break_scanner():
    event = dict(EVENT, notice='Принести ноутбук ]} "обов\'язково"')
    assert to_lesson(extract_events(page(events=[event]))[0]).notice == 'Принести ноутбук ]} "обов\'язково"'


def test_normalize_name_handles_case_spaces_and_latin_lookalikes():
    assert normalize_name("ТЦР-32") == normalize_name("тцр 32") == normalize_name("TЦP–32")


def test_find_student():
    students = [Option(1, "Іваненко Іван Іванович"), Option(2, "Іваненко Петро Олегович"), Option(3, "Бойко Анна")]
    assert find_student(students, "іваненко петро").id == 2
    assert find_student(students, "Бойко").id == 3
    with pytest.raises(RozkladError, match="кільком"):
        find_student(students, "Іваненко")
    with pytest.raises(RozkladError):
        find_student(students, "Шевченко")


# --- HTTP-клієнт на фейковому сайті ------------------------------------------


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class FakeSite:
    """Імітує e-rozklad: факультети → курси → групи → студенти → розклад."""

    def __init__(self):
        self.headers = {}
        self.requests = []
        self.token = "t1"
        self.reject_next_post = False
        self.faculties = [(1, "ННІ Інформаційних технологій"), (2, "ННІ Цифрових технологій")]
        self.courses = {1: [1, 2, 3, 4], 2: [1, 2, 3]}
        self.groups = {(1, 3): [(11, "ІПЗ-31"), (12, "КН-32")], (2, 3): [(21, "ТЦР-31"), (22, "ТЦР-32")]}
        self.students = {22: [(501, "Іваненко Іван"), (502, "Бойко Анна")]}
        self.events = [EVENT]

    def request(self, method, url, timeout=None, params=None, data=None):
        path = urlsplit(url).path
        self.requests.append((method, path, dict(params or {}), dict(data or {})))
        if method == "GET":
            return FakeResponse(200, page({"facultyId": self.faculties}, csrf=self.token))
        if self.reject_next_post or data["_csrf-frontend"] != self.token:
            self.reject_next_post = False
            self.token = "t2"
            return FakeResponse(400, "Unable to verify your data submission.")
        get = lambda name: data.get(f"TimeTableForm[{name}]") or None  # noqa: E731
        faculty, course, group = get("facultyId"), get("course"), get("groupId")
        selects = {"facultyId": self.faculties}
        if faculty:
            selects["course"] = [(c, f"{c} курс") for c in self.courses.get(int(faculty), [])]
        if faculty and course:
            selects["groupId"] = self.groups.get((int(faculty), int(course)), [])
        if path.endswith("/student") and group:
            selects["studentId"] = self.students.get(int(group), [])
        events = self.events if params.get("type") == 1 else None
        return FakeResponse(200, page(selects, csrf=self.token, events=events))


@pytest.fixture
def site():
    return FakeSite()


def test_find_group_guesses_course_from_name(site):
    client = RozkladClient(session=site)
    target = client.find_group("тцр-32")
    assert target == Target(2, 3, 22, group_name="ТЦР-32", faculty_name="ННІ Цифрових технологій")
    # Курс 3 вгадано з назви — повний перебір курсів не знадобився.
    assert len(site.requests) == 3


def test_find_group_falls_back_to_full_scan(site):
    site.groups = {(2, 1): [(31, "ТЦР-32")]}  # «неочевидний» курс
    assert RozkladClient(session=site).find_group("ТЦР-32") == Target(
        2, 1, 31, group_name="ТЦР-32", faculty_name="ННІ Цифрових технологій"
    )


def test_find_group_not_found(site):
    with pytest.raises(RozkladError, match="не знайдено"):
        RozkladClient(session=site).find_group("ХХХ-99")


def test_group_schedule_request(site):
    client = RozkladClient(session=site)
    lessons = client.schedule(Target(2, 3, 22), date(2026, 10, 2), date(2026, 10, 9))
    assert [lesson.subject for lesson in lessons] == ["Об'єктно-орієнтоване програмування"]
    method, path, params, data = site.requests[-1]
    assert (method, path, params) == ("POST", "/time-table/group", {"type": 1})
    assert data == {
        "_csrf-frontend": "t1",
        "TimeTableForm[facultyId]": "2",
        "TimeTableForm[course]": "3",
        "TimeTableForm[groupId]": "22",
        "TimeTableForm[dateStart]": "02.10.2026",
        "TimeTableForm[dateEnd]": "09.10.2026",
        "TimeTableForm[indicationDays]": "5",
        "time-table-type": "1",
    }


def test_student_schedule_uses_student_page(site):
    client = RozkladClient(session=site)
    target = client.resolve_student(Target(2, 3, 22, group_name="ТЦР-32"), "бойко")
    assert (target.student_id, target.student_name) == (502, "Бойко Анна")
    client.schedule(target, date(2026, 10, 2), date(2026, 10, 9))
    method, path, params, data = site.requests[-1]
    assert (path, params, data["TimeTableForm[studentId]"]) == ("/time-table/student", {"type": 1}, "502")
    assert ("GET", "/time-table/student", {"type": 0}, {}) in site.requests


def test_expired_csrf_is_refreshed_once(site):
    client = RozkladClient(session=site)
    client.faculties()
    site.reject_next_post = True
    assert client.courses(2) == [1, 2, 3]
    posts = [r for r in site.requests if r[0] == "POST"]
    assert [p[3]["_csrf-frontend"] for p in posts] == ["t1", "t2"]


def test_http_errors_become_rozklad_errors():
    class Down:
        headers = {}

        def request(self, *args, **kwargs):
            return FakeResponse(404)

    with pytest.raises(RozkladError, match="HTTP 404"):
        RozkladClient(session=Down()).faculties()


def test_group_search_for_the_ui(site):
    client = RozkladClient(session=site)
    found = client.search_groups("тцр")
    assert [(g.group_name, g.faculty_name, g.course) for g in found] == [
        ("ТЦР-31", "ННІ Цифрових технологій", 3),
        ("ТЦР-32", "ННІ Цифрових технологій", 3),
    ]
    requests_made = len(site.requests)
    client.search_groups("іпз")
    assert len(site.requests) == requests_made  # список груп кешується


def test_name_matches_needs_surname_and_name_in_any_order():
    from autopara.rozklad import name_matches

    assert name_matches("Ляшко Денис Іванович", "ляшко денис") and name_matches("Ляшко Денис Іванович", "Денис Ляшко")
    assert not name_matches("Ляшко Денис Іванович", "Ляшко")  # одне прізвище — замало
    assert not name_matches("Ляшко Дмитро Іванович", "Ляшко Денис")
