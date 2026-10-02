import threading

from autopara.telegram import TelegramBot


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data


class FakeApi:
    def __init__(self, updates):
        self.updates = updates
        self.calls = []

    def post(self, url, json=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        self.calls.append((method, json))
        if method == "getUpdates":
            result, self.updates = self.updates, []
            return FakeResponse({"ok": True, "result": result})
        return FakeResponse({"ok": True, "result": {}})


def update(update_id, chat_id, text):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def test_commands_only_from_my_chat():
    api = FakeApi([update(1, 42, "статус"), update(2, 666, "вийди"), update(3, 42, "+")])
    bot = TelegramBot("123:abc", chat_id=42, session=api)
    stop = threading.Event()
    heard = []

    def handle(text):
        heard.append(text)
        if len(heard) == 2:
            stop.set()
        return f"ok: {text}"

    bot.listen(handle, stop)
    assert heard == ["статус", "+"]  # чужий чат проігноровано
    sent = [params["text"] for method, params in api.calls if method == "sendMessage"]
    assert sent == ["ok: статус", "ok: +"]
    offsets = [params["offset"] for method, params in api.calls if method == "getUpdates"]
    assert offsets == [0]
    assert bot._offset == 4  # наступного разу ці оновлення не прийдуть знову
