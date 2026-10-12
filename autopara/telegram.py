"""Сповіщення тобі на телефон (Telegram-бот) і на робочий стіл, та команди від тебе."""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable
from xml.sax.saxutils import escape

import requests

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024  # більший підпис до фото/відео Telegram не прийме


class TelegramError(Exception):
    pass


FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
TEXT_LIMIT = 4000  # Telegram приймає до 4096 знаків в одному повідомленні
THINKING = "👀"  # реакція на твоє повідомлення: «прочитав, думаю» — замість окремого «Думаю…»


@dataclass(frozen=True)
class Incoming:
    chat_id: int
    text: str
    is_reply: bool  # відповідь на якесь повідомлення бота
    callback_id: str = ""  # натиснута кнопка під повідомленням бота
    replied: str = ""  # текст повідомлення бота, на яке відповіли
    message_id: int = 0  # твоє повідомлення — на нього бот відповідає реплаєм
    voice: str = ""  # file_id голосового повідомлення
    photo: str = ""  # file_id фото (найбільший розмір, що не завеликий)
    menu_id: int = 0  # повідомлення бота, під яким натиснули кнопку (меню налаштувань оновлюємо на місці)


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Довгу відповідь — кількома повідомленнями, розрізаючи між абзацами чи рядками, а не посеред слова."""
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit))
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return parts + ([rest] if rest else [])


Buttons = list[list[tuple[str, str]]]  # рядки кнопок: (підпис, що «написати» боту при натисканні)


class TelegramBot:
    def __init__(self, token: str, chat_id: int = 0, session: requests.Session | None = None):
        self.token = token
        self.chat_id = chat_id
        self.session = session or requests.Session()
        self._offset = 0
        # Чи йде відповідь на це повідомлення бота в чат пари (решта відповідей — розмова з ботом).
        self.reply_goes_to_chat: Callable[[str], bool] = lambda replied: True
        self.current: Incoming | None = None  # повідомлення, яке зараз обробляємо
        self.on_sent: Callable[[str], None] | None = None  # що бот написав (для пам'яті розмови)

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

    def me(self) -> dict:
        """Хто цей бот: {"username": ..., "first_name": ...}. Заодно перевіряє токен."""
        return self._call("getMe")

    def ack(self) -> None:
        """Підтвердити Telegram, що вже прочитані оновлення оброблено, щоб вони не прийшли знову."""
        self._call("getUpdates", offset=self._offset, timeout=0, limit=1)

    def send(self, text: str, buttons: Buttons | None = None, reply_to: int = 0) -> None:
        """Повідомлення (довге — кількома). ``reply_to`` — відповісти реплаєм на твоє повідомлення: так відповідь
        не сплутаєш зі звітом про пару, що прийшов саме тоді."""
        parts = split_text(text) or [""]
        for index, part in enumerate(parts):
            params: dict = {"chat_id": self.chat_id, "text": part}
            if reply_to and index == 0:
                params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if buttons and index == len(parts) - 1:
                params["reply_markup"] = _keyboard(buttons)
            self._call("sendMessage", **params)
        if self.on_sent is not None and text.strip():
            try:
                self.on_sent(text)
            except Exception:  # noqa: BLE001 — пам'ять розмови не має заважати надсиланню
                log.debug("Не вдалося запам'ятати надіслане", exc_info=True)

    def edit(self, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        """Змінити вже надіслане повідомлення бота (меню налаштувань після натиснутої кнопки)."""
        params: dict = {"chat_id": self.chat_id, "message_id": message_id, "text": text[:TEXT_LIMIT]}
        if buttons:
            params["reply_markup"] = _keyboard(buttons)
        try:
            self._call("editMessageText", **params)
        except TelegramError as e:
            if "not modified" not in str(e):  # натиснули двічі — показувати нічого нового
                raise

    def react(self, message_id: int, emoji: str = THINKING) -> None:
        """Реакція на твоє повідомлення (👀 — «прочитав, думаю»); "" — прибрати. Не вийшло — не біда."""
        try:
            self._call("setMessageReaction", chat_id=self.chat_id, message_id=message_id,
                       reaction=[{"type": "emoji", "emoji": emoji}] if emoji else [])
        except TelegramError as e:
            log.debug("Реакція не поставилась: %s", e)

    def download(self, file_id: str, limit: int = 20 * 2**20) -> bytes:
        """Файл, який ти надіслав боту (голосове, фото). Більше ``limit`` — не тягнемо."""
        info = self._call("getFile", file_id=file_id)
        if int(info.get("file_size") or 0) > limit:
            raise TelegramError("файл завеликий")
        try:
            response = self.session.get(FILE_API.format(token=self.token, path=info["file_path"]), timeout=120)
            response.raise_for_status()
        except (requests.RequestException, KeyError) as e:
            raise TelegramError(f"не вдалося завантажити файл: {e}") from e
        return response.content

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
            "getUpdates",
            http_timeout=wait + 10,
            offset=self._offset,
            timeout=wait,
            allowed_updates=["message", "callback_query"],
        )
        messages = []
        for update in result:
            self._offset = max(self._offset, update["update_id"] + 1)
            message = update.get("message") or {}
            voice = (message.get("voice") or message.get("audio") or message.get("video_note") or {}).get("file_id", "")
            photo = _photo_id(message)
            if "text" in message or voice or photo:
                original = message.get("reply_to_message") or {}
                # Реплай на власне повідомлення студента — не на повідомлення бота: тоді «на що відповіли» немає.
                from_bot = (original.get("from") or {}).get("is_bot", True)
                replied = (original.get("text") or original.get("caption") or "") if from_bot else ""
                text = message.get("text") or message.get("caption") or ""
                messages.append(Incoming(message["chat"]["id"], text, "reply_to_message" in message, replied=replied,
                                         message_id=message.get("message_id", 0), voice=voice, photo=photo))
            if callback := update.get("callback_query"):
                under = callback.get("message") or {}
                messages.append(Incoming(under.get("chat", {}).get("id", 0), callback.get("data", ""), False, callback["id"],
                                         menu_id=under.get("message_id", 0)))
        return messages

    def listen(self, handle: Callable[[str, bool], str | None], stop: threading.Event,
               media: Callable[[Incoming], str | None] | None = None) -> None:
        """Приймає команди лише з твого чату й відповідає на них (реплаєм на твоє повідомлення). Голосові й фото —
        у ``media``. Блокує — запускай у потоці."""
        while not stop.is_set():
            try:
                for incoming in self.updates():
                    if incoming.chat_id != self.chat_id:
                        log.warning("Ігнорую повідомлення боту з чужого чату %s", incoming.chat_id)
                        continue
                    if incoming.callback_id:  # прибрати «годинник» на кнопці
                        self._call("answerCallbackQuery", callback_query_id=incoming.callback_id)
                    # Відповідь іде в чат пари лише на сповіщення, що чекає відповіді («тебе кличуть»); відповідь
                    # на будь-що інше (скажімо, на відповідь мозку) — це просто продовження розмови з ботом.
                    is_reply = incoming.is_reply and self.reply_goes_to_chat(incoming.replied)
                    self.current = incoming  # хто питає — щоб відповідь, яка прийде пізніше, була реплаєм на нього
                    try:
                        if incoming.voice or incoming.photo:
                            reply = media(incoming) if media else "Голосові й фото я поки не вмію — напиши текстом."
                        else:
                            reply = handle(incoming.text, is_reply)
                    except Exception:  # noqa: BLE001 — одна зламана команда не має глушити бота
                        log.exception("Команда «%s» з Telegram не вдалася", incoming.text[:40])
                        reply = "⚠️ Не вийшло — подробиці в журналі програми."
                    if reply:
                        self.send(reply, reply_to=incoming.message_id)
            except TelegramError as e:
                log.warning("%s — повторю за хвилину", e)
                stop.wait(60)


def _keyboard(buttons: Buttons) -> dict:
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in buttons]}


def _photo_id(message: dict) -> str:
    """Фото з повідомлення: найбільший розмір до ~1280 пікселів (більше мовній моделі не треба). Картинка,
    надіслана файлом, — теж."""
    sizes = message.get("photo") or []
    if sizes:
        fitting = [size for size in sizes if max(size.get("width", 0), size.get("height", 0)) <= 1280] or sizes[:1]
        return fitting[-1].get("file_id", "")
    document = message.get("document") or {}
    if str(document.get("mime_type", "")).startswith("image/"):
        return document.get("file_id", "")
    return ""


def wait_for_code(token: str, code: str, wait_seconds: int = 180, session: requests.Session | None = None) -> int | None:
    """Підключення бота: чекає, поки хтось надішле йому ``code`` (чи натисне Start за посиланням з ним).

    Повертає chat_id того, хто надіслав код, — лише він і отримуватиме сповіщення. Випадковий
    перехожий, що знайшов бота, коду не знає, тож підключитися замість тебе не може.
    """
    bot = TelegramBot(token, session=session)
    pattern = re.compile(rf"^(/start(@\w+)?\s+)?{re.escape(code)}$", re.I)
    deadline = time.monotonic() + wait_seconds
    while (left := deadline - time.monotonic()) > 0:
        for incoming in bot.updates(wait=max(1, min(25, int(left)))):
            text = incoming.text.strip()
            if incoming.chat_id and pattern.match(text):
                bot.chat_id = incoming.chat_id
                bot.ack()  # щоб програма не отримала цей код ще раз як команду
                bot.send("👋 Готово! Тепер я писатиму сюди про твої пари. Напиши «допомога», щоб побачити команди.")
                return incoming.chat_id
            if incoming.chat_id and text.startswith("/start"):
                TelegramBot(token, incoming.chat_id, session=bot.session).send(
                    "Щоб підключити мене, натисни «Підключити» у вікні autopara або надішли сюди код звідти."
                )
    return None


# Windows: стандартне сповіщення через PowerShell (без додаткових програм). Текст передаємо через
# змінну середовища, а не в самому скрипті, тож жодні лапки в ньому нічого не зламають.
WINDOWS_TOAST = (
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
    "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; $xml.LoadXml($env:AUTOPARA_TOAST);"
    "$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
    "[Windows.UI.Notifications.ToastNotification]::new($xml))"
)


def desktop_notifier() -> Callable[[str, bool], None] | None:
    """Як показати сповіщення на робочому столі на цій ОС (або None, якщо ніяк)."""
    if sys.platform == "win32" and shutil.which("powershell"):

        def windows(text: str, urgent: bool) -> None:
            sound = '<audio src="ms-winsoundevent:Notification.Reminder"/>' if urgent else ""
            xml = (
                '<toast><visual><binding template="ToastGeneric"><text>autopara</text>'
                f"<text>{escape(text[:400])}</text></binding></visual>{sound}</toast>"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_TOAST],
                env={**os.environ, "AUTOPARA_TOAST": xml},
                capture_output=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

        return windows
    if sys.platform == "darwin" and shutil.which("osascript"):

        def mac(text: str, urgent: bool) -> None:
            script = 'on run argv\ndisplay notification (item 1 of argv) with title "autopara"\nend run'
            subprocess.run(["osascript", "-e", script, text[:400]], capture_output=True, check=False)

        return mac
    if shutil.which("notify-send"):

        def linux(text: str, urgent: bool) -> None:
            level = "critical" if urgent else "normal"
            subprocess.run(["notify-send", "-a", "autopara", "-u", level, "autopara", text], check=False)

        return linux
    return None


class Notifier:
    """Надсилає сповіщення у фоні, щоб повільна мережа не гальмувала решту.

    Текст іде і в Telegram, і на робочий стіл; фото, відео, голосові й файли — лише в Telegram.
    """

    def __init__(self, bot: TelegramBot | None = None, desktop: bool = True):
        self.bot = bot
        self.desktop = desktop_notifier() if desktop else None
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def __call__(self, text: str, urgent: bool = False) -> None:
        log.info("🔔 %s", text)

        def job() -> None:
            if self.desktop:
                self.desktop(text, urgent)
            if self.bot:
                self.bot.send(text)

        self._queue.put(job)

    def ask(self, text: str, buttons: Buttons, urgent: bool = True) -> None:
        """Сповіщення з кнопками: відповісти можна одним дотиком."""
        log.info("🔔 %s", text)

        def job() -> None:
            if self.desktop:
                self.desktop(text, urgent)
            if self.bot:
                self.bot.send(text, buttons)

        self._queue.put(job)

    def reply(self, text: str, reply_to: int = 0) -> None:
        """Відповідь у розмові з ботом — реплаєм на твоє повідомлення (лише в Telegram, без сповіщення на столі)."""
        log.info("💬 %s", text.splitlines()[0][:200] if text else "")
        if self.bot:
            self._queue.put(lambda: self.bot.send(text, reply_to=reply_to))

    def edit(self, message_id: int, text: str, buttons: Buttons | None = None) -> None:
        """Змінити вже надіслане повідомлення бота (меню налаштувань після натиснутої кнопки)."""
        params: dict = {"chat_id": self.chat_id, "message_id": message_id, "text": text[:TEXT_LIMIT]}
        if buttons:
            params["reply_markup"] = _keyboard(buttons)
        try:
            self._call("editMessageText", **params)
        except TelegramError as e:
            if "not modified" not in str(e):  # натиснули двічі — показувати нічого нового
                raise

    def react(self, message_id: int, emoji: str = THINKING) -> None:
        if self.bot and message_id:
            self._queue.put(lambda: self.bot.react(message_id, emoji))

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
