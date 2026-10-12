import threading

from autopara.telegram import CAPTION_LIMIT, Notifier, TelegramBot


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeApi:
    def __init__(self, updates=()):
        self.updates = list(updates)
        self.calls = []

    def post(self, url, json=None, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, json if json is not None else data, files))
        if method == "getUpdates":
            result, self.updates = self.updates, []
            return FakeResponse({"ok": True, "result": result})
        return FakeResponse({"ok": True, "result": {}})


def update(update_id, chat_id, text, reply=False):
    message = {"chat": {"id": chat_id}, "text": text}
    if reply:
        message["reply_to_message"] = {"message_id": 1}
    return {"update_id": update_id, "message": message}


def test_commands_only_from_my_chat():
    api = FakeApi([update(1, 42, "статус"), update(2, 666, "вийди"), update(3, 42, "Вибачте, зв'язок", reply=True)])
    bot = TelegramBot("123:abc", chat_id=42, session=api)
    stop = threading.Event()
    heard = []

    def handle(text, is_reply):
        heard.append((text, is_reply))
        if len(heard) == 2:
            stop.set()
        return f"ok: {text}"

    bot.listen(handle, stop)
    assert heard == [("статус", False), ("Вибачте, зв'язок", True)]  # чужий чат проігноровано
    sent = [params["text"] for method, params, _ in api.calls if method == "sendMessage"]
    assert sent == ["ok: статус", "ok: Вибачте, зв'язок"]
    assert bot._offset == 4  # наступного разу ці оновлення не прийдуть знову


def test_a_broken_command_does_not_silence_the_bot():
    """Наживо «статус» упав — і бот замовк до перезапуску програми. Тепер — відповідь про помилку, і далі слухає."""
    api = FakeApi([update(1, 42, "статус"), update(2, 42, "напиши тест")])
    bot = TelegramBot("123:abc", chat_id=42, session=api)
    stop = threading.Event()

    def handle(text, is_reply):
        if text == "статус":
            raise ValueError("зламано")
        stop.set()
        return "Пишу «тест»."

    bot.listen(handle, stop)
    sent = [params["text"] for method, params, _ in api.calls if method == "sendMessage"]
    assert sent == ["⚠️ Не вийшло — подробиці в журналі програми.", "Пишу «тест»."]


def test_photo_is_uploaded_and_long_caption_follows_as_text():
    api = FakeApi()
    notifier = Notifier(TelegramBot("123:abc", chat_id=42, session=api), desktop=False)
    caption = "Тебе кличуть: " + "слово " * 300
    notifier.photo(b"\xff\xd8jpeg", caption)
    notifier.flush()
    (method, data, files), (text_method, text_params, _) = api.calls
    assert method == "sendPhoto" and files == {"photo": ("screen.jpg", b"\xff\xd8jpeg")}
    assert data["chat_id"] == "42" and len(data["caption"]) == CAPTION_LIMIT
    assert text_method == "sendMessage" and text_params["text"].startswith("Тебе кличуть")



def test_buttons_are_sent_and_presses_handled():
    press = {"update_id": 7, "callback_query": {"id": "cb1", "data": "так", "message": {"chat": {"id": 42}}}}
    api = FakeApi([press])
    bot = TelegramBot("123:abc", chat_id=42, session=api)
    bot.send("Написати «+»?", [[("✅ Так", "так"), ("❌ Ні", "ні")]])
    stop = threading.Event()
    pressed = []

    def handle(text, is_reply):
        pressed.append(text)
        stop.set()
        return "Пишу «+»."

    bot.listen(handle, stop)
    assert pressed == ["так"]
    methods = [method for method, _, _ in api.calls]
    assert methods == ["sendMessage", "getUpdates", "answerCallbackQuery", "sendMessage"]
    markup = api.calls[0][1]["reply_markup"]["inline_keyboard"]
    assert markup == [[{"text": "✅ Так", "callback_data": "так"}, {"text": "❌ Ні", "callback_data": "ні"}]]


