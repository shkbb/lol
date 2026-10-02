"""Не дає ноуту заснути від бездіяльності, поки сьогодні ще є пари (Linux, systemd-inhibit)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)


class SleepGuard:
    def __init__(self) -> None:
        self.available = sys.platform.startswith("linux") and shutil.which("systemd-inhibit") is not None
        self._process: subprocess.Popen | None = None

    @property
    def holding(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def hold(self, needed: bool) -> None:
        if not self.available or needed == self.holding:
            return
        if needed:
            # Блокуємо сон і засинання від бездіяльності; закриття кришки, як і раніше, присипляє ноут.
            # Блокування живе, поки живе autopara: `tail --pid` завершиться разом з нами,
            # навіть якщо нас вб'ють, тож «вічної» заборони сну не лишиться.
            self._process = subprocess.Popen(
                [
                    "systemd-inhibit",
                    "--what=sleep:idle",
                    "--who=autopara",
                    "--why=Сьогодні ще є пари",
                    "--mode=block",
                    "tail",
                    f"--pid={os.getpid()}",
                    "-f",
                    "/dev/null",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self._process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                log.info("Не даю ноуту заснути, поки не скінчаться сьогоднішні пари")
                return
            error = (self._process.stderr.read() or "").strip()
            self._process = None
            self.available = False  # не пробуємо щоразу знову
            log.warning("Не вдалося заборонити сон через systemd-inhibit (%s) — стеж за цим сам", error)
        else:
            self.release()
            log.info("Пари на сьогодні скінчились — ноут знову може засинати")

    def release(self) -> None:
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                self._process.wait(timeout=5)
            self._process = None
