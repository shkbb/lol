import random
from datetime import datetime
from zoneinfo import ZoneInfo

from autopara.config import JoinSettings
from autopara.leaving import LeaveDecider, Observation

TZ = ZoneInfo("Europe/Kyiv")


def at(hhmm):
    return datetime.fromisoformat(f"2026-10-05T{hhmm}").replace(tzinfo=TZ)


def decider(**settings):
    return LeaveDecider(at("09:30"), at("10:50"), JoinSettings(**settings), rng=random.Random(1))


def run(d, steps):
    """steps: [(час, кількість учасників)] → перша причина виходу з часом, або None."""
    for hhmm, count in steps:
        if reason := d.update(at(hhmm), Observation(participants=count)):
            return hhmm, reason
    return None


def test_alone_at_the_start_is_normal():
    assert run(decider(), [("09:28", 1), ("09:35", 1), ("09:44", 1)]) is None


def test_nobody_came():
    hhmm, reason = run(decider(), [("09:28", 1), ("09:44", 1), ("09:45", 1)])
    assert hhmm == "09:45" and "ніхто" in reason


def test_left_alone_after_everyone_left():
    hhmm, reason = run(decider(), [("09:28", 3), ("09:50", 4), ("10:00", 1), ("10:01", 1)])
    assert hhmm == "10:01" and "сам" in reason


def test_crowd_leaving_early():
    hhmm, reason = run(decider(), [("09:30", 20), ("10:10", 7), ("10:11", 7)])
    assert hhmm == "10:11" and "більшість" in reason


def test_a_few_people_dropping_mid_pair_is_not_the_end():
    assert run(decider(), [("09:30", 20), ("10:00", 14), ("10:20", 12), ("10:49", 12)]) is None


def test_people_coming_back_cancel_the_leave():
    assert run(decider(), [("09:30", 20), ("10:10", 7), ("10:10:30", 18), ("10:12", 18)]) is None


def test_after_scheduled_end_leave_once_people_start_leaving():
    d = decider()
    assert run(d, [("09:30", 20), ("10:55", 20), ("11:00", 19)]) is None  # пара затягнулась
    hhmm, _ = run(d, [("11:02", 13), ("11:03", 13)])
    assert hhmm == "11:03"


def test_hard_stop_after_overtime():
    hhmm, reason = run(decider(max_overtime_minutes=20), [("09:30", 20), ("11:09", 20), ("11:10", 20)])
    assert hhmm == "11:10" and "20 хв" in reason


def test_unknown_count_falls_back_to_schedule():
    d = decider(leave_after_minutes=3)
    assert d.update(at("10:52"), Observation()) is None
    assert d.update(at("10:53"), Observation()) == "пара скінчилась за розкладом"


def test_meeting_ended():
    assert decider().update(at("09:40"), Observation(ended=True)) == "зустріч завершилась"


def test_mic_on_means_you_are_here():
    d = decider()
    assert d.update(at("10:00"), Observation(participants=2, mic_on=True)) is None
    assert d.update(at("11:30"), Observation(participants=2, mic_on=True)) is None


def test_next_lesson_in_same_meeting():
    d = decider()
    run(d, [("09:30", 20)])
    d.next_lesson(at("11:10"), at("12:30"))
    assert run(d, [("11:05", 18), ("11:30", 18), ("12:40", 18)]) is None
