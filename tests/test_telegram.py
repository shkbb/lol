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
