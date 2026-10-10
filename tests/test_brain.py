import json

import pytest

from autopara.assistant import Decision
from autopara.brain import BrainError, BrainWorker, ChatBrain, OllamaBrain, parse_decision


class FakeOllama:
    def __init__(self, content):
        self.content = content
        self.payload = None

    def post(self, url, json=None, timeout=None):
        self.payload = json
        content = self.content

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"message": {"content": content}}

        return Response()


def test_decide_sends_schema_and_parses():
    ollama = FakeOllama(json.dumps({"action": "chat_plus", "message": "", "reason": "просять +"}))
    decision = OllamaBrain("http://127.0.0.1:11434", "gemma3:4b", session=ollama).decide("ситуація")
    assert decision == Decision("chat_plus", "", "просять +")
    assert ollama.payload["model"] == "gemma3:4b"
    assert ollama.payload["format"]["properties"]["action"]["enum"][0] == "none"
    assert ollama.payload["messages"][1] == {"role": "user", "content": "ситуація"}


def test_integrated_graphics_brain_runs_on_processor_and_yields_during_lessons():
    ollama = FakeOllama("відповідь")
    lesson = [False]
    brain = OllamaBrain("http://x", "gemma3:4b", session=ollama, gpu=False, busy=lambda: lesson[0])
    brain.summarize("інструкція", "текст")
    options = ollama.payload["options"]
    # Вбудована карта (Vulkan) вішала GPU — лише процесор; контекст один на всі запити (інакше перезавантаження).
    assert options["num_gpu"] == 0 and options["num_ctx"] == 6144 and "num_thread" not in options
    lesson[0] = True
    brain.choose("що обрати?", ["а", "б"])
    assert ollama.payload["options"]["num_thread"] >= 2 and ollama.payload["options"]["num_ctx"] == 6144
    OllamaBrain("http://x", "gemma3:4b", session=ollama, gpu=True).summarize("і", "т")
    assert "num_gpu" not in ollama.payload["options"]  # окрема карта — як вирішить сама Ollama


def test_invented_actions_become_none():
    assert parse_decision('{"action": "unmute", "message": "", "reason": ""}').action == "none"


def test_garbage_is_an_error():
    with pytest.raises(BrainError):
        parse_decision("я думаю, що треба написати плюс")


def test_worker_feeds_snapshot_and_applies_decision():
    class Assistant:
        applied = []

        def snapshot(self, key, now):
            return f"ситуація {key}"

        def apply_decision(self, key, decision, now):
            self.applied.append((key, decision))

    class Brain:
        def decide(self, situation):
            assert situation == "ситуація pair"
            return Decision("notify", "Тебе кличуть")

    worker = BrainWorker(Brain(), clock=lambda: None)
    worker.assistant = Assistant()
    worker.think("pair")
    assert Assistant.applied == [("pair", Decision("notify", "Тебе кличуть"))]


