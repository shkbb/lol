from datetime import date, time

import pytest

from autopara.rozklad import Lesson


def make_lesson(day=date(2026, 10, 5), start="09:30", end="10:50", subject="Бази даних", **kw) -> Lesson:
    return Lesson(day, time.fromisoformat(start), time.fromisoformat(end), subject, **kw)


@pytest.fixture
def lesson():
    return make_lesson
