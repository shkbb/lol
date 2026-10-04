"""Вбудований браузер програми (Chromium від Playwright): є він чи ні, і докачати, якщо немає.

Програма не залежить від того, чи стоїть у системі Chrome чи Edge: у неї свій браузер з окремим
профілем. Інсталятор ставить його одразу; якщо не вийшло (не було інтернету), програма докачає сама,
поки людина проходить майстер першого запуску.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_lock = threading.Lock()
_state = {"status": "unknown", "error": ""}  # unknown | ready | installing | error


def _resolve() -> str:
    """Де Playwright чекає свій Chromium. В окремому потоці: sync Playwright не можна запускати
    вдруге в потоці, де він уже працює (а в потоці контролера він працює)."""
    result: dict = {}

    def find() -> None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                result["path"] = playwright.chromium.executable_path
        except Exception as e:  # noqa: BLE001 — немає драйвера Playwright тощо
            result["error"] = e

    thread = threading.Thread(target=find, name="browser-path", daemon=True)
    thread.start()
    thread.join(60)
    return result.get("path", "")


def installed() -> str | None:
    """Шлях до вбудованого браузера або None, якщо його ще не завантажено."""
    path = _resolve()
    return path if path and Path(path).exists() else None


def _install() -> tuple[bool, str]:
    # --no-shell: без окремого «безголового» браузера — він нам не потрібен, а завантаження вдвічі менше.
    command = [sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"]
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", **flags)
    if result.returncode == 0:
        return True, ""
    tail = (result.stderr or result.stdout).strip().splitlines()[-2:]
    return False, " ".join(tail)[-300:] or f"код {result.returncode}"


def ensure() -> bool:
    """Переконатися, що браузер є; якщо немає — завантажити (~150 МБ, один раз). True — готовий."""
    with _lock:
        if installed():
            _state.update(status="ready", error="")
            return True
        _state.update(status="installing", error="")
        log.info("Завантажую браузер програми (один раз, ~150 МБ)...")
        ok, error = _install()
        if ok and installed():
            _state.update(status="ready", error="")
            log.info("Браузер програми готовий")
            return True
        message = "не вдалося завантажити браузер програми — перевір інтернет, програма спробує ще раз"
        _state.update(status="error", error=f"{message} ({error})" if error else message)
        log.error("%s: %s", message, error)
        return False


def status() -> dict:
    """{"status": ready | installing | error | unknown, "error": "..."} — для вікна програми."""
    return dict(_state)


def prepare_in_background() -> threading.Thread:
    """Докачати браузер у фоні, поки людина проходить майстер."""
    thread = threading.Thread(target=ensure, name="browser-setup", daemon=True)
    thread.start()
    return thread
