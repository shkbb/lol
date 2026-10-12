"""Налаштування з Telegram: «налаштування» — повідомлення з перемикачами; натиснув — змінилось і збереглось, як у вікні
програми (те саме config.toml). Тут лише те, що зручно міняти з телефона; решта — у вікні програми.
"""

from __future__ import annotations

import copy
from typing import Callable

from .config import Config

PREFIX = "cfg:"  # кнопки налаштувань: «cfg:telegram.reports» — перемкнути, «cfg:me.chat_reply=ask» — вибрати
WORDS = ("налаштування", "/settings", "settings", "⚙", "⚙️")

# (шлях у налаштуваннях, що це)
NOTICES = [
    ("telegram.reports", "Звіт після пари"),
    ("telegram.weekly", "Підсумок тижня (неділя ввечері)"),
    ("telegram.mentions", "Сповіщати, коли тебе кличуть"),
    ("telegram.joins", "Писати, коли зайшов і вийшов з пари"),
    ("telegram.clips", "Відео моменту, коли кличуть"),
]
CHAT_REPLY = [("off", "ніколи"), ("ask", "питати мене"), ("auto", "сам")]
IN_CHAT = [  # що писати в чат пари — лише коли туди взагалі можна писати (див. CHAT_REPLY)
    ("me.roll_call", "Відмічатися на перекличці"),
    ("me.answer_here", "Відповідати «я тут»"),
    ("me.polite", "Дублювати «дякую», «добрий день»"),
]
LESSON = [("join.auto_leave", "Виходити з пари, коли вона скінчилась")]
CHOICES = {"me.chat_reply": [value for value, _ in CHAT_REPLY]}
TOGGLES = {path for path, _ in NOTICES + IN_CHAT + LESSON}

Buttons = list[list[tuple[str, str]]]


def wanted(text: str) -> bool:
    """Це про налаштування: команда «налаштування» чи натиснута кнопка з меню."""
    low = text.strip().casefold()
    return low in WORDS or low.startswith(PREFIX)


def _get(config: Config, path: str):
    section, name = path.split(".")
    return getattr(getattr(config, section), name)


def menu(config: Config) -> tuple[str, Buttons]:
    """Текст і кнопки меню — з поточними значеннями (✅ / ⬜, • біля вибраного)."""
    def toggle(path: str, label: str) -> list[tuple[str, str]]:
        return [(("✅ " if _get(config, path) else "⬜ ") + label, PREFIX + path)]

    buttons: Buttons = [toggle(path, label) for path, label in NOTICES]
    chat = config.me.chat_reply
    buttons.append([(("• " if chat == value else "") + label, f"{PREFIX}me.chat_reply={value}") for value, label in CHAT_REPLY])
    if chat != "off":
        buttons += [toggle(path, label) for path, label in IN_CHAT]
    buttons += [toggle(path, label) for path, label in LESSON]
    said = dict(CHAT_REPLY)[chat] if chat in dict(CHAT_REPLY) else chat
    text = (
        "⚙️ Налаштування — натисни, щоб змінити (зберігається одразу).\n\n"
        f"✍️ Писати в чат пари самому (перекличка, «я тут»): {said}.\n"
        + ("Перекличку, «я тут» і привітання — за кнопками нижче.\n" if chat != "off" else
           "Поки «ніколи» — у чат пари пишу лише те, що напишеш ти («напиши …», «+»).\n")
        + "\nЩо змінилось під час пари, може застосуватись після неї. Решта налаштувань — у вікні програми."
    )
    return text, buttons


def change(config: Config, data: str) -> Config | None:
    """Нові налаштування після натиснутої кнопки (копія), або None — кнопка незнайома чи застаріла."""
    body = data.strip()[len(PREFIX):] if data.strip().startswith(PREFIX) else ""
    path, _, value = body.partition("=")
    if "." not in path:
        return None
    new = copy.deepcopy(config)
    section, name = path.split(".", 1)
    if path in TOGGLES and not value:
        setattr(getattr(new, section), name, not _get(config, path))
        return new
    if value and value in CHOICES.get(path, []):
        setattr(getattr(new, section), name, value)
        return new
    return None


def respond(text: str, bot, current: Config | None, save: Callable[[Config], None] | None) -> str:
    """«налаштування» — надіслати меню; кнопка з меню — змінити, зберегти й оновити меню на місці. Повертає, що ще
    відповісти текстом ("" — нічого: меню вже надіслане)."""
    if current is None or save is None:
        return "⚙️ Налаштування з Telegram працюють, коли програма запущена з вікном (autopara app)."
    pressed = text.strip().startswith(PREFIX)
    if pressed and (new := change(current, text)) is not None:
        # Деякі зміни перезапускають бота — тоді новий отримав би це натискання ще раз (Telegram вважає його
        # непрочитаним, поки не підтвердили) і перемкнув би назад. Тож підтверджуємо до збереження.
        try:
            bot.ack()
        except Exception:  # noqa: BLE001 — не вийшло підтвердити: зберегти однаково треба
            pass
        save(new)
        current = new
    body, buttons = menu(current)
    menu_id = bot.current.menu_id if bot.current is not None else 0
    if pressed and menu_id:
        bot.edit(menu_id, body, buttons)
    else:
        bot.send(body, buttons)
    return ""
