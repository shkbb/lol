"""Сторінки «Сьогодні», «Розклад» і «Журнал»."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPlainTextEdit, QVBoxLayout, QWidget

from .. import app as app_module
from . import widgets as w
from .panels import PLATFORM, lesson_badge

DAY_NAMES = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня",
          "листопада", "грудня"]
SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def when(iso: str) -> str:
    moment = datetime.fromisoformat(iso)
    today = date.today()
    day = "сьогодні" if moment.date() == today else "завтра" if moment.date() == today + timedelta(days=1) \
        else f"{SHORT[moment.weekday()]} {moment:%d.%m}"
    return f"{day}, {moment:%H:%M}"


def today_text() -> str:
    today = date.today()
    return f"{DAY_NAMES[today.weekday()].lower()}, {today.day} {MONTHS[today.month - 1]}"


class TodayPage(w.Page):
    """Головна: чи працює програма, що зараз, що потребує уваги і що далі."""

    def __init__(self, model, bridge, go):
        super().__init__("Сьогодні", today_text())
        self.model, self.bridge, self.go = model, bridge, go
        self._shown = None

        self.status = w.Card()
        top = QHBoxLayout()
        self.dot = w.label("●")
        self.dot.setStyleSheet("font-size: 20px;")
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = w.label("", "big")
        self.subtitle = w.label("", "muted")
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        self.toggle = w.button("", "", self._toggle)
        top.addWidget(self.dot)
        top.addSpacing(6)
        top.addLayout(text, 1)
        top.addWidget(self.toggle)
        self.status.add(top)
        self.banners = QVBoxLayout()
        self.banners.setSpacing(8)
        self.status.add(self.banners)
        self.add(self.status)

        self.attention = w.Card("Потребує уваги")
        self.attention_list = QVBoxLayout()
        self.attention_list.setSpacing(8)
        self.attention.add(self.attention_list)
        self.calm = w.label("✓ Нічого не потребує твоєї уваги", "calm")
        self.add(self.attention)
        self.add(self.calm)

        self.upcoming = w.Card()
        head = QHBoxLayout()
        head.addWidget(w.label("Далі", "h2"), 1)
        head.addWidget(w.button("Весь розклад", "link", lambda: self.go("schedule")))
        self.upcoming.add(head)
        self.upcoming_list = QVBoxLayout()
        self.upcoming_list.setSpacing(6)
        self.upcoming.add(self.upcoming_list)
        self.add(self.upcoming)
        self.update_state({})

    def _toggle(self) -> None:
        running = bool(self._shown and self._shown.get("running"))
        self.toggle.setEnabled(False)
        self.bridge.command("user_stop" if running else "start", ok=lambda _: self.toggle.setEnabled(True),
                            fail=lambda m: (self.toggle.setEnabled(True), self._error(m)), timeout=300)

    def _error(self, message: str) -> None:
        self.banners.addWidget(w.banner(f"⚠ {message}", "bad"))

    def update_state(self, state: dict) -> None:
        keys = ("running", "configured", "group", "university", "error", "restart_pending", "active", "unlinked",
                "conflicts", "upcoming", "browser")
        view = {key: state.get(key) for key in keys}
        if view == self._shown:
            return
        self._shown = view
        running = bool(state.get("running"))
        self.dot.setStyleSheet(f"font-size: 20px; color: {'#16a34a' if running else '#9aa1ad'};")
        self.title.setText("Програма працює й заходитиме на пари сама" if running else "Програма вимкнена")
        where = " · ".join(filter(None, [state.get("university"), state.get("group")]))
        self.subtitle.setText(where or ("Групу ще не обрано" if not state.get("configured") else ""))
        self.toggle.setText("Вимкнути" if running else "Увімкнути")
        self.toggle.setProperty("kind", "" if running else "primary")
        w.restyle(self.toggle)
        self.toggle.setVisible(bool(state.get("configured")))

        w.clear(self.banners)
        browser = state.get("browser") or {}
        if browser.get("status") == "installing":
            self.banners.addWidget(w.banner("Готую браузер програми (один раз, кілька хвилин)…", "info"))
        elif browser.get("status") == "error":
            self.banners.addWidget(w.banner(f"⚠ {browser.get('error')}", "bad"))
        if state.get("restart_pending"):
            self.banners.addWidget(w.banner("Нові налаштування застосуються, щойно скінчиться пара.", "warn"))
        if state.get("error"):
            self.banners.addWidget(w.banner(f"⚠ {state['error']}", "bad"))
        for session in state.get("active") or []:
            row = QFrame()
            row.setObjectName("row")
            line = QHBoxLayout(row)
            line.setContentsMargins(12, 9, 12, 9)
            line.addWidget(w.badge(f"зараз · {PLATFORM.get(session['platform'], session['platform'])}", "ok"))
            name = w.label(session["title"])
            name.setStyleSheet("font-weight: 700;")
            line.addWidget(name, 1)
            if session.get("participants"):
                line.addWidget(w.label(f"{session['participants']} учасників", "muted"))
            line.addWidget(w.button("Вийти", "", lambda key=session["key"]: self.bridge.command("leave", key)))
            self.banners.addWidget(row)

        w.clear(self.attention_list)
        items = 0
        for lesson in (state.get("unlinked") or [])[:5]:
            row = QHBoxLayout()
            text = QVBoxLayout()
            text.setSpacing(1)
            name = w.label(lesson["title"])
            name.setStyleSheet("font-weight: 700;")
            text.addWidget(name)
            text.addWidget(w.label(f"немає посилання · {lesson['when']}", "hint"))
            row.addLayout(text, 1)
            row.addWidget(w.button("Додати посилання", "", lambda: self.go("settings", "pairs")))
            self.attention_list.addLayout(row)
            items += 1
        for conflict in state.get("conflicts") or []:
            self.attention_list.addWidget(w.label(f"⚠ {conflict}"))
            items += 1
        self.attention.setVisible(items > 0)
        self.calm.setVisible(items == 0 and running)

        w.clear(self.upcoming_list)
        upcoming = state.get("upcoming") or []
        for visit in upcoming[:5]:
            row = QHBoxLayout()
            time_label = w.label(when(visit["join_at"]), "muted", wrap=False)
            time_label.setMinimumWidth(130)
            row.addWidget(time_label)
            row.addWidget(w.label(visit["title"]), 1)
            row.addWidget(w.badge(PLATFORM.get(visit["platform"], visit["platform"]), "accent"))
            self.upcoming_list.addLayout(row)
        if not upcoming:
            self.upcoming_list.addWidget(w.label(
                "Найближчими днями пар з посиланнями немає." if running else "Увімкни програму, щоб побачити план.", "muted"))


class SchedulePage(w.Page):
    """Тиждень пар і куди програма заходитиме."""

    def __init__(self, model, bridge, go):
        super().__init__("Розклад", "Твої пари і куди програма заходитиме.")
        self.model, self.bridge, self.go = model, bridge, go
        today = date.today()
        self.this_week = today + timedelta(days=1 if today.weekday() == 6 else -today.weekday())  # у неділю — вже наступний
        self.week = self.this_week
        self.cache: dict = {}
        nav = QHBoxLayout()
        self.week_label = w.label("", "h2", wrap=False)
        self.back_today = w.button("Цей тиждень", "", lambda: self._move(None))
        nav.addWidget(w.button("←", "", lambda: self._move(-7)))
        nav.addWidget(self.week_label)
        nav.addWidget(w.button("→", "", lambda: self._move(7)))
        nav.addWidget(self.back_today)
        nav.addStretch(1)
        nav.addWidget(w.button("Оновити", "", self.refresh))
        self.add(nav)
        self.summary = w.label("", "muted")
        self.add(self.summary)
        self.days = QVBoxLayout()
        self.days.setSpacing(12)
        holder = QWidget()
        holder.setLayout(self.days)
        self.add(holder)
        model.saved_ok.connect(self._forget)
        self._loaded = False

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._loaded:
            self._load()

    def _forget(self) -> None:
        self.cache.clear()
        self._loaded = False

    def refresh(self) -> None:
        self.cache.pop(self.week, None)
        self._load()

    def _move(self, days: int | None) -> None:
        self.week = self.this_week if days is None else self.week + timedelta(days=days)
        self._load()

    def _load(self) -> None:
        self._loaded = True
        end = self.week + timedelta(days=6)
        self.week_label.setText(f"{self.week:%d.%m} – {end:%d.%m}")
        self.back_today.setVisible(self.week != self.this_week)
        w.clear(self.days)
        if not (self.model.get("schedule.group") or self.model.get("schedule.group_uid")):
            self.summary.setText("")
            card = w.Card("Спершу обери групу")
            card.add(w.hbox(w.button("Обрати групу", "primary", lambda: self.go("settings", "pairs")), "stretch"))
            self.days.addWidget(card)
            return
        if self.week in self.cache:
            return self._show(self.week, self.cache[self.week])
        self.summary.setText("Завантажую розклад…")
        week = self.week
        self.bridge.call_api(app_module.api_lessons, {"start": week.isoformat(), "days": "6"},
                             ok=lambda data: self._show(week, data), fail=lambda m: self.summary.setText(f"⚠ {m}"))

    def _show(self, week: date, data: dict) -> None:
        self.cache[week] = data
        if week != self.week:
            return  # уже гортнули далі
        w.clear(self.days)
        missing = sum(1 for lesson in data["lessons"] if not lesson.get("url") and lesson.get("source") != "skip")
        parts = [data.get("group") or ""]
        if missing:
            parts.append(f"без посилання: {missing}")
        elif data["lessons"]:
            parts.append("усі пари з посиланнями")
        self.summary.setText(" · ".join(filter(None, parts)))
        by_day: dict = {}
        for lesson in data["lessons"]:
            by_day.setdefault(lesson["date"], []).append(lesson)
        for offset in range(7):
            day = week + timedelta(days=offset)
            lessons = by_day.get(day.isoformat(), [])
            if offset == 6 and not lessons:
                continue  # неділя без пар
            is_today = day.isoformat() == data["today"]
            card = w.Card()
            head = QHBoxLayout()
            head.addWidget(w.label(f"{DAY_NAMES[day.weekday()]}, {day:%d.%m}", "h2"))
            if is_today:
                head.addWidget(w.badge("сьогодні", "accent"))
            head.addStretch(1)
            card.add(head)
            if not lessons:
                card.add(w.label("Пар немає", "muted"))
            for lesson in lessons:
                card.add(self._lesson(lesson, data, is_today))
            self.days.addWidget(card)

    def _lesson(self, lesson: dict, data: dict, is_today: bool) -> QWidget:
        row = QFrame()
        row.setObjectName("row")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(12, 9, 12, 9)
        now = data["now"]
        past = lesson["date"] < data["today"] or (is_today and now >= lesson["end"])
        current = is_today and lesson["start"] <= now < lesson["end"]
        time_label = w.label(f"{lesson['start']}–{lesson['end']}", "muted", wrap=False)
        time_label.setMinimumWidth(96)
        layout.addWidget(time_label)
        text = QVBoxLayout()
        text.setSpacing(1)
        name = w.label(lesson["subject"] + (f" ({lesson['type']})" if lesson.get("type") else ""))
        name.setStyleSheet("font-weight: 700;" + (" color: gray;" if past else ""))
        text.addWidget(name)
        extra = " · ".join(filter(None, [lesson.get("teacher"), lesson.get("room")]))
        if extra:
            text.addWidget(w.label(extra, "hint"))
        layout.addLayout(text, 1)
        if current:
            layout.addWidget(w.badge("зараз", "accent"))
        layout.addWidget(lesson_badge(lesson))
        if is_today and not past and lesson.get("url"):
            minutes = self._minutes_left(lesson["end"], now) + 10
            layout.addWidget(w.button("Зайти зараз", "primary" if current else "",
                                      lambda url=lesson["url"], minutes=minutes: self._join(url, minutes)))
        return row

    @staticmethod
    def _minutes_left(end: str, now: str) -> int:
        to_minutes = lambda hhmm: int(hhmm[:2]) * 60 + int(hhmm[3:5])  # noqa: E731
        return max(to_minutes(end) - to_minutes(now), 0)

    def _join(self, url: str, minutes: int) -> None:
        self.summary.setText("Заходжу — стежити можна на сторінці «Сьогодні»")
        self.bridge.call_api(app_module.api_join, body={"url": url, "minutes": minutes},
                             fail=lambda m: self.summary.setText(f"⚠ {m}"))


class LogPage(w.Page):
    """Що програма робила — на випадок, якщо щось пішло не так."""

    def __init__(self, model, bridge):
        super().__init__("Журнал", "Що програма робила. Якщо щось не працює — скопіюй звідси й надішли.", width=1100)
        self.bridge = bridge
        self.after = 0
        self.text = QPlainTextEdit()
        self.text.setObjectName("log")
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(3000)
        self.text.setMinimumHeight(420)
        self.add(self.text)
        self.add(w.label(f"Файл журналу: {model.get('data_dir') or '~/.autopara'}/autopara.log", "hint"))
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.pull)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.pull()
        self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.timer.stop()

    def pull(self) -> None:
        lines = self.bridge.api(app_module.api_log, {"after": str(self.after)})["lines"]
        bar = self.text.verticalScrollBar()
        stick = bar.value() >= bar.maximum() - 30
        for line in lines:
            self.after = line["n"]
            self.text.appendPlainText(line["text"])
        if stick:
            bar.setValue(bar.maximum())


def state_digest(state: dict) -> str:
    return json.dumps(state, sort_keys=True, ensure_ascii=False, default=str)
