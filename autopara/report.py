"""Звіт після кожної пари: про що була, що робили, завдання й дедлайни, чи кликали тебе.

Розшифровку пари мозок конспектує частинами ще під час пари (у фоні, коли не зайнятий
важливішим), тож у кінці лишається тільки звести конспект докупи — звіт приходить за хвилину-дві.
Без мозку звіт усе одно буде: статистика, згадки, дії помічника й файл з розшифровкою.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import notes
from .assistant import CHUNK_CHARS, LessonRecord
from .brain import BrainError
from .lang import UKRAINIAN_ONLY, ukrainian

log = logging.getLogger(__name__)

CHUNK_PROMPT = (
    "Ти конспектуєш онлайн-пару для студента, якого на ній не було. Тобі дають уривок автоматичної "
    "розшифровки (можливі помилки розпізнавання). Випиши 2–5 коротких пунктів українською: що пояснювали, "
    "що робили, і окремо — будь-які завдання, дедлайни, оголошення. Без вступу. " + UKRAINIAN_ONLY
)
QUESTION_PROMPT = (
    "Ти — autopara, помічник студента: сам ходить за нього на онлайн-пари, слухає їх і пише йому в Telegram. "
    "Зараз студент пише тобі. Відповідай українською, коротко й по-людськи (до 100 слів), по-дружньому, але по суті. "
    "Якщо питання про пари — спирайся лише на дані нижче (автоматична розшифровка з помилками розпізнавання, чат, "
    "підсумки минулих пар); чого там немає — так і скажи, не вигадуй. Якщо це просто розмова чи питання не про "
    "пари — відповідай як звичайний співрозмовник. Сам ти на парі нічого не робиш; якщо студент просить дію, "
    "підкажи команду: «екран» — скріншот, «чат» — чат пари, «статус», «напиши <текст>» — у чат пари, «вийди»."
)
FINAL_PROMPT = (
    "Ти складаєш короткий звіт про пару для студента, якого на ній не було. Тобі дають конспект пари "
    "частинами і, можливо, повідомлення з чату. Напиши українською, до 150 слів, рівно в такому форматі:\n"
    "Про що: …\nЩо робили: …\nЗавдання й дедлайни: … (або «не було»)\nВажливе: … (або «нічого»)\n"
    "Лише те, що справді казали. Без заповнювачів у дужках ([дата], [ім'я]): чого не казали — просто не згадуй. "
    + UKRAINIAN_ONLY
)


BLINK = timedelta(minutes=2)  # коротше — не пара, а випадковий захід (і звіт про нього не потрібен)


class Reporter:
    """Після кожної пари: запис у ~/.autopara/Пари/<Предмет>/ (завжди) і звіт у Telegram (якщо ввімкнено)."""

    def __init__(self, notifier, brain=None, worker=None, folder: Path | None = None, send: bool = True):
        self.notifier = notifier
        self.brain = brain
        self.worker = worker
        self.folder = folder
        self.send = send
        self.audio = None  # refine.LessonAudio — звук пари для уточнення після неї
        self.refiner = None  # refine.Refiner — друге, точніше розпізнавання після пари
        self.after_report: Callable[[], object] | None = None  # звіт надіслано (вивантажити мозок, якщо пар не буде)
        self._notes: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def chunk(self, report_id: str, text: str) -> None:
        """Черговий шматок розшифровки — конспектуємо у фоні."""
        if self.brain is None or self.worker is None:
            return
        self.worker.submit(lambda: self._summarize_chunk(report_id, text))

    def finish(self, record: LessonRecord) -> None:
        """Пара скінчилась: запис — одразу (щоб не загубився, навіть якщо програму зараз закриють),
        підсумок і звіт — потім (після всіх конспектів, що вже в черзі)."""
        recorded = self.audio.stop(record.report_id) if self.audio is not None else None
        if not (record.transcript or record.chat) and record.left - record.joined < BLINK:
            if recorded is not None:
                recorded[0].unlink(missing_ok=True)
            return  # зайшов і одразу вийшов (зустріч уже скінчилась) — звітувати нема про що
        path = self.save(record)
        if recorded is not None and self.refiner is not None:
            # Звіт — з уточненої розшифровки (див. refine.py): вона буде пізніше, зате точніша.
            self.refiner.submit(record, path, *recorded)
            if self.send:
                self.notifier(f"📝 «{record.title}» скінчилась. Звіт пришлю, щойно ще раз уважно розшифрую запис "
                              "(у перервах між парами чи після них).")
            return
        self._later(record, path)

    def _later(self, record: LessonRecord, path: Path | None) -> None:
        if self.worker is not None:
            self.worker.submit(lambda: self._finish(record, path))
        else:
            threading.Thread(target=self._finish, args=(record, path), daemon=True).start()

    def fallback(self, record: LessonRecord, path: Path | None) -> None:
        """Уточнити не вийшло — звіт із живої розшифровки, як і раніше."""
        self._later(record, path)

    def complete(self, record: LessonRecord, path: Path | None, refined: list[tuple[datetime, str]]) -> None:
        """Уточнена розшифровка готова (потік уточнення): конспект і звіт — уже з неї."""
        with self._lock:
            self._notes.pop(record.report_id, None)  # конспект живої розшифровки вже не потрібен
        live = record
        record = replace(record, transcript=refined)
        parts = []
        if self.brain is not None:
            for piece in _pieces([text for _, text in refined], CHUNK_CHARS):
                try:
                    if note := ukrainian(self.brain.summarize, CHUNK_PROMPT, piece):
                        parts.append(note)
                except BrainError as e:
                    log.warning("Не вдалося законспектувати шматок пари: %s", e)
        summary = self._compose(parts, record)
        path = self.save(record, summary, live=live) or path
        if self.send:
            self.notifier(render(record, summary) + "\n\n🎧 Розшифровку уточнено після пари.")
            if path is not None:
                self.notifier.document(path.read_bytes(), path.name, f"Розшифровка «{record.title}»")
        self._reported()

    def _summarize_chunk(self, report_id: str, text: str) -> None:
        try:
            note = ukrainian(self.brain.summarize, CHUNK_PROMPT, text)
        except BrainError as e:
            log.warning("Не вдалося законспектувати шматок пари: %s", e)
            return
        if note is None:
            log.warning("Конспект шматка пари вийшов російською — пропускаю")
            return
        with self._lock:
            self._notes.setdefault(report_id, []).append(note)

    def _finish(self, record: LessonRecord, path: Path | None = None) -> None:
        if record.rest and self.brain is not None:
            self._summarize_chunk(record.report_id, record.rest)
        with self._lock:
            parts = self._notes.pop(record.report_id, [])
        summary = self._compose(parts, record)
        if summary and path is not None:
            notes.add_summary(path, summary)
        if self.send:
            self.notifier(render(record, summary))
            if path is not None:
                self.notifier.document(path.read_bytes(), path.name, f"Розшифровка «{record.title}»")
        self._reported()

    def _reported(self) -> None:
        if self.after_report is not None:
            try:
                self.after_report()
            except Exception:  # noqa: BLE001 — це лише прибирання пам'яті
                log.debug("Не вдалося прибрати після звіту", exc_info=True)

    def _compose(self, parts: list[str], record: LessonRecord) -> str:
        """Конспект частинами й чат → підсумок пари (або хоч би конспект частинами)."""
        others = [f"{m.author or '?'}: {m.text}" for _, m in record.chat]
        if self.brain is None or not (parts or len(others) >= 5):
            return ""
        material = "\n\n".join(f"Частина {i}:\n{note}" for i, note in enumerate(parts, 1))
        if others:
            material += "\n\nЧат:\n" + "\n".join(others[-40:])
        try:
            summary = ukrainian(self.brain.summarize, FINAL_PROMPT, material)
            return summary if summary is not None else "\n".join(parts)
        except BrainError as e:
            log.warning("Не вдалося скласти звіт: %s", e)
            return "\n".join(parts)

    def save(self, record: LessonRecord, summary: str = "", live: LessonRecord | None = None) -> Path | None:
        """Розшифровка й чат пари — у теку її предмета (лише локально). ``live`` — жива розшифровка
        для порівняння, коли основна вже уточнена."""
        if self.folder is None or not (record.transcript or record.chat):
            return None
        path = notes.lesson_path(self.folder, record.title, record.start)
        header = f"{record.title} — {record.start:%d.%m.%Y %H:%M}–{record.end:%H:%M}, {record.teacher}"
        transcript = [f"[{t:%H:%M:%S}] {text}" for t, text in record.transcript]
        chat = [f"[{t:%H:%M:%S}] {m.author or '?'}: {m.text}" for t, m in record.chat]
        earlier = [f"[{t:%H:%M:%S}] {text}" for t, text in live.transcript] if live is not None else None
        notes.write(path, header, summary, transcript, chat, live=earlier)
        return path


def _pieces(lines: list[str], size: int) -> list[str]:
    """Розшифровка шматками приблизно по ``size`` символів — для конспекту частинами."""
    pieces, current = [], ""
    for line in lines:
        if current and len(current) + len(line) > size:
            pieces.append(current)
            current = ""
        current = f"{current} {line}".strip()
    if current:
        pieces.append(current)
    return pieces


def render(record: LessonRecord, summary: str) -> str:
    lines = [f"📝 Звіт: «{record.title}», {record.start:%H:%M}–{record.end:%H:%M}"]
    if record.teacher:
        lines.append(f"👤 {record.teacher}")
    stay = f"⏱ на парі з {record.joined:%H:%M} до {record.left:%H:%M}"
    lines.append(stay + (f" · 👥 до {record.peak} учасників" if record.peak else ""))
    lines.append("")
    if summary:
        lines.append(summary.strip())
    elif record.transcript:
        lines.append("Мозок вимкнений — підсумку немає, але розшифровка у файлі нижче.")
    else:
        lines.append("Вуха вимкнені — що було на парі, я не чув.")
    if record.mentions:
        lines.append("")
        lines.append("🔔 Тебе згадували:")
        lines += [f"  {t:%H:%M} «{text[:150]}»" for t, text in record.mentions[:5]]
    if record.sent:
        lines.append("")
        lines.append("💬 Я писав у чат: " + ", ".join(f"«{text}» о {t:%H:%M}" for t, text in record.sent))
    return "\n".join(lines)