def test_pairing_accepts_only_the_code():
    from autopara.telegram import wait_for_code

    api = FakeApi([update(1, 666, "/start"), update(2, 777, "привіт"), update(3, 42, "/start 482917")])
    assert wait_for_code("123:abc", "482917", wait_seconds=5, session=api) == 42
    sent = [(params["chat_id"], params["text"]) for method, params, _ in api.calls if method == "sendMessage"]
    assert sent[0][0] == 666 and "код" in sent[0][1]  # підказали, як підключитись, але не підключили
    assert sent[-1][0] == 42 and sent[-1][1].startswith("👋 Готово")
    acks = [params for method, params, _ in api.calls if method == "getUpdates" and params.get("limit") == 1]
    assert acks == [{"offset": 4, "timeout": 0, "limit": 1}]  # «/start 482917» не прийде програмі ще раз


def test_pairing_by_typed_code_and_timeout():
    from autopara.telegram import wait_for_code

    assert wait_for_code("123:abc", "000111", wait_seconds=5, session=FakeApi([update(1, 9, " 000111 ")])) == 9
    assert wait_for_code("123:abc", "000111", wait_seconds=1, session=FakeApi([update(1, 9, "000112")])) is None


def test_desktop_notifications_per_platform(monkeypatch):
    from autopara import telegram

    runs = []
    monkeypatch.setattr(telegram.subprocess, "run", lambda args, **kw: runs.append((args, kw)))
    monkeypatch.setattr(telegram.shutil, "which", lambda name: f"/bin/{name}")
    tricky = 'Ляшко, ви тут? <b>"$(rm -rf ~)"</b> & \'лапки\''

    monkeypatch.setattr(telegram.sys, "platform", "win32")
    telegram.desktop_notifier()(tricky, True)
    args, kw = runs[-1]
    assert args[0] == "powershell" and tricky not in " ".join(args)  # текст — не в команді
    assert "&lt;b&gt;" in kw["env"]["AUTOPARA_TOAST"] and "&amp;" in kw["env"]["AUTOPARA_TOAST"]

    monkeypatch.setattr(telegram.sys, "platform", "darwin")
    telegram.desktop_notifier()(tricky, False)
    args, _ = runs[-1]
    assert args[0] == "osascript" and args[-1] == tricky  # окремим аргументом, не всередині скрипта

    monkeypatch.setattr(telegram.sys, "platform", "linux")
    telegram.desktop_notifier()(tricky, True)
    assert runs[-1][0] == ["notify-send", "-a", "autopara", "-u", "critical", "autopara", tricky]

    monkeypatch.setattr(telegram.shutil, "which", lambda name: None)
    assert telegram.desktop_notifier() is None


def test_reply_goes_to_the_meeting_only_when_it_answers_an_alert():
    """Відповідь на сповіщення «тебе кличуть» — у чат пари; на відповідь мозку — це вже розмова з ботом."""
    alert = update(1, 42, "Так, я тут", reply=True)
    alert["message"]["reply_to_message"]["text"] = "🚨 Тебе кличуть на парі"
    talk = update(2, 42, "а що ще?", reply=True)
    talk["message"]["reply_to_message"]["text"] = "🧠 Сьогодні говорили про індекси."
    api = FakeApi([alert, talk])
    bot = TelegramBot("123:abc", chat_id=42, session=api)
    bot.reply_goes_to_chat = lambda replied: replied.startswith("🚨")
    stop = threading.Event()
    heard = []

    def handle(text, is_reply):
        heard.append((text, is_reply))
        if len(heard) == 2:
            stop.set()

    bot.listen(handle, stop)
    assert heard == [("Так, я тут", True), ("а що ще?", False)]


