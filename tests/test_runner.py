from datetime import datetime
from zoneinfo import ZoneInfo

from autopara.config import Config, JoinSettings, ScheduleSettings
from autopara.links import LinkRule
from autopara.rozklad import RozkladError, Target
from autopara.runner import Runner
from autopara.session import JoinError, Session

from .conftest import make_lesson

TZ = ZoneInfo("Europe/Kyiv")
MEET = "https://meet.google.com/aaa-bbbb-ccc"


def at(hhmm):
    return datetime.fromisoformat(f"2026-10-05T{hhmm}").replace(tzinfo=TZ)


class FakeClient:
    def __init__(self, lessons):
        self.lessons = lessons
        self.fail = False
        self.calls = 0

    def find_group(self, name, faculty_id=None, course=None):
        return Target(2, 3, 22, group_name=name)

    def schedule(self, target, start, end):
        self.calls += 1
        if self.fail:
            raise RozkladError("сайт лежить")
        return [l for l in self.lessons if start <= l.day <= end]


class FakeJoiner:
    def __init__(self):
        self.joined, self.left = [], []
        self.errors = []

    def join(self, visit):
        if self.errors:
            raise self.errors.pop(0)
        self.joined.append(visit.title)
        return Session(visit, visit.leave_at, page="page")

    def alive(self, session):
        return True

    def leave(self, session):
        self.left.append(session.visit.title)


def make_runner(lessons):
    config = Config(
        schedule=ScheduleSettings(group="ТЦР-32"),
        join=JoinSettings(minutes_before=2),
        links=[LinkRule(subject="Бази", url=MEET)],
    )
    client, joiner = FakeClient(lessons), FakeJoiner()
    return Runner(config, client, joiner), client, joiner


def test_joins_once_on_time_and_leaves_after():
    runner, _, joiner = make_runner([make_lesson()])
    runner.tick(at("09:00"))
    assert joiner.joined == []
    runner.tick(at("09:28"))
    runner.tick(at("09:29"))
    assert joiner.joined == ["Бази даних"]
    runner.tick(at("10:49"))
    assert joiner.left == []
    runner.tick(at("10:50"))
    assert joiner.left == ["Бази даних"]


def test_starting_mid_lesson_joins_immediately_but_not_at_the_very_end():
    runner, _, joiner = make_runner([make_lesson()])
    runner.tick(at("10:00"))
    assert joiner.joined == ["Бази даних"]
    late_runner, _, late_joiner = make_runner([make_lesson()])
    late_runner.tick(at("10:47"))
    assert late_joiner.joined == []


def test_failed_join_is_retried_then_given_up():
    runner, _, joiner = make_runner([make_lesson()])
    joiner.errors = [JoinError("мережа"), JoinError("мережа"), JoinError("мережа")]
    runner.tick(at("09:28"))
    runner.tick(at("09:29"))  # ще рано для повтору
    runner.tick(at("09:30"))
    runner.tick(at("09:32"))
    runner.tick(at("09:40"))
    assert joiner.joined == [] and joiner.errors == []
    assert runner.attempts[runner.plan.visits[0].key] == 3


def test_fatal_join_error_is_not_retried():
    runner, _, joiner = make_runner([make_lesson()])
    joiner.errors = [JoinError("потрібно увійти", retry=False), JoinError("не має дійти")]
    runner.tick(at("09:28"))
    runner.tick(at("09:35"))
    assert len(joiner.errors) == 1


def test_schedule_errors_do_not_stop_the_loop():
    runner, client, joiner = make_runner([make_lesson()])
    client.fail = True
    runner.tick(at("09:00"))
    runner.tick(at("09:01"))
    assert client.calls == 1  # наступна спроба — через 5 хв
    client.fail = False
    runner.tick(at("09:05"))
    runner.tick(at("09:28"))
    assert joiner.joined == ["Бази даних"]


def test_replanning_mid_call_does_not_rejoin():
    lessons = [make_lesson(), make_lesson(start="11:10", end="12:30", type="Пз")]
    runner, _, joiner = make_runner(lessons)
    runner.tick(at("09:28"))
    # Розклад оновився вже після першої пари — друга виглядає як окремий візит на ту саму зустріч.
    runner.next_refresh = None
    runner.tick(at("11:09"))
    runner.tick(at("12:29"))
    assert joiner.joined == ["Бази даних + Бази даних (Пз)"]
    assert joiner.left == []
    runner.tick(at("12:30"))
    assert len(joiner.left) == 1


def test_open_meeting_is_reused_for_the_next_pair():
    # Без auto_leave вкладка лишається відкритою — наступна пара в тій самій зустрічі не відкриває другу.
    lessons = [make_lesson(), make_lesson(start="14:30", end="15:50", type="Пз")]
    runner, _, joiner = make_runner(lessons)
    runner.config.join.auto_leave = False
    runner.tick(at("09:28"))
    runner.tick(at("14:28"))
    assert joiner.joined == ["Бази даних"]
    assert [s.leave_at for s in runner.active.values()] == [at("15:50")]
