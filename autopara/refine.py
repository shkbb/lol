"""Друге розпізнавання після пари — як сервіси субтитрів: увесь запис цілим файлом і точнішим режимом.

Під час пари вуха розпізнають наживо, шматками, — щоб одразу помітити, що тебе кличуть. Але так модель
бачить лише кілька секунд і квапиться. Тож звук пари ще й пишемо у файл (лише на цьому комп'ютері),
а після пари розпізнаємо весь запис ще раз: з контекстом, beam 5, запасними «температурами». Звіт
і запис пари робляться вже з цієї розшифровки; жива лишається в кінці файла для порівняння.

Модель — окрема й точніша (large-v3), а наживо — швидка (turbo): наживо головне встигати. Уточнення йде,
лише коли пари немає й найближчим часом не буде, а модель вантажиться тільки на цей час і з таким
пошуком, на який вистачає вільної пам'яті, — тож звіт приходить пізніше, зате пару нічим не гальмує.
Звук видаляється, щойно розшифровано. Якщо програму закрили посеред уточнення — продовжимо після запуску.
"""

from __future__ import annotations

import gc
import json
import logging
import re
import threading
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .assistant import ChatMessage, LessonRecord
from .ears import Paused

log = logging.getLogger(__name__)

BYTES_PER_SECOND = 16_000 * 2


def _safe(report_id: str) -> str:
    return re.sub(r"[^\w.-]+", "_", report_id)[:120]


class LessonAudio:
    """Пише звук кожної пари у файл, поки вона йде. ``feed`` викликає потік, що приносить звук."""

    def __init__(self, folder: Path, clock: Callable[[], datetime]):
        self.folder = folder
        self.clock = clock
        self._lock = threading.Lock()
        self._open: dict[str, tuple] = {}  # report_id → (файл, коли почався звук)

    def start(self, report_id: str) -> None:
        with self._lock:
            if report_id in self._open:
                return
            self.folder.mkdir(parents=True, exist_ok=True)
            # З часом у назві: та сама пара після перезапуску програми не затре звук, що ще чекає на уточнення.
            path = self.folder / f"{_safe(report_id)}-{self.clock():%Y%m%d-%H%M%S}.raw"
            self._open[report_id] = (open(path, "wb"), None)

    def feed(self, pcm: bytes) -> None:
        if not self._open:
            return
        with self._lock:
            for report_id, (handle, started) in list(self._open.items()):
                if started is None:
                    # Час першого звуку мінус те, що вже в цьому шматку, — від нього рахуємо час кожної фрази.
                    started = self.clock() - timedelta(seconds=len(pcm) / BYTES_PER_SECOND)
                    self._open[report_id] = (handle, started)
                handle.write(pcm)

    def stop(self, report_id: str) -> tuple[Path, datetime] | None:
        """Пара скінчилась: (файл звуку, коли він почався) або None, якщо звуку не було."""
        with self._lock:
            item = self._open.pop(report_id, None)
        if item is None:
            return None
        handle, started = item
        handle.close()
        path = Path(handle.name)
        if started is None or path.stat().st_size < BYTES_PER_SECOND * 30:
            path.unlink(missing_ok=True)  # менше пів хвилини звуку — нічого уточнювати
            return None
        return path, started

    def close(self) -> None:
        with self._lock:
            items, self._open = list(self._open.values()), {}
        for handle, _ in items:
            handle.close()


def record_to_json(record: LessonRecord) -> dict:
    data = asdict(record)
    for key in ("start", "end", "joined", "left"):
        data[key] = data[key].isoformat()
    for key in ("transcript", "mentions", "sent"):
        data[key] = [[t.isoformat(), text] for t, text in getattr(record, key)]
    data["chat"] = [[t.isoformat(), asdict(message)] for t, message in record.chat]
    return data


def record_from_json(data: dict) -> LessonRecord:
    data = dict(data)
    for key in ("start", "end", "joined", "left"):
        data[key] = datetime.fromisoformat(data[key])
    for key in ("transcript", "mentions", "sent"):
        data[key] = [(datetime.fromisoformat(t), text) for t, text in data[key]]
    data["chat"] = [(datetime.fromisoformat(t), ChatMessage(**message)) for t, message in data["chat"]]
    return LessonRecord(**data)


# Скільки вільної пам'яті треба для уточнення large-v3 (сама модель ~2 ГБ, кожен варіант пошуку — ще ~0,5 ГБ).
# Менше — чекаємо: зранку програму вбило саме те, що уточнення не зважало на пам'ять.
MIN_FREE_GB = 4.0
BEAM_BY_FREE_GB = ((8.0, 5), (6.0, 3), (MIN_FREE_GB, 2))


def beam_for(free_gb: float | None) -> int:
    """Скільки варіантів зважувати при такій вільній пам'яті (0 — замало пам'яті, почекати)."""
    if free_gb is None:  # не знаємо (macOS) — обережно
        return 2
    return next((beam for need, beam in BEAM_BY_FREE_GB if free_gb >= need), 0)