def test_answers_are_replies_long_ones_split_and_media_goes_to_its_handler():
    """Відповідь — реплаєм на твоє повідомлення (не сплутаєш зі звітом), довга — кількома повідомленнями;
    голосові й фото — окремому обробнику."""
    voice = {"update_id": 5, "message": {"message_id": 70, "chat": {"id": 42}, "voice": {"file_id": "v1", "duration": 4}}}
    photo = {"update_id": 6, "message": {"message_id": 71, "chat": {"id": 42}, "caption": "що це?", "photo": [
        {"file_id": "small", "width": 320, "height": 240}, {"file_id": "mid", "width": 1280, "height": 960},
        {"file_id": "huge", "width": 4000, "height": 3000}]}}
    text = update(7, 42, "рецепт борщу")
    text["message"]["message_id"] = 72
    api = FakeApi([voice, photo, text])
    bot = TelegramBot("123:abc", chat_id=42, session=api)
    stop = threading.Event()
    got = []

    def media(incoming):
        got.append((incoming.voice, incoming.photo, incoming.text, incoming.message_id))
        return ""  # відповість пізніше реплаєм (бот уже поставив 👀)

    def handle(text, is_reply):
        stop.set()
        return "Інгредієнти:\n" + "• буряк\n" * 900  # ~7 тис. знаків — два повідомлення

    bot.listen(handle, stop, media)
    assert got == [("v1", "", "", 70), ("", "mid", "що це?", 71)]  # фото — найбільше до 1280 пікселів
    sent = [params for method, params, _ in api.calls if method == "sendMessage"]
    assert len(sent) == 2 and all(len(params["text"]) <= 4000 for params in sent)
    assert sent[0]["reply_parameters"]["message_id"] == 72 and "reply_parameters" not in sent[1]
    bot.react(72)
    assert api.calls[-1][0] == "setMessageReaction" and api.calls[-1][1]["reaction"] == [{"type": "emoji", "emoji": "👀"}]


def test_everything_the_bot_writes_is_remembered_and_replies_carry_what_they_answer():
    """Звіт, підсумок тижня, сповіщення — усе, що бот написав, іде в пам'ять розмови (довге — цілим); реплай несе
    текст повідомлення бота, на яке відповіли, а реплай на власне повідомлення — ні."""
    digest = update(1, 42, "цікавий звіт, дякую", reply=True)
    digest["message"]["reply_to_message"].update(text="🗓 Підсумок тижня 05.10–11.10", **{"from": {"is_bot": True}})
    own = update(2, 42, "і ще одне", reply=True)
    own["message"]["reply_to_message"].update(text="моє попереднє питання", **{"from": {"is_bot": False}})
    bot = TelegramBot("123:abc", chat_id=42, session=FakeApi([digest, own]))
    assert [(m.text, m.replied) for m in bot.updates()] == [
        ("цікавий звіт, дякую", "🗓 Підсумок тижня 05.10–11.10"), ("і ще одне", "")]
    sent = []
    bot.on_sent = sent.append
    report = "🗓 Підсумок тижня\n" + "рядок про пару\n" * 600  # довше за одне повідомлення Telegram
    bot.send(report)
    bot.send("🧠 Радий, що знадобилось!", reply_to=7)
    assert sent == [report, "🧠 Радий, що знадобилось!"]



def test_settings_menu_is_edited_in_place_and_buttons_know_their_message():
    press = {"update_id": 9, "callback_query": {"id": "cb1", "data": "cfg:telegram.weekly",
                                                 "message": {"message_id": 77, "chat": {"id": 42}}}}
    api = FakeApi([press])
    bot = TelegramBot("123:abc", chat_id=42, session=api)
    [incoming] = bot.updates()
    assert (incoming.text, incoming.menu_id, incoming.callback_id) == ("cfg:telegram.weekly", 77, "cb1")
    bot.edit(77, "⚙️ Налаштування", [[("⬜ Підсумок тижня", "cfg:telegram.weekly")]])
    method, params, _ = api.calls[-1]
    assert method == "editMessageText" and params["message_id"] == 77
    assert params["reply_markup"] == {"inline_keyboard": [[{"text": "⬜ Підсумок тижня", "callback_data": "cfg:telegram.weekly"}]]}

    class Unchanged(FakeApi):
        def post(self, url, json=None, data=None, files=None, timeout=None):
            return FakeResponse({"ok": False, "description": "Bad Request: message is not modified"})

    TelegramBot("123:abc", chat_id=42, session=Unchanged()).edit(77, "те саме")  # двічі натиснули — не помилка
