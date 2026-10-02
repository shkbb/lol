"""Сповіщення тобі на телефон (Telegram-бот) і на робочий стіл, та команди від тебе."""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024  # більший підпис до фото/відео Telegram не прийме


class TelegramError(Exception):
    pass


@dataclass(frozen=True)
class Incoming:
    chat_id: int
    text: str
    is_reply: bool  # відповідь на якесь повідомлення бота


class TelegramBot:
    def __init__(self, token: str, chat_id: int = 0, session: requests.Session | None = None):
        self.token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self._offset = 0

    def _call(self, method: str, http_timeout: float = 20, files: dict | None = None, **params) -> object:
        url = API.format(token=self.token, method=method)
        try:
            if files:
                data = {key: str(value) for key, value in params.items()}
                response = self.session.post(url, data=data, files=files, timeout=max(http_timeout, 120))
            else:
                response = self.session.post(url, json=params, timeout=http_timeout)
            data = response.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramError(f"Telegram недоступний: {e}") from e
        if not data.get("ok"):
            raise TelegramError(f"Telegram: {data.get('description', 'помилка')}")
        return data["result"]

    def send(self, text: str) -> None:
        self._call("sendMessage", chat_id=self.chat_id, text=text[:4000])

    def _send_file(self, method: str, field: str, filename: str, content: bytes, caption: str) -> None:
        self._call(method, files={field: (filename, content)}, chat_id=self.chat_id, caption=caption[:CAPTION_LIMIT])

    def send_photo(self, content: bytes, caption: str = "") -> None:
        self._send_file("sendPhoto", "photo", "screen.jpg", content, caption)

    def send_video(self, content: bytes, filename: str, caption: str = "") -> None:
        self._send_file("sendVideo", "video", filename, content, caption)

    def send_voice(self, content: bytes, filename: str, caption: str = "") -> None:
        self._send_file("sendVoice", "voice", filename, content, caption)

    def send_document(self, content: bytes, filename: str, caption: str = "") -> None:
        self._send_file("sendDocument", "document", filename, content, caption)

    def updates(self, wait: int = 30) -> list[Incoming]:
        """Нові текстові повідомлення боту."""
        result = self._call(
            "getUpdates", http_timeout=wait + 10, offset=self._offset, timeout=wait, allowed_updates=["message"]
        )
        messages = []
        for update in result:
            self._offset = max(self._offset, update["update_id"] + 1)
            message = update.get("message") or {}
            if "text" in message:
                messages.append(Incoming(message["chat"]["id"], message["text"], "reply_to_message" in message))
        return messages

    def listen(self, handle: Callable[[str, bool], str | None], stop: threading.Event) -> None:
        """Приймає команди лише з твого чату й відповідає на них. Блокує — запускай у потоці."""
        while not stop.is_set():
            try:
                for incoming in self.updates():
                    if incoming.chat_id != self.chat_id:
                        log.warning("Ігнорую повідомлення боту з чужого чату %s", incoming.chat_id)
                        continue
                    if reply := handle(incoming.text, incoming.is_reply):
                        self.send(reply)
            except TelegramError as e:
                log.warning("%s — повторю за хвилину", e)
                stop.wait(60)


class Notifier:
    """Надсилає сповіщення у фоні, щоб повільна мережа не гальмувала решту.

    Текст іде і в Telegram, і на робочий стіл; фото, відео, голосові й файли — лише в Telegram.
    """

    def __init__(self, bot: TelegramBot | None = None, desktop: bool = True):
        self.bot = bot
        self.desktop = desktop and sys.platform.startswith("linux") and shutil.which("notify-send") is not None
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def __call__(self, text: str, urgent: bool = False) -> None:
        log.info("🔔 %s", text)

        def job() -> None:
            if self.desktop:
                subprocess.run(
                    ["notify-send", "-a", "autopara", "-u", "critical" if urgent else "normal", "autopara", text],
                    check=False,
                )
            if self.bot:
                self.bot.send(text)

        self._queue.put(job)

    def _media(self, send: Callable[[TelegramBot], None], caption: str, what: str) -> None:
        log.info("🔔 [%s] %s", what, caption.splitlines()[0] if caption else "")
        if not self.bot:
            return

        def job() -> None:
            send(self.bot)
            if len(caption) > CAPTION_LIMIT:  # повний текст — окремим повідомленням
                self.bot.send(caption)

        self._queue.put(job)

    def photo(self, content: bytes, caption: str = "") -> None:
        self._media(lambda bot: bot.send_photo(content, caption), caption, "фото")

    def video(self, content: bytes, filename: str, caption: str = "") -> None:
        self._media(lambda bot: bot.send_video(content, filename, caption), caption, "відео")

    def voice(self, content: bytes, filename: str, caption: str = "") -> None:
        self._media(lambda bot: bot.send_voice(content, filename, caption), caption, "аудіо")

    def document(self, content: bytes, filename: str, caption: str = "") -> None:
        self._media(lambda bot: bot.send_document(content, filename, caption), caption, filename)

    def _run(self) -> None:
        while True:
            job = self._queue.get()
            try:
                job()
            except TelegramError as e:
                log.warning("Не вдалося надіслати сповіщення: %s", e)
            except Exception:  # noqa: BLE001 — одне невдале сповіщення не має зупиняти решту
                log.exception("Помилка під час надсилання сповіщення")
            finally:
                self._queue.task_done()

    def flush(self, timeout: float = 5) -> None:
        """Дочекатися відправки всього, що в черзі (наприклад, перед виходом)."""
        done = threading.Event()
        threading.Thread(target=lambda: (self._queue.join(), done.set()), daemon=True).start()
        done.wait(timeout)
