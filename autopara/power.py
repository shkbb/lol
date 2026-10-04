"""Не дає комп'ютеру заснути від бездіяльності, поки йде пара або до наступної менше доби.

Linux — systemd-inhibit, macOS — caffeinate, Windows — SetThreadExecutionState.
Закрита кришка ноута, як і раніше, присипляє його.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

# Windows: ES_CONTINUOUS — стан діє до наступного виклику; ES_SYSTEM_REQUIRED — не засинати (екран гасне як завжди).
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _windows_execution_state(flags: int) -> bool:
    import ctypes

    return ctypes.windll.kernel32.SetThreadExecutionState(flags) != 0  # type: ignore[attr-defined]


class SleepGuard:
    def __init__(self) -> None:
        if sys.platform == "win32":
            self._backend = "windows"
        elif sys.platform == "darwin":
            self._backend = "caffeinate" if shutil.which("caffeinate") else ""
        else:
            self._backend = "systemd" if sys.platform.startswith("linux") and shutil.which("systemd-inhibit") else ""
        self.available = bool(self._backend)
        self._process: subprocess.Popen | None = None
        self._windows_held = False

    @property
    def holding(self) -> bool:
        if self._backend == "windows":
            return self._windows_held
        return self._process is not None and self._process.poll() is None

    def hold(self, needed: bool) -> None:
        if not self.available or needed == self.holding:
            return
        if needed:
            if self._start():
                log.info("Не даю комп'ютеру заснути: до пари менше доби (екран гасне як завжди)")
        else:
            self.release()
            log.info("Найближчої доби пар немає — комп'ютер знову може засинати")

    def _start(self) -> bool:
        if self._backend == "windows":
            # Стан прив'язаний до потоку: знімається в release() з того ж потоку або коли потік завершиться.
            if _windows_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                self._windows_held = True
                return True
            return self._give_up("SetThreadExecutionState відмовив")
        if self._backend == "caffeinate":
            # -w: блокування живе, поки живе autopara.
            command = ["caffeinate", "-i", "-w", str(os.getpid())]
        else:
            # Блокуємо сон і засинання від бездіяльності. `tail --pid` завершиться разом з нами,
            # навіть якщо нас вб'ють, тож «вічної» заборони сну не лишиться.
            command = [
                "systemd-inhibit",
                "--what=sleep:idle",
                "--who=autopara",
                "--why=Скоро пара — autopara зайде на неї сама",
                "--mode=block",
                "tail",
                f"--pid={os.getpid()}",
                "-f",
                "/dev/null",
            ]
        self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            self._process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            return True
        error = (self._process.stderr.read() or "").strip()
        self._process = None
        return self._give_up(error)

    def _give_up(self, error: str) -> bool:
        self.available = False  # не пробуємо щоразу знову
        log.warning("Не вдалося заборонити сон (%s) — стеж за цим сам", error)
        return False

    def release(self) -> None:
        if self._windows_held:
            _windows_execution_state(ES_CONTINUOUS)
            self._windows_held = False
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                self._process.wait(timeout=5)
            self._process = None
