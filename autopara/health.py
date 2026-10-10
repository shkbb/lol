"""Сторож: попереджає в Telegram про проблеми, які інакше мовчазні, — і одразу каже, що з ними зробити.

Раз на кілька хвилин перевіряє: місце на диску, чи відповідає Ollama (мозок), чи не застрягло уточнення після пари,
чи давно не оновлювався розклад. Нова проблема — «⚠️ що сталося + що зробити»; триває — нагадування не частіше ніж
раз на 12 год; минула — «✅ знову гаразд». (Вуха, що оглухли на парі, стереже Runner — там свій сторож.)
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

PERIOD = 600  # с між перевірками
REPEAT = 12 * 3600  # с між нагадуваннями про ту саму проблему
MIN_FREE_GB = 3.0
REFINE_STUCK_HOURS = 24
SCHEDULE_STALE_HOURS = 48


@dataclass(frozen=True)
class Problem:
    key: str
    text: str  # що сталося й чим це загрожує
    fix: str  # що зробити
    ok: str  # що написати, коли минуло


Check = Callable[[], "Problem | None"]


class Health:
    def __init__(self, notify: Callable[[str], None], checks: list[Check], period: float = PERIOD,
                 repeat: float = REPEAT, clock: Callable[[], float] = time.monotonic):
        self.notify = notify
        self.checks = checks
        self.period = period
        self.repeat = repeat
        self.clock = clock
        self._active: dict[str, tuple[float, Problem]] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="health", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self._stop.wait(60)  # дати програмі розкрутитися (Ollama, розклад)
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self.period)

    def step(self) -> None:
        now = self.clock()
        seen = set()
        for check in self.checks:
            try:
                problem = check()
            except Exception:  # noqa: BLE001 — зламана перевірка не має глушити решту
                log.debug("Сторож: перевірка впала", exc_info=True)
                continue
            if problem is None:
                continue
            seen.add(problem.key)
            last = self._active.get(problem.key)
            if last is None or now - last[0] >= self.repeat:
                log.warning("Сторож: %s", problem.text)
                self.notify(f"⚠️ {problem.text}\n🛠 Що зробити: {problem.fix}")
                self._active[problem.key] = (now, problem)
        for key in [key for key in self._active if key not in seen]:
            _, problem = self._active.pop(key)
            self.notify(f"✅ {problem.ok}")


def disk_check(folder: Path, min_gb: float = MIN_FREE_GB) -> Check:
    def check() -> Problem | None:
        target = folder if folder.exists() else Path.home()
        free = shutil.disk_usage(target).free / 1e9
        if free >= min_gb:
            return None
        return Problem(
            "disk", f"Мало місця на диску: вільно {free:.1f} ГБ. Записи пар і звук для уточнення можуть не зберегтися.",
            "звільни кілька гігабайт: кошик, «Завантаження», старі відео. Великі мовні моделі — у терміналі "
            "`ollama list`, непотрібні прибрати `ollama rm <назва>`.",
            "Місця на диску знову достатньо.")
    return check


def ollama_check(reachable: Callable[[], bool]) -> Check:
    """Ollama, що працювала, а тоді перестала (дві перевірки поспіль). Якщо її взагалі не встановлено — мовчимо:
    про це каже вікно програми, а не нагадування кожні 12 годин."""
    state = {"seen": False, "misses": 0}

    def check() -> Problem | None:
        if reachable():
            state["seen"], state["misses"] = True, 0
            return None
        state["misses"] += 1
        if not state["seen"] or state["misses"] < 2:
            return None
        fix = ("перезапусти Ollama: у терміналі `sudo systemctl restart ollama` (або просто перезавантаж ноутбук)."
               if sys.platform.startswith("linux") else
               "запусти програму Ollama (значок біля годинника чи в меню «Пуск»/«Програми») або перезавантаж комп'ютер.")
        return Problem(
            "ollama", "Мозок (Ollama) не відповідає: не буде сповіщень про оголошення на парі, підсумків пар і відповідей "
            "бота на питання (розклад і команди працюють).", fix, "Мозок (Ollama) знову працює.")
    return check


def refine_check(folder: Path, hours: float = REFINE_STUCK_HOURS, clock: Callable[[], float] = time.time) -> Check:
    """Уточнення після пари (refine.py): запис чекає в черзі добу й довше — щось не так."""
    def check() -> Problem | None:
        jobs = [path for path in folder.glob("*.json") if not path.name.endswith(".result.json")] if folder.is_dir() else []
        if not jobs:
            return None
        oldest = min(path.stat().st_mtime for path in jobs)
        waited = (clock() - oldest) / 3600
        if waited < hours:
            return None
        return Problem(
            "refine", f"Уточнення після пари стоїть уже {waited:.0f} год (у черзі {len(jobs)}): звіти з точнішою "
            "розшифровкою не приходять.",
            "перезапусти програму. Якщо повториться — «Налаштування» → «Більше налаштувань» → «Уточнення після пари» → "
            "«не уточнювати» (звіт приходитиме одразу) і перешли розробнику файл ~/.autopara/autopara.log.",
            "Уточнення після пари знову йде.")
    return check


def schedule_check(cache: Path, hours: float = SCHEDULE_STALE_HOURS, clock: Callable[[], float] = time.time) -> Check:
    """Розклад давно не оновлювався (сайт лежить чи немає інтернету) — заходимо за збереженою копією."""
    def check() -> Problem | None:
        if not cache.exists():
            return None
        age = (clock() - cache.stat().st_mtime) / 3600  # копію переписуємо після кожного вдалого оновлення
        if age < hours:
            return None
        return Problem(
            "schedule", f"Розклад не оновлювався {age:.0f} год (сайт розкладу недоступний або немає інтернету) — на пари "
            "заходжу за збереженою копією.",
            "перевір інтернет. Якщо інтернет є, а сайт розкладу лежить, — робити нічого не треба; але якщо пару "
            "перенесли, встав її посилання вручну: «Налаштування» → «Посилання».",
            "Розклад знову оновлюється.")
    return check
