from datetime import datetime
from zoneinfo import ZoneInfo

from autopara.config import JoinSettings
from autopara.links import LinkRule
from autopara.planner import build_plan

TZ = ZoneInfo("Europe/Kyiv")
MEET = "https://meet.google.com/aaa-bbbb-ccc"
ZOOM = "https://zoom.us/j/12345678901"
RULES = [LinkRule(subject="Бази", url=MEET), LinkRule(subject="Мережі", url=ZOOM)]


def at(hhmm, day=5):
    return datetime.fromisoformat(f"2026-10-{day:02d}T{hhmm}").replace(tzinfo=TZ)


def test_join_and_leave_times(lesson):
    plan = build_plan([lesson()], RULES, JoinSettings(minutes_before=3), TZ, at("08:00"))
    [visit] = plan.visits
    assert (visit.join_at, visit.leave_at, visit.platform) == (at("09:27"), at("10:50"), "meet")


def test_past_lessons_dropped_current_kept(lesson):
    lessons = [lesson(start="08:00", end="09:20"), lesson(subject="Мережі", start="09:30", end="10:50")]
    plan = build_plan(lessons, RULES, JoinSettings(), TZ, at("10:00"))
    assert [v.url for v in plan.visits] == [ZOOM]


def test_back_to_back_pairs_with_same_link_are_one_call(lesson):
    lessons = [lesson(start="09:30", end="10:50"), lesson(start="11:10", end="12:30", type="Пз")]
    plan = build_plan(lessons, RULES, JoinSettings(), TZ, at("08:00"))
    [visit] = plan.visits
    assert visit.leave_at == at("12:30")
    assert visit.title == "Бази даних + Бази даних (Пз)"
    # Перша пара вже скінчилась, друга йде — візит той самий (той самий ключ), а не новий.
    later = build_plan(lessons, RULES, JoinSettings(), TZ, at("11:30"))
    assert [v.key for v in later.visits] == [visit.key]


def test_different_links_back_to_back(lesson):
    lessons = [lesson(start="09:30", end="10:50"), lesson(subject="Мережі", start="11:10", end="12:30")]
    plan = build_plan(lessons, RULES, JoinSettings(leave_after_minutes=30), TZ, at("08:00"))
    assert [v.url for v in plan.visits] == [MEET, ZOOM]
    # Виходимо з першої, щойно настав час заходити на другу, а не через 30 хв.
    assert plan.visits[0].leave_at == plan.visits[1].join_at == at("11:08")


def test_overlapping_subgroups_reported(lesson):
    lessons = [lesson(type="Лб"), lesson(subject="Мережі", type="Лб")]
    plan = build_plan(lessons, RULES, JoinSettings(), TZ, at("08:00"))
    assert [v.url for v in plan.visits] == [MEET]
    assert len(plan.conflicts) == 1 and "накладається" in plan.conflicts[0]


def test_unlinked_and_skipped(lesson):
    lessons = [lesson(subject="Філософія"), lesson(subject="Фізичне виховання", start="11:10", end="12:30")]
    rules = [*RULES, LinkRule(subject="Фізичне", skip=True)]
    plan = build_plan(lessons, rules, JoinSettings(), TZ, at("08:00"))
    assert plan.visits == []
    assert [l.subject for l in plan.unlinked] == ["Філософія"]
