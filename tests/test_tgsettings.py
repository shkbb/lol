"""Налаштування з Telegram: «налаштування» — меню з перемикачами; кнопка — змінити, зберегти й оновити меню на місці."""

from types import SimpleNamespace

from autopara import tgsettings
from autopara.config import Config


class FakeBot:
    def __init__(self, menu_id=0):
        self.current = SimpleNamespace(menu_id=menu_id)
        self.sent, self.edited, self.acked = [], [], 0

    def send(self, text, buttons=None, reply_to=0):
        self.sent.append((text, buttons))

    def edit(self, message_id, text, buttons=None):
        self.edited.append((message_id, text, buttons))

    def ack(self):
        self.acked += 1


def labels(buttons):
    return [label for row in buttons for label, _ in row]


def test_menu_shows_current_values_and_hides_chat_details_when_chat_is_off():
    config = Config()
    config.me.chat_reply = "off"
    config.telegram.weekly = False
    text, buttons = tgsettings.menu(config)
    shown = labels(buttons)
    assert "✅ Звіт після пари" in shown and "⬜ Підсумок тижня (неділя ввечері)" in shown
    assert "• ніколи" in shown and "питати мене" in shown and "сам" in shown
    assert not any("перекличці" in label for label in shown)  # у чат не пишемо — що писати там, не питаємо
    assert "ніколи" in text
    config.me.chat_reply = "ask"
    assert "✅ Відмічатися на перекличці" in labels(tgsettings.menu(config)[1])
    assert all(len(data.encode()) <= 64 for row in buttons for _, data in row)  # межа Telegram для кнопок


def test_buttons_change_a_copy_and_unknown_ones_change_nothing():
    config = Config()
    reports = tgsettings.change(config, "cfg:telegram.reports")
    assert reports.telegram.reports is False and config.telegram.reports is True  # оригінал не чіпаємо
    assert tgsettings.change(config, "cfg:me.chat_reply=ask").me.chat_reply == "ask"
    for stale in ("cfg:me.chat_reply=always", "cfg:telegram.token", "cfg:schedule.group", "cfg:", "cfg:nothing"):
        assert tgsettings.change(config, stale) is None  # лише те, що є в меню
    assert tgsettings.wanted("Налаштування") and tgsettings.wanted("/settings") and tgsettings.wanted("cfg:join.auto_leave")
    assert not tgsettings.wanted("які налаштування в тебе?")  # це вже питання, а не команда


def test_command_sends_the_menu_and_a_button_saves_and_updates_it_in_place():
    saved = []
    bot = FakeBot()
    assert tgsettings.respond("налаштування", bot, Config(), saved.append) == ""
    assert bot.sent and not saved
    bot = FakeBot(menu_id=77)
    assert tgsettings.respond("cfg:telegram.weekly", bot, Config(), saved.append) == ""
    assert saved[-1].telegram.weekly is False
    assert bot.acked == 1  # натискання підтверджене ще до збереження: перезапущений бот не отримає його вдруге
    message_id, text, buttons = bot.edited[-1]
    assert message_id == 77 and "⬜ Підсумок тижня (неділя ввечері)" in labels(buttons) and not bot.sent
    assert "з вікном" in tgsettings.respond("налаштування", FakeBot(), None, None)  # без програми (autopara run) — підказка
