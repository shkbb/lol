"""Спільні типи для «заходилки» й фонового циклу (без залежності від Playwright)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .planner import Visit


class JoinError(Exception):
    def __init__(self, message: str, retry: bool = True):
        super().__init__(message)
        self.retry = retry


class JoinDenied(JoinError):
    """Зустріч не пускає (саме цей акаунт чи взагалі) — повторювати з тим самим акаунтом немає сенсу."""

    def __init__(self, message: str):
        super().__init__(message, retry=False)


@dataclass
class Session:
    visit: Visit
    leave_at: datetime
    page: Any = None  # playwright Page; None — Zoom-клієнт, ним ми не керуємо
    decider: Any = None  # LeaveDecider, якщо виходимо разом з групою, а не за розкладом
    current: str = ""  # пара, що йде зараз у цій зустрічі (наступна пара в тій самій зустрічі — інша назва)

    @property
    def url(self) -> str:
        return self.visit.url

    @property
    def title(self) -> str:
        return self.current or self.visit.title
