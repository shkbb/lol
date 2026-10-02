"""Коли виходити з пари: разом з групою, а не рівно за розкладом.

Пара може скінчитися на пів години раніше або затягнутися на кілька хвилин —
розклад цього не знає. Зате це видно з кількості учасників зустрічі:
коли пара закінчується, люди масово виходять. Тож ми виходимо слідом за
більшістю (не першими і не останніми), а розклад — лише запасний варіант,
якщо порахувати учасників не вдалося.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import JoinSettings

log = logging.getLogger(__name__)

# З якої кількості учасників (разом з тобою) вважаємо, що пара «зібралась».
MIN_CROWD = 5
# Яка частка від максимуму учасників має лишитись, щоб ми теж вийшли:
# під час пари — коли пішло 60 % людей, після кінця за розкладом — коли пішло 30 %.
CROWD_LEFT_DURING = 0.4
CROWD_LEFT_AFTER_END = 0.7
# На початку пари сам у зустрічі — це нормально: всі ще збираються.
START_GRACE = timedelta(minutes=15)


@dataclass(frozen=True)
class Observation:
    participants: int | None = None  # разом з тобою; None — порахувати не вдалося
    ended: bool = False  # зустріч завершилась або тебе з неї вийшли
    mic_on: bool = False  # мікрофон увімкнено вручну — значить, ти за ноутом


class LeaveDecider:
    def __init__(self, start: datetime, end: datetime, settings: JoinSettings, rng: random.Random | None = None):
        self.start = start
        self.end = end
        self.after = timedelta(minutes=settings.leave_after_minutes)
        self.overtime = timedelta(minutes=settings.max_overtime_minutes)
        self.rng = rng or random.Random()
        self.peak = 0
        self._pending: tuple[datetime, str] | None = None  # (коли виходимо, чому)

    def next_lesson(self, start: datetime, end: datetime) -> None:
        """Наступна пара в тій самій зустрічі: відлік починаємо заново."""
        self.start, self.end = start, end
        self._pending = None

    def update(self, now: datetime, seen: Observation) -> str | None:
        """Повертає причину виходу, або None, якщо лишаємось."""
        if seen.ended:
            return "зустріч завершилась"
        if seen.mic_on:
            # Ти сам щось говориш — не заважаємо.
            self._pending = None
            return None
        if now >= self.end + max(self.overtime, self.after):
            minutes = int(max(self.overtime, self.after).total_seconds() // 60)
            return f"минуло {minutes} хв після кінця пари за розкладом"
        if seen.participants is None:
            return "пара скінчилась за розкладом" if now >= self.end + self.after else None

        self.peak = max(self.peak, seen.participants)
        reason, delay = self._condition(now, seen.participants)
        if reason is None:
            if self._pending is not None:
                log.info("Люди повертаються — лишаюсь (%d учасників)", seen.participants)
            self._pending = None
            return None
        if self._pending is None:
            self._pending = (now + delay, reason)
            if delay:
                log.info("%s — вийду приблизно за %d с", reason, delay.total_seconds())
        when, _ = self._pending
        return reason if now >= when else None

    def _condition(self, now: datetime, count: int) -> tuple[str | None, timedelta]:
        ratio = CROWD_LEFT_AFTER_END if now >= self.end else CROWD_LEFT_DURING
        if self.peak >= MIN_CROWD and count <= max(1, int(self.peak * ratio)):
            # Невелика випадкова затримка: не виходимо ні першими, ні останніми.
            return f"більшість вийшла ({count} з {self.peak})", self._delay(20, 60)
        if count <= 1 and now >= self.start + START_GRACE:
            if self.peak <= 1:
                return "через 15 хв після початку ніхто так і не прийшов", timedelta(0)
            return "ти лишився в зустрічі сам", self._delay(5, 15)
        return None, timedelta(0)

    def _delay(self, low: int, high: int) -> timedelta:
        return timedelta(seconds=self.rng.randint(low, high))