class FakeBrain:
    """Що бачить BrainConnector у кожну спробу: (результат check(), чи відповідає Ollama)."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.now = self.rounds[0]
        self.pulled = 0

    def check(self):
        self.now = self.rounds.pop(0) if self.rounds else self.now
        return self.now[0]

    def reachable(self):
        return self.now[1]

    def pull(self):
        self.pulled += 1
        return True


def test_brain_connects_when_ollama_appears_later():
    from autopara.brain import BrainConnector

    ready = []
    brain = FakeBrain([("Ollama не встановлена", False), ("Ollama не встановлена", False), (None, True)])
    BrainConnector(brain, lambda: ready.append(True), retry_seconds=0.01)._run()
    assert ready == [True] and brain.pulled == 0


def test_brain_downloads_missing_model_itself():
    from autopara.brain import BrainConnector

    ready = []
    brain = FakeBrain([("модель не завантажена", True)])
    BrainConnector(brain, lambda: ready.append(True))._run()
    assert brain.pulled == 1 and ready == [True]


def test_choose_only_picks_from_the_list():
    options = ["Увійти до зустрічі", "Інші способи"]
    ollama = FakeOllama(json.dumps({"number": 1}))
    brain = OllamaBrain("http://127.0.0.1:11434", "gemma3:4b", session=ollama)
    assert brain.choose("Яка кнопка входить?", options) == 0
    assert "1. Увійти до зустрічі\n2. Інші способи" in ollama.payload["messages"][1]["content"]
    for answer in ({"number": 0}, {"number": 3}, {"oops": 1}):  # «жодна», поза списком, не той формат
        assert OllamaBrain("http://x", "m", session=FakeOllama(json.dumps(answer))).choose("?", options) is None
    assert OllamaBrain("http://x", "m", session=FakeOllama("не json")).choose("?", options) is None


def test_low_memory_unloads_the_brain_model():
    """Наживо пам'ять Ollama з вбудованою відеокартою росла за день до 6+ ГБ — і ноутбук двічі вичерпав пам'ять."""
    from autopara.brain import BrainMemoryGuard

    class Session:
        def __init__(self):
            self.unloads = []
            self.loaded = True

        def get(self, url, timeout):
            loaded = self.loaded

            class Response:
                def json(self):
                    return {"models": [{"name": "gemma3:4b"}] if loaded else []}

            return Response()

        def post(self, url, json, timeout):
            self.unloads.append(json)
            self.loaded = False

    session = Session()
    brain = OllamaBrain("http://127.0.0.1:11434", "gemma3:4b", session=session)
    free = {"gb": 6.0}
    guard = BrainMemoryGuard(brain, lambda: free["gb"], period=0.05)
    guard.start()
    try:
        import time

        time.sleep(0.2)
        assert session.unloads == []  # пам'яті вдосталь — модель не чіпаємо
        free["gb"] = 1.2
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not session.unloads:
            time.sleep(0.05)
    finally:
        guard.stop()
    assert session.unloads == [{"model": "gemma3:4b", "keep_alive": 0}]
    assert brain.unload() is False  # уже вивантажено — нічого не робимо


def test_model_is_reloaded_every_few_requests_so_ollama_leak_does_not_pile_up():
    """Наживо кожен запит до Ollama з відеокартою Intel додавав ~270 МБ, які вона не віддавала."""
    from autopara.brain import REQUESTS_PER_LOAD

    class Session:
        def __init__(self):
            self.chats, self.unloads = 0, 0

        def post(self, url, json, timeout):
            if url.endswith("/api/generate"):
                self.unloads += 1
                return None
            self.chats += 1

            class Response:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"message": {"content": "конспект"}}

            return Response()

        def get(self, url, timeout):
            class Response:
                def json(self):
                    return {"models": [{"name": "gemma3:4b"}]}

            return Response()

    session = Session()
    brain = OllamaBrain("http://127.0.0.1:11434", "gemma3:4b", session=session)
    for _ in range(REQUESTS_PER_LOAD * 2 + 1):
        assert brain.summarize("конспектуй", "текст") == "конспект"
    assert session.chats == REQUESTS_PER_LOAD * 2 + 1 and session.unloads == 2


def test_chat_questions_go_to_the_big_model_only_when_free():
    """Розумний режим: поза парами й коли вистачає пам'яті — велика модель (відповідь у Telegram, підсумок пари).
    Перед нею вивантажуємо швидку; між шматками звіту велика лишається в пам'яті, а щойно знадобиться швидка —
    спершу геть велику: дві моделі разом у пам'ять не влізуть."""
    log = []

    class Model:
        def __init__(self, name):
            self.name, self.before = name, None

        def summarize(self, instruction, text, **options):
            if self.before:
                self.before()
            log.append(f"{self.name}: відповідь")
            return self.name

        def unload(self):
            log.append(f"{self.name}: вивантажено")

    quiet, free, now = [True], [6.0], [0.0]
    small, big = Model("швидка"), Model("розумна")
    chat = ChatBrain(small, big, quiet=lambda: quiet[0], free_gb=lambda: free[0], need_gb=9,
                     wait=lambda s: free.__setitem__(0, free[0] + 1), clock=lambda: now[0])
    assert chat.summarize("і", "т") == "швидка"  # розумну ще не завантажено
    chat.big_ready = True
    log.clear()
    assert chat.will_think_long() and chat.summarize("і", "т") == "розумна"
    now[0] = 20
    assert chat.summarize("і", "т") == "розумна"  # наступний шматок звіту — велика ще в пам'яті
    # Спершу геть швидку, дочекались пам'яті (6 → 9 ГБ), далі розумна двічі поспіль без перезавантажень.
    assert log == ["швидка: вивантажено", "розумна: відповідь", "розумна: відповідь"]
    log.clear()
    quiet[0] = False  # пара — лише швидка, а розумну перед тим геть з пам'яті
    assert chat.summarize("і", "т") == "швидка" and log == ["розумна: вивантажено", "швидка: відповідь"]
    quiet[0], free[0] = True, 2.0  # пам'яті мало навіть без швидкої
    assert not chat.will_think_long() and chat.summarize("і", "т") == "швидка"
    assert chat.summarize("і", "т", images=[b"jpg"]) == "швидка"  # фото — швидка (вона бачить картинки)
    free[0] = None  # macOS: скільки вільно — невідомо, тож розумну не вантажимо
    assert not chat.will_think_long() and chat.summarize("і", "т") == "швидка"


