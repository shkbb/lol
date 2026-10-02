"""Звіт після кожної пари: про що була, що робили, завдання й дедлайни, чи кликали тебе.

Розшифровку пари мозок конспектує частинами ще під час пари (у фоні, коли не зайнятий
важливішим), тож у кінці лишається тільки звести конспект докупи — звіт приходить за хвилину-дві.
Без мозку звіт усе одно буде: статистика, згадки, дії помічника й файл з розшифровкою.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from .assistant import LessonRecord
from .brain import BrainError

log = logging.getLogger(__name__)

CHUNK_PROMPT = (
    "Ти конспектуєш онлайн-пару для студента, якого на ній не було. Тобі дають уривок автоматичної "
    "розшифровки (можливі помилки розпізнавання). Випиши 2–5 коротких пунктів українською: що пояснювали, "
    "що робили, і окремо — будь-які завдання, дедлайни, оголошення. Без вступу."
)
FINAL_PROMPT = (
    "Ти складаєш короткий звіт про пару для студента, якого на ній не було. Тобі дають конспект пари "
    "частинами і, можливо, повідомлення з чату. Напиши українською, до 150 слів, рівно в такому форматі:\n"
    "Про що: …\nЩо робили: …\nЗавдання й дедлайни: … (або «не було»)\nВажливе: … (або «нічого»)"
)


class Reporter:
    def __init__(self, notifier, brain=None, worker=None, folder: Path | None = None):
        self.notifier = notifier
        self.brain = brain
        self.worker = worker
        self.folder = folder
        self._notes: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def chunk(self, report_id: str, text: str) -> None:
        """Черговий шматок розшифровки — конспектуємо у фоні."""
        if self.brain is None or self.worker is None:
            return
        self.worker.submit(lambda: self._summarize_chunk(report_id, text))

    def finish(self, record: LessonRecord) -> None:
        """Пара скінчилась — складаємо звіт (після всіх конспектів, що вже в черзі)."""
        if self.worker is not None:
            self.worker.submit(lambda: self._finish(record))
        else:
            threading.Thread(target=self._finish, args=(record,), daemon=True).start()

    def _summarize_chunk(self, report_id: str, text: str) -> None:
        try:
            note = self.brain.summarize(CHUNK_PROMPT, text)
        except BrainError as e:
            log.warning("Не вдалося законспектувати шматок пари: %s", e)
            return
        with self._lock:
            self._notes.setdefault(report_id, []).append(note)

    def _finish(self, record: LessonRecord) -> None:
        if record.rest and self.brain is not None:
            self._summarize_chunk(record.report_id, record.rest)
        with self._lock:
            notes = self._notes.pop(record.report_id, [])
        summary = ""
        others = [f"{m.author or '?'}: {m.text}" for _, m in record.chat]
        if self.brain is not None and (notes or len(others) >= 5):
            material = "\n\n".join(f"Частина {i}:\n{note}" for i, note in enumerate(notes, 1))
            if others:
                material += "\n\nЧат:\n" + "\n".join(others[-40:])
            try:
                summary = self.brain.summarize(FINAL_PROMPT, material)
            except BrainError as e:
                log.warning("Не вдалося скласти звіт: %s", e)
                summary = "\n".join(notes)  # хоч би конспект частинами
        self.notifier(render(record, summary))
        if (path := self.save(record)) is not None:
            self.notifier.document(path.read_bytes(), path.name, f"Розшифровка «{record.title}»")

    def save(self, record: LessonRecord) -> Path | None:
        """Розшифровка й чат пари — у ~/.autopara/transcripts (лише локально)."""
        if self.folder is None or not (record.transcript or record.chat):
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        safe_title = re.sub(r'[\\/:*?"<>|]+', " ", record.title).strip()
        path = self.folder / f"{record.start:%Y-%m-%d %H-%M} {safe_title}.txt"
        lines = [f"{record.title} — {record.start:%d.%m.%Y %H:%M}–{record.end:%H:%M}, {record.teacher}", ""]
        lines += [f"[{t:%H:%M:%S}] {text}" for t, text in record.transcript]
        if record.chat:
            lines += ["", "--- Чат ---"]
            lines += [f"[{t:%H:%M:%S}] {m.author or '?'}: {m.text}" for t, m in record.chat]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


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
