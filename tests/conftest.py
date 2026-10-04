import os
import tempfile
from datetime import date, time
from pathlib import Path

# Тести не чіпають справжні теки користувача (~/.autopara, ~/.config/autopara): домашня тека на час
# тестів тимчасова. Браузер Playwright — той самий, уже завантажений.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache" / "ms-playwright"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="autopara-tests-home-")
os.environ.pop("XDG_CONFIG_HOME", None)

import pytest  # noqa: E402

from autopara.rozklad import Lesson  # noqa: E402


def make_lesson(day=date(2026, 10, 5), start="09:30", end="10:50", subject="Бази даних", **kw) -> Lesson:
    return Lesson(day, time.fromisoformat(start), time.fromisoformat(end), subject, **kw)


@pytest.fixture
def lesson():
    return make_lesson