def test_quick_model_waits_while_the_smart_one_answers():
    """Спільний замок: поки розумна модель відповідає, запит швидкої (скажімо, звіт після пари) чекає."""
    import threading

    lock = threading.RLock()
    slow = FakeOllama("ок")
    brain = OllamaBrain("http://x", "gemma3:4b", session=slow, gpu=False, lock=lock)
    big = OllamaBrain("http://x", "qwen3.5:9b", session=FakeOllama("розумно"), gpu=False, lock=lock)
    big.think = False
    done = []
    with lock:  # «розумна думає»
        worker = threading.Thread(target=lambda: done.append(brain.summarize("і", "т")))
        worker.start()
        worker.join(0.2)
        assert done == []  # швидка чекає
        assert big.summarize("і", "т") == "розумно" and big.session.payload["think"] is False
    worker.join(2)
    assert done == ["ок"] and "think" not in slow.payload

def test_gpu_switched_off_in_settings_keeps_the_brain_on_processor(monkeypatch):
    """«Відеокарта: не використовувати взагалі» — і мовна модель лише на процесорі, навіть з окремою картою."""
    from autopara import accel

    monkeypatch.setattr(accel, "brain_on_gpu", lambda: True)  # окрема карта є
    ollama = FakeOllama("ок")
    OllamaBrain("http://x", "gemma3:4b", session=ollama, gpu=False).summarize("і", "т")
    assert ollama.payload["options"]["num_gpu"] == 0
    OllamaBrain("http://x", "gemma3:4b", session=ollama).summarize("і", "т")
    assert "num_gpu" not in ollama.payload["options"]  # «auto» — як вирішить Ollama


def test_embedder_batches_texts_and_stays_on_processor():
    from autopara.brain import Embedder

    sent = []

    class Session:
        def post(self, url, json=None, timeout=None):
            sent.append(json)

            class Response:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"embeddings": [[0.1, 0.2] for _ in json["input"]]}

            return Response()

    vectors = Embedder("http://x", session=Session(), batch=2)(["а", "б", "в"])
    assert len(vectors) == 3 and [len(p["input"]) for p in sent] == [2, 1]
    assert sent[0]["model"] == "embeddinggemma" and sent[0]["options"] == {"num_gpu": 0}
