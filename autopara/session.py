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


@dataclass
class Session:
    visit: Visit
    leave_at: datetime
    page: Any = None  # playwright Page; None — Zoom-клієнт, ним ми не керуємо

    @property
    def url(self) -> str:
        return self.visit.url
