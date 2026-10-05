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

import json
import logging
import re
import subprocess
import sys
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
MAX_FAILURES = 2  # стільки разів поспіль не вийшло (скажімо, процес убило через пам'ять) — звіт з живої


def beam_for(free_gb: float | None) -> int:
    """Скільки варіантів зважувати при такій вільній пам'яті (0 — замало пам'яті, почекати)."""
    if free_gb is None:  # не знаємо (macOS) — обережно
        return 2
    return next((beam for need, beam in BEAM_BY_FREE_GB if free_gb >= need), 0)


class Refiner:
    """Фоновий потік: черга пар на уточнення. ``complete(record, path, lines)`` — уточнена розшифровка готова,
    ``fallback(record, path)`` — не вийшло (звіт із живої розшифровки).

    Розшифровує окремий процес (refine_worker.py) з точнішою моделлю: після нього система отримує всю пам'ять
    назад, а якщо її забракне — ядро вб'є саме його, не програму. І лише тоді, коли ``allowed()``: пари немає
    й найближчим часом не буде. Почалась пара — процес зупиняємо, а запис чекає наступного вільного часу.
    """

    def __init__(self, folder: Path, model: str, language: str, hint: Callable[[LessonRecord], str],
                 complete: Callable[[LessonRecord, Path | None, list[tuple[datetime, str]]], None],
                 fallback: Callable[[LessonRecord, Path | None], None],
                 allowed: Callable[[], bool] = lambda: True, free_gb: Callable[[], float | None] = lambda: None,
                 poll_seconds: float = 30, worker: Callable[[Path, int, str], list[str]] | None = None):
        self.folder = folder
        self.model = model
        self.language = language
        self.hint = hint
        self.complete = complete
        self.fallback = fallback
        self.allowed = allowed
        self.free_gb = free_gb
        self.poll_seconds = poll_seconds
        self.worker = worker or self._worker_command
        self._jobs: deque[Path] = deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="refine", daemon=True)
        self._waiting_logged = ""

    def _worker_command(self, job: Path, beam: int, hint: str) -> list[str]:
        return [sys.executable, "-m", "autopara.refine_worker", str(job), "--model", self.model,
                "--language", self.language, "--beam", str(beam), "--hint", hint]

    def start(self) -> None:
        for job in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            if job.name.endswith(".result.json"):
                continue
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
            except Exception as e:  # noqa: BLE001 — не вийшло уточнити: звіт однаково має прийти
                if self._failed_again(job):
                    log.warning("Не вдалося уточнити розшифровку (%s) — звіт буде з живої розшифровки", e)
                    self._give_up(job)
                else:
                    log.warning("Не вдалося уточнити розшифровку (%s) — спробую ще раз", e)
                    continue
            self._done(job)

    def _failed_again(self, job: Path) -> bool:
        """Друга невдача поспіль (наприклад, процес двічі вбило ядро через пам'ять) — більше не пробуємо."""
        data = json.loads(job.read_text(encoding="utf-8"))
        data["failures"] = data.get("failures", 0) + 1
        job.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data["failures"] >= MAX_FAILURES

    def _refine(self, job: Path, beam: int) -> None:
        data = json.loads(job.read_text(encoding="utf-8"))
        record = record_from_json(data["record"])
        path = Path(data["path"]) if data["path"] else None
        started = datetime.fromisoformat(data["started"])
        result = job.with_suffix(".result.json")
        result.unlink(missing_ok=True)
        minutes = job.with_suffix(".raw").stat().st_size / BYTES_PER_SECOND / 60
        log.info("Уточнюю розшифровку «%s» (%.0f хв звуку, модель %s, beam %d) окремим процесом", record.title,
                 minutes, self.model, beam)
        process = subprocess.Popen(self.worker(job, beam, self.hint(record)), stdin=subprocess.DEVNULL)
        try:
            while process.poll() is None:
                if self._stop.wait(2):
                    raise InterruptedError
                if not self.allowed():
                    raise Paused
        finally:
            if process.poll() is None:
                process.terminate()  # пара починається або програма закривається — пам'ять і процесор їй
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
        if process.returncode != 0 or not result.exists():
            raise RuntimeError(f"процес уточнення завершився з кодом {process.returncode}")
        lines = json.loads(result.read_text(encoding="utf-8"))
        result.unlink(missing_ok=True)
        refined = [(started + timedelta(seconds=offset), text) for offset, text in lines]
        log.info("Розшифровку «%s» уточнено: %d фраз (наживо було %d)", record.title, len(refined),
                 len(record.transcript))
        if refined or not record.transcript:
            self.complete(record, path, refined)
        else:
            self.fallback(record, path)  # уточнення нічого не почуло, а наживо щось було — краще жива
        job.with_suffix(".raw").unlink(missing_ok=True)
        job.unlink(missing_ok=True)

    def _give_up(self, job: Path) -> None:
        try:
            data = json.loads(job.read_text(encoding="utf-8"))
            self.fallback(record_from_json(data["record"]), Path(data["path"]) if data["path"] else None)
        except Exception:  # noqa: BLE001
            log.exception("Не вдалося надіслати звіт")
        job.with_suffix(".raw").unlink(missing_ok=True)
        job.with_suffix(".result.json").unlink(missing_ok=True)
        job.unlink(missing_ok=True)
