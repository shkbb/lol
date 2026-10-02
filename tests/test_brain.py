import json

import pytest

from autopara.assistant import Decision
from autopara.brain import BrainError, BrainWorker, OllamaBrain, parse_decision


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
