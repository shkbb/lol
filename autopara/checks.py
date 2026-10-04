"""Що налаштовано, а що ні — для `autopara check` і для вікна програми."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from .config import Config
from .rozklad import RozkladError
from .sources import make_source, resolve_target


@dataclass(frozen=True)
class Check:
    ok: bool | None  # None — вимкнено або не налаштовано, і це не помилка
    area: str
    text: str


def zoom_client_found() -> bool:
    """Чи є Zoom-клієнт. На Linux — команда zoom або будь-яка програма, що відкриває посилання zoommtg://
    (так знаходиться й Zoom з Flatpak). На Windows і macOS шукати незручно — вважаємо, що є."""
    if sys.platform != "linux" or shutil.which("zoom"):
        return True
    try:
        query = ["xdg-mime", "query", "default", "x-scheme-handler/zoommtg"]
        return bool(subprocess.run(query, capture_output=True, text=True, timeout=5).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def run_checks(config: Config, telegram_ping: bool = True) -> list[Check]:
    from .brain import OllamaBrain
    from .ears import list_input_devices, pick_source, pulse_available, whisper_available
    from .media import ffmpeg_available
    from .telegram import TelegramBot, TelegramError

    checks = []
    try:
        target = resolve_target(make_source(config), config)
        checks.append(Check(True, "розклад", target.student_name or target.group_name or target.uid))
    except RozkladError as e:
        checks.append(Check(False, "розклад", str(e)))
    from . import browser_setup

    browser = browser_setup.installed()
    checks.append(Check(bool(browser), "браузер", "браузер програми готовий" if browser else "ще не завантажено — програма завантажить сама"))
    checks.append(
        Check(
            config.profile_dir.exists(),
            "вхід у Google",
            "профіль браузера є" if config.profile_dir.exists() else "ще не входив — натисни «Увійти в Google»",
        )
    )
    if config.join.zoom_mode == "app":
        found = zoom_client_found()
        checks.append(Check(found, "Zoom", "клієнт знайдено" if found else "встанови Zoom з zoom.us/download"))
    if config.telegram.clips and config.telegram.mentions:
        has_ffmpeg = ffmpeg_available()
        checks.append(Check(has_ffmpeg, "відеовирізки", "ffmpeg є" if has_ffmpeg else "немає ffmpeg — перевстанови autopara"))
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
        problem = whisper_available()
        where = "звук зі сторінки пари"
        if pick_source(config.ears.source) == "pulse":
            pulse = pulse_available()
            where = "системний звук" if pulse is None else f"звук зі сторінки пари, бо системний недоступний: {pulse}"
        checks.append(Check(problem is None, "вуха", problem or f"готові ({config.ears.model}, {where})"))
        if config.join.zoom_mode == "app":
            if pulse_available() is None:
                checks.append(Check(True, "вуха в Zoom", "Zoom-клієнт чую через PipeWire/PulseAudio"))
            elif not config.ears.app_device:
                checks.append(Check(False, "вуха в Zoom", "Zoom-клієнт не чую — обери віртуальний кабель або Zoom у браузері"))
            else:
                found = any(config.ears.app_device.casefold() in name.casefold() for name in list_input_devices())
                text = f"слухаю «{config.ears.app_device}»" if found else f"пристрій «{config.ears.app_device}» не знайдено"
                checks.append(Check(found, "вуха в Zoom", text))
    else:
        checks.append(Check(None, "вуха", "вимкнено"))
    return checks
