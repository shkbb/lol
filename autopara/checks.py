"""Що налаштовано, а що ні — для `autopara check` і для вікна програми."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .rozklad import RozkladError
from .sources import make_source, resolve_target

CHROME_NAMES = ["google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "microsoft-edge", "msedge"]
CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]


@dataclass(frozen=True)
class Check:
    ok: bool | None  # None — вимкнено або не налаштовано, і це не помилка
    area: str
    text: str


def find_chrome(config: Config | None = None) -> str | None:
    if config is not None and config.join.browser_path:
        return config.join.browser_path if Path(config.join.browser_path).exists() else None
    for name in CHROME_NAMES:
        if found := shutil.which(name):
            return found
    return next((path for path in CHROME_PATHS if Path(path).exists()), None)


def install_hint(linux: str, other: str) -> str:
    return linux if sys.platform.startswith("linux") else other


def run_checks(config: Config, telegram_ping: bool = True) -> list[Check]:
    from .brain import OllamaBrain
    from .ears import ears_available
    from .telegram import TelegramBot, TelegramError

    checks = []
    try:
        target = resolve_target(make_source(config), config)
        checks.append(Check(True, "розклад", target.student_name or target.group_name or target.uid))
    except RozkladError as e:
        checks.append(Check(False, "розклад", str(e)))
    chrome = find_chrome(config)
    checks.append(Check(bool(chrome), "браузер", chrome or "Chrome не знайдено — встанови Google Chrome"))
    checks.append(
        Check(
            config.profile_dir.exists(),
            "вхід у Google",
            "профіль браузера є" if config.profile_dir.exists() else "ще не входив — натисни «Увійти в Google»",
        )
    )
    if config.join.zoom_mode == "app":
        zoom = shutil.which("zoom") or (sys.platform != "linux" and "zoom")  # на Windows/Mac шукати незручно
        checks.append(Check(bool(zoom), "Zoom", "клієнт знайдено" if zoom else install_hint("yay -S zoom", "встанови Zoom")))
    if config.telegram.clips:
        has_ffmpeg = shutil.which("ffmpeg") is not None
        hint = install_hint("sudo pacman -S ffmpeg", "встанови ffmpeg")
        checks.append(Check(has_ffmpeg, "відеовирізки", "ffmpeg є" if has_ffmpeg else f"немає ffmpeg — {hint}"))
    if config.telegram.token and config.telegram.chat_id:
        try:
            if telegram_ping:
                TelegramBot(config.telegram.token, config.telegram.chat_id).send("✓ autopara: перевірка зв'язку")
            checks.append(Check(True, "Telegram", "бот на зв'язку"))
        except TelegramError as e:
            checks.append(Check(False, "Telegram", str(e)))
    else:
        checks.append(Check(None, "Telegram", "не налаштовано"))
    if config.brain.enabled:
        problem = OllamaBrain(config.brain.url, config.brain.model).check()
        checks.append(Check(problem is None, "мозок", problem or config.brain.model))
    else:
        checks.append(Check(None, "мозок", "вимкнено"))
    if config.ears.enabled:
        problem = ears_available()
        if problem is None and importlib.util.find_spec("faster_whisper") is None:
            problem = 'немає faster-whisper — pip install "autopara[ears]"'
        checks.append(Check(problem is None, "вуха", problem or f"готові ({config.ears.model})"))
    else:
        checks.append(Check(None, "вуха", "вимкнено"))
    return checks
