"""Агент для важкої роботи: Antigravity CLI (``agy``) без інтерфейсу.

Програма запускає офіційний ``agy`` як звичайну програму в окремій теці завдання — так дозволяють
умови Antigravity, і ліміти йдуть з твоєї підписки. Сама умова лежить у файлі ``TASK.md`` у цій теці,
а агентові передаємо коротке доручення її прочитати: так довгі умови, лапки чи символи не ламають
командний рядок (особливо на Windows).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from time import monotonic

from .config import AgentSettings

log = logging.getLogger(__name__)

TEXT_KEYS = ("result", "response", "text", "output", "content", "message", "answer")
# Не провал роботи, а «зачекай»: ліміт підписки чи потрібен вхід. Спроби на таке не витрачаємо.
LIMIT_RE = re.compile(r"quota|rate.?limit|resource.?exhausted|\b429\b|too many requests|usage limit", re.I)
AUTH_RE = re.compile(r"not (?:logged|signed) in|log ?in required|sign ?in|unauthenticated|\b401\b|auth(?:entication)? required", re.I)
LIMIT_PAUSE = timedelta(hours=1)
AUTH_PAUSE = timedelta(minutes=30)


class AgentError(Exception):
    pass


@dataclass
class AgentResult:
    ok: bool
    text: str
    error: str = ""
    seconds: float = 0.0


def parse_output(stdout: str) -> tuple[str, bool]:
    """Текст відповіді й чи це помилка — з JSON, потоку JSON-рядків або просто тексту."""
    stdout = stdout.strip()
    candidates = []
    try:
        candidates.append(json.loads(stdout))
    except ValueError:
        for line in stdout.splitlines():  # stream-json: беремо останній рядок з результатом
            try:
                candidates.append(json.loads(line))
            except ValueError:
                continue
    for data in reversed(candidates):
        if not isinstance(data, dict):
            continue
        failed = bool(data.get("is_error")) or data.get("subtype", "").startswith("error")
        for key in TEXT_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), failed
            if isinstance(value, dict):  # {"message": {"content": "..."}}
                inner = value.get("content") or value.get("text")
                if isinstance(inner, str) and inner.strip():
                    return inner.strip(), failed
        if failed:
            return str(data.get("error") or data), True
    return stdout, False


class Agent:
    def __init__(self, settings: AgentSettings, usage_file: Path):
        self.settings = settings
        self.usage_file = usage_file
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.cancelled = False
        self.paused_until: datetime | None = None
        self.pause_reason = ""

    def problem(self) -> str | None:
        """None — агент готовий; інакше — чого бракує."""
        if shutil.which(self.settings.command) is None:
            return (
                f"не знайдено «{self.settings.command}» — встанови Antigravity CLI й один раз увійди "
                "(запусти agy у терміналі)"
            )
        if self.used_today() >= self.settings.daily_limit:
            return f"на сьогодні вичерпано ліміт запусків агента ({self.settings.daily_limit})"
        if self.paused_until is not None and datetime.now() < self.paused_until:
            return f"{self.pause_reason} — спробую знову о {self.paused_until:%H:%M}"
        return None

    def used_today(self) -> int:
        try:
            usage = json.loads(self.usage_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return int(usage.get(date.today().isoformat(), 0))

    def _count(self) -> None:
        today = date.today().isoformat()
        self.usage_file.parent.mkdir(parents=True, exist_ok=True)
        self.usage_file.write_text(json.dumps({today: self.used_today() + 1}), encoding="utf-8")

    def run(self, instruction: str, workdir: Path, timeout_minutes: float | None = None) -> AgentResult:
        """Виконати доручення в теці ``workdir`` (там уже лежать TASK.md і матеріали)."""
        if problem := self.problem():
            raise AgentError(problem)
        executable = shutil.which(self.settings.command) or self.settings.command
        command = [executable, *self.settings.args, "-p", instruction]
        self._count()
        started = monotonic()
        timeout = (timeout_minutes or self.settings.timeout_minutes) * 60
        log.info("Агент: працюю в %s", workdir)
        # Окрема група процесів — щоб при скасуванні зупинити й усе, що агент запустив (браузер, сервери).
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
        try:
            process = subprocess.Popen(
                command,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                **group,
            )
        except OSError as e:
            return AgentResult(False, "", f"не вдалося запустити агента: {e}", monotonic() - started)
        with self._lock:
            self._process = process
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(process)
            process.communicate()
            return AgentResult(False, "", f"агент не впорався за {int(timeout // 60)} хв", monotonic() - started)
        finally:
            with self._lock:
                self._process = None
        if self.cancelled:
            return AgentResult(False, "", "агента зупинено", monotonic() - started)
        text, failed = parse_output(stdout)
        seconds = monotonic() - started
        if process.returncode != 0 or failed:
            error = (stderr.strip() or text)[-800:] or f"код виходу {process.returncode}"
            self._pause_if_needed(error)
            return AgentResult(False, text, error, seconds)
        return AgentResult(True, text, "", seconds)

    def _pause_if_needed(self, error: str) -> None:
        """Ліміт підписки чи вхід — це не провал завдання: робимо паузу й кидаємо AgentError."""
        if LIMIT_RE.search(error):
            self.pause_reason, pause = "Antigravity: вичерпано ліміт підписки", LIMIT_PAUSE
        elif AUTH_RE.search(error):
            self.pause_reason, pause = "Antigravity просить увійти — запусти agy у терміналі й увійди", AUTH_PAUSE
        else:
            return
        self.paused_until = datetime.now() + pause
        raise AgentError(self.problem() or self.pause_reason)

    def cancel(self) -> None:
        """Зупинити агента (програму закривають чи перезапускають) — разом з усім, що він запустив."""
        self.cancelled = True
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            _kill_tree(process)


def _kill_tree(process: subprocess.Popen) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        process.kill()