class Refiner:
    """Фоновий потік: черга пар на уточнення. ``complete(record, path, lines)`` — уточнена розшифровка готова,
    ``fallback(record, path)`` — не вийшло (звіт із живої розшифровки).

    Уточнює окремою, точнішою моделлю (``make_transcriber``), яку вантажить лише на час розпізнавання й одразу
    вивантажує. І лише тоді, коли ``allowed()``: пари немає й найближчим часом не буде, — тож уточнення не
    забирає в пари ні процесор, ні пам'ять. Почалась пара — зупиняється й продовжує потім спочатку.
    """

    def __init__(self, folder: Path, make_transcriber: Callable[[], object | None], hint: Callable[[LessonRecord], str],
                 complete: Callable[[LessonRecord, Path | None, list[tuple[datetime, str]]], None],
                 fallback: Callable[[LessonRecord, Path | None], None],
                 allowed: Callable[[], bool] = lambda: True, free_gb: Callable[[], float | None] = lambda: None,
                 poll_seconds: float = 30):
        self.folder = folder
        self.make_transcriber = make_transcriber  # → WhisperTranscriber (None — модель ще не готова)
        self.hint = hint
        self.complete = complete
        self.fallback = fallback
        self.allowed = allowed
        self.free_gb = free_gb
        self.poll_seconds = poll_seconds
        self._jobs: deque[Path] = deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="refine", daemon=True)
        self._waiting_logged = ""

    def start(self) -> None:
        for job in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            if job.with_suffix(".raw").exists():
                log.info("Розшифровка пари чекає на уточнення (з минулого запуску)")
                self._jobs.append(job)
            else:
                job.unlink(missing_ok=True)
        for audio in self.folder.glob("*.raw") if self.folder.is_dir() else []:
            if not audio.with_suffix(".json").exists():
                audio.unlink(missing_ok=True)  # пара, яку програма не дописала (аварійно закрилась) — запису немає
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    def submit(self, record: LessonRecord, path: Path | None, audio: Path, started: datetime) -> None:
        job = audio.with_suffix(".json")
        data = {"record": record_to_json(record), "path": str(path) if path else "", "started": started.isoformat()}
        job.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self._cond:
            self._jobs.append(job)
            self._cond.notify_all()

    def pending(self) -> int:
        with self._cond:
            return len(self._jobs)

    def _next(self) -> Path | None:
        with self._cond:
            while not self._jobs and not self._stop.is_set():
                self._cond.wait(5)
            return self._jobs[0] if self._jobs and not self._stop.is_set() else None

    def _done(self, job: Path) -> None:
        with self._cond:
            if self._jobs and self._jobs[0] == job:
                self._jobs.popleft()

    def _wait(self, why: str) -> None:
        if why != self._waiting_logged:
            self._waiting_logged = why
            log.info("Уточнення розшифровки чекає: %s", why)
        self._stop.wait(self.poll_seconds)

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self._next()
            if job is None:
                return
            if not self.allowed():
                self._wait("іде пара або скоро почнеться")
                continue
            beam = beam_for(self.free_gb())
            if not beam:
                self._wait(f"замало вільної пам'яті (треба {MIN_FREE_GB:.0f} ГБ)")
                continue
            self._waiting_logged = ""
            try:
                self._refine(job, beam)
            except Paused:
                log.info("Почалась пара — уточнення відкладаю, продовжу після неї")
                continue
            except InterruptedError:
                return  # програма зупиняється — продовжимо після запуску (файли лишились)
            except Exception:  # noqa: BLE001 — не вийшло уточнити: звіт однаково має прийти
                log.exception("Не вдалося уточнити розшифровку")
                self._give_up(job)
            self._done(job)

    def _refine(self, job: Path, beam: int) -> None:
        data = json.loads(job.read_text(encoding="utf-8"))
        record = record_from_json(data["record"])
        path = Path(data["path"]) if data["path"] else None
        started = datetime.fromisoformat(data["started"])
        audio = job.with_suffix(".raw")
        transcriber = self.make_transcriber()
        if transcriber is None:  # модель ще вантажиться
            if self._stop.wait(10):
                raise InterruptedError
            raise Paused
        try:
            pcm = audio.read_bytes()
            minutes = len(pcm) / BYTES_PER_SECOND / 60
            log.info("Уточнюю розшифровку «%s» (%.0f хв звуку, модель %s, beam %d)", record.title, minutes,
                     getattr(transcriber, "name", "?"), beam)
            lines = transcriber.transcribe_long(pcm, self.hint(record), stop=self._stop, beam_size=beam,
                                                allowed=self.allowed)
        finally:
            del transcriber  # модель — геть з пам'яті ще до того, як мозок візьметься за конспект
            gc.collect()
        refined = [(started + timedelta(seconds=offset), text) for offset, text in lines]
        log.info("Розшифровку «%s» уточнено: %d фраз (наживо було %d)", record.title, len(refined),
                 len(record.transcript))
        if refined or not record.transcript:
            self.complete(record, path, refined)
        else:
            self.fallback(record, path)  # уточнення нічого не почуло, а наживо щось було — краще жива
        audio.unlink(missing_ok=True)
        job.unlink(missing_ok=True)

    def _give_up(self, job: Path) -> None:
        try:
            data = json.loads(job.read_text(encoding="utf-8"))
            self.fallback(record_from_json(data["record"]), Path(data["path"]) if data["path"] else None)
        except Exception:  # noqa: BLE001
            log.exception("Не вдалося надіслати звіт")
        job.with_suffix(".raw").unlink(missing_ok=True)
        job.unlink(missing_ok=True)
