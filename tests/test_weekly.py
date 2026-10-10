"""Підсумок тижня в неділю ввечері — із підсумків пар, раз на тиждень, і вимикається в налаштуваннях."""

from datetime import datetime

from autopara import notes
from autopara.helper import Library
from autopara.weekly import WeeklyDigest, digest


def record(root, title, start, summary):
    begin = datetime.fromisoformat(start)
    notes.write(notes.lesson_path(root, title, begin),
                f"{title} — {begin:%d.%m.%Y %H:%M}–{begin.replace(hour=begin.hour + 1):%H:%M}, Петренко П.П.",
                summary, ["[10:00:00] текст"], [])


def library(tmp_path):
    record(tmp_path, "Бази даних (Лк)", "2026-10-06T09:30", "**Про що:** ключі.\n**Завдання й дедлайни:** лаба 2 до п'ятниці.")
    record(tmp_path, "Бази даних (Пз)", "2026-10-08T09:30", "**Про що:** нормальні форми.\n**Завдання й дедлайни:** не було")
    record(tmp_path, "Бази даних (Лк)", "2026-09-29T09:30", "**Про що:** минулий тиждень.")
    lib = Library(tmp_path)
    lib.refresh()
    return lib


def test_digest_lists_this_weeks_lessons_tasks_and_next_week(tmp_path):
    text = digest(library(tmp_path).docs, datetime(2026, 10, 11, 19, 30), "📅 пн 12.10:\n  08:00–09:20 Архітектура ПЗ (Лк)")
    assert text.startswith("🗓 Підсумок тижня 05.10–11.10")
    assert "📚 Бази даних — 2 пари (06.10 Лк, 08.10 Пз)" in text and "06.10: ключі." in text and "08.10: нормальні форми." in text
    assert "• Бази даних (06.10): лаба 2 до п'ятниці." in text and "не було" not in text
    assert "минулий тиждень" not in text and "08:00–09:20 Архітектура ПЗ (Лк)" in text


def test_sent_once_on_sunday_evening_and_can_be_switched_off(tmp_path):
    sent, now, enabled = [], [datetime(2026, 10, 11, 18, 0)], [True]
    weekly = WeeklyDigest(library(tmp_path), sent.append, lambda: now[0], lambda when: "", tmp_path / "weekly.json",
                          enabled=lambda: enabled[0])
    assert not weekly.step()  # неділя, але ще не вечір
    now[0] = datetime(2026, 10, 11, 19, 5)
    assert weekly.step() and sent[0].startswith("🗓 Підсумок тижня")
    now[0] = datetime(2026, 10, 11, 21, 0)
    assert not weekly.step()  # за цей тиждень уже надіслали (і пам'ятає це після перезапуску — weekly.json)
    now[0], enabled[0] = datetime(2026, 10, 18, 19, 5), False
    assert not weekly.step() and len(sent) == 1  # вимкнено в налаштуваннях
