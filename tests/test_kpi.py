from datetime import date, time

import pytest

from autopara.config import parse_config
from autopara.kpi import KpiClient, group_from_link
from autopara.rozklad import RozkladError
from autopara.sources import make_source, resolve_target

# Формат — як у моделей офіційного сайту github.com/kpi-ua/schedule.kpi.ua (src/models).
GROUPS = [
    {"id": 4383, "name": "ІП-21", "faculty": "ФІОТ"},
    {"id": 4400, "name": "ІП-22", "faculty": "ФІОТ"},
    {"id": 5000, "name": "ТВ-21", "faculty": "ФПМ"},
    {"id": 5001, "name": "ТВ-21", "faculty": "ІАТЕ"},
]
LESSONS = {
    "groupCode": "ІП-21",
    "scheduleFirstWeek": [
        {"day": "Пн", "pairs": [
            {"name": "Бази даних", "time": "8.30", "tag": "lec", "type": "Лек on-line",
             "lecturer": {"id": "1", "name": "Петренко Петро Петрович"},
             "location": {"uri": "https://meet.google.com/aaa-bbbb-ccc", "title": "Google Meet"}, "dates": []},
        ]},
        {"day": "Вв", "pairs": [
            {"name": "Фізика", "time": "10:25", "tag": "lab", "type": "Лаб", "lecturer": {"id": "2", "name": "Іванов І.І."},
             "location": {"uri": "", "title": "ауд. 101"}, "dates": []},
        ]},
    ],
    "scheduleSecondWeek": [
        {"day": "Пн", "pairs": [
            {"name": "Англійська мова", "time": "12:20:00", "tag": "prac", "type": "Прак", "lecturer": None,
             "location": None, "dates": []},
            {"name": "Консультація", "time": "14:15", "tag": "prac", "type": "", "lecturer": None,
             "location": None, "dates": ["2026-10-12"]},
        ]},
    ],
}


class FakeApi:
    def __init__(self, week=1):
        self.headers = {}
        self.week = week
        self.calls = []

    def get(self, url, params=None, timeout=None):
        path = url.split("api.campus.kpi.ua", 1)[1]
        self.calls.append((path, params))
        data = {"/group/all": GROUPS, "/schedule/lessons": LESSONS, "/time/current": {"currentWeek": self.week}}[path]

        class Response:
            status_code = 200

            def json(self):
                return data

        return Response()


def test_group_from_link():
    assert group_from_link("https://schedule.kpi.ua/?groupId=4383") == "4383"
    assert group_from_link("schedule.kpi.ua/?lang=uk&groupId=4383#x") == "4383"
    assert group_from_link("ІП-21") is None


def test_find_group_by_name_link_and_ambiguity():
    client = KpiClient(session=FakeApi())
    assert client.find_group("іп-21").uid == "4383"
    found = client.find_group("https://schedule.kpi.ua/?groupId=4383")
    assert (found.uid, found.group_name, found.faculty_name) == ("4383", "ІП-21", "ФІОТ")
    with pytest.raises(RozkladError, match="кілька"):
        client.find_group("ТВ-21")
    assert [g.uid for g in client.search_groups("ІП-2")] == ["4383", "4400"]


def test_two_week_cycle_and_irregular_dates():
    # 05.10.2026 — понеділок першого тижня (за /time/current), 12.10 — другого.
    client = KpiClient(session=FakeApi(week=1))
    group = client.find_group("ІП-21")
    lessons = client.schedule(group, date(2026, 10, 5), date(2026, 10, 13), today=date(2026, 10, 7))
    got = [(l.day, l.start, l.end, l.subject, l.type) for l in lessons]
    assert got == [
        (date(2026, 10, 5), time(8, 30), time(10, 5), "Бази даних", "Лк"),
        (date(2026, 10, 6), time(10, 25), time(12, 0), "Фізика", "Лб"),
        (date(2026, 10, 12), time(12, 20), time(13, 55), "Англійська мова", "Пз"),
        (date(2026, 10, 12), time(14, 15), time(15, 50), "Консультація", "Пз"),
    ]
    first = lessons[0]
    assert first.link == "https://meet.google.com/aaa-bbbb-ccc" and first.teacher == "Петренко Петро Петрович"
    assert lessons[1].room == "ауд. 101" and lessons[1].link is None


def test_week_parity_follows_the_server():
    client = KpiClient(session=FakeApi(week=2))
    group = client.find_group("ІП-21")
    monday = client.schedule(group, date(2026, 10, 5), date(2026, 10, 5), today=date(2026, 10, 7))
    assert [l.subject for l in monday] == ["Англійська мова"]  # «Консультація» — лише 12.10


def test_kpi_source_from_config():
    config = parse_config({"schedule": {"university": "kpi", "group": "https://schedule.kpi.ua/?groupId=4383"}})
    client = make_source(config)
    assert isinstance(client, KpiClient)
    client.session = FakeApi()
    target = resolve_target(client, config)
    assert (target.uid, target.group_name) == ("4383", "ІП-21")
