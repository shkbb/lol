"""Сповіщення тобі на телефон (Telegram-бот) і на робочий стіл, та команди від тебе."""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
from typing import Callable

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(Exception):
    pass


class TelegramBot:
    def __init__(self, token: str, chat_id: int = 0, session: requests.Session | None = None):
        self.token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self._offset = 0

    def _call(self, method: str, http_timeout: float = 20, **params) -> object:
        try:
            url = API.format(token=self.token, method=method)
            response = self.session.post(url, json=params, timeout=http_timeout)
            data = response.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramError(f"Telegram недоступний: {e}") from e
        if not data.get("ok"):
            raise TelegramError(f"Telegram: {data.get('description', 'помилка')}")
        return data["result"]

    def send(self, text: str) -> None:
        self._call("sendMessage", chat_id=self.chat_id, text=text[:4000])

    def updates(self, wait: int = 30) -> list[tuple[int, str]]:
        """Нові текстові повідомлення боту: [(chat_id, текст)]."""
        result = self._call(
            "getUpdates", http_timeout=wait + 10, offset=self._offset, timeout=wait, allowed_updates=["message"]
        )
        messages = []
        for update in result:
            self._offset = max(self._offset, update["update_id"] + 1)
            message = update.get("message") or {}
            if "text" in message:
                messages.append((message["chat"]["id"], message["text"]))
        return messages

    def listen(self, handle: Callable[[str], str], stop: threading.Event) -> None:
        """Приймає команди лише з твого чату й відповідає на них. Блокує — запускай у потоці."""
        while not stop.is_set():
            try:
                for chat_id, text in self.updates():
                    if chat_id != self.chat_id:
                        log.warning("Ігнорую повідомлення боту з чужого чату %s", chat_id)
                        continue
                    if reply := handle(text):
                        self.send(reply)
            except TelegramError as e:
                log.warning("%s — повторю за хвилину", e)
                stop.wait(60)


class Notifier:
    """Надсилає сповіщення у фоні, щоб повільна мережа не гальмувала решту."""

    def __init__(self, bot: TelegramBot | None = None, desktop: bool = True):
        self.bot = bot
        self.desktop = desktop and sys.platform.startswith("linux") and shutil.which("notify-send") is not None
        self._queue: queue.Queue[tuple[str, bool]] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def __call__(self, text: str, urgent: bool = False) -> None:
        log.info("🔔 %s", text)
        self._queue.put((text, urgent))

    def _run(self) -> None:
        while True:
            text, urgent = self._queue.get()
            if self.desktop:
                subprocess.run(
                    ["notify-send", "-a", "autopara", "-u", "critical" if urgent else "normal", "autopara", text],
                    check=False,
                )
            if self.bot:
                try:
                    self.bot.send(text)
                except TelegramError as e:
                    log.warning("Не вдалося надіслати сповіщення: %s", e)
            self._queue.task_done()

    def flush(self, timeout: float = 5) -> None:
        """Дочекатися відправки всього, що в черзі (наприклад, перед виходом)."""
        done = threading.Event()
        threading.Thread(target=lambda: (self._queue.join(), done.set()), daemon=True).start()
        done.wait(timeout)
