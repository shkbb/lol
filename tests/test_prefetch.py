"""Інсталятор завантажує моделі заздалегідь — ті самі, що вибере програма на цьому комп'ютері, і не падає без мережі."""

import json
import sys
import types

import requests

from autopara import accel, ears, prefetch
from autopara.accel import CPU, Device
from autopara.config import Config


def computer(monkeypatch, cpus, ram, device=CPU):
    monkeypatch.setattr(accel, "whisper_device", lambda setting="auto", free=False: device)
    monkeypatch.setattr(ears.os, "cpu_count", lambda: cpus)
    monkeypatch.setattr(ears, "total_ram_gb", lambda: ram)


def test_whisper_models_match_what_the_program_will_load(monkeypatch):
    config = Config()
    computer(monkeypatch, 8, 16)
    assert [name for name, _ in prefetch.whisper_models(config)] == ["large-v3-turbo", "large-v3"]
    computer(monkeypatch, 4, 8)  # слабший: уточнення тією ж моделлю, що наживо
    assert [name for name, _ in prefetch.whisper_models(config)] == ["large-v3-turbo"]
    computer(monkeypatch, 8, 16, Device("cuda", "int8_float16", "RTX 4060 (CUDA)", 8.0))
    assert [name for name, _ in prefetch.whisper_models(config)] == ["large-v3"]
    config.ears.refine_model = "off"
    computer(monkeypatch, 8, 16)
    assert [name for name, _ in prefetch.whisper_models(config)] == ["large-v3-turbo"]


def test_ollama_models_skip_empty_and_repeated():
    config = Config()
    assert [name for name, _ in prefetch.ollama_models(config)] == ["gemma3:4b", "embeddinggemma"]
    config.brain.chat_model = "qwen3.5:9b"
    config.brain.embed_model = ""
    assert [name for name, _ in prefetch.ollama_models(config)] == ["gemma3:4b", "qwen3.5:9b"]
    config.brain.chat_model = "gemma3:4b"
    assert [name for name, _ in prefetch.ollama_models(config)] == ["gemma3:4b"]


class Ollama:
    def __init__(self, have=(), lines=None, up=True):
        self.have, self.lines, self.up, self.pulled = list(have), lines, up, []

    def get(self, url, timeout=None):
        if not self.up:
            raise requests.ConnectionError("відмова")
        have = self.have
        return types.SimpleNamespace(ok=True, json=lambda: {"models": [{"name": name} for name in have]})

    def post(self, url, json=None, stream=False, timeout=None):
        self.pulled.append(json["model"])
        lines = self.lines

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def raise_for_status(self):
                pass

            def iter_lines(self):
                return iter(lines)

        return Response()


def test_ollama_pull_shows_progress_and_skips_what_is_there():
    said, shown = [], []
    lines = [json.dumps(line).encode() for line in (
        {"status": "pulling manifest"},
        {"status": "pulling", "total": 3_300_000_000, "completed": 1_650_000_000},
        {"status": "pulling", "total": 3_300_000_000, "completed": 3_300_000_000},
        {"status": "pulling", "total": 500, "completed": 500},  # дрібний файл — без поступу
        {"status": "success"})]
    session = Ollama(have=["gemma3:4b"], lines=lines)
    assert prefetch.fetch_ollama("http://x", "gemma3:4b", "мозок", said.append, session, shown.append)
    assert session.pulled == [] and "вже є" in said[-1]
    assert prefetch.fetch_ollama("http://x", "embeddinggemma", "пошук", said.append, session, shown.append)
    assert session.pulled == ["embeddinggemma"] and shown == ["  50% з 3.3 ГБ", "  100% з 3.3 ГБ"]
    broken = Ollama(lines=[json.dumps({"error": "no space left on device"}).encode()])
    assert not prefetch.fetch_ollama("http://x", "qwen3.5:9b", "розумний", said.append, broken)
    assert "no space left" in said[-1] and "докачає сама" in said[-1]


def test_ollama_not_installed_is_not_waited_for(monkeypatch):
    naps = []
    session = Ollama(up=False)
    assert not prefetch.ollama_ready("http://x", session, installed=lambda: False, sleep=naps.append)
    assert naps == []
    ticks = iter([0, 0, 1, 70])  # встановлена, але ще запускається — чекаємо, поки не вийде час
    monkeypatch.setattr(prefetch.time, "monotonic", lambda: next(ticks))
    assert not prefetch.ollama_ready("http://x", session, wait=60, installed=lambda: True, sleep=naps.append)
    assert naps == [2, 2]


def fake_hub(monkeypatch, cached=(), free_gb=100.0):
    downloads = []
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")  # fetch_whisper ставить їх лише для свого процесу
    monkeypatch.setenv("HF_HUB_DISABLE_PROGRESS_BARS", "1")

    def snapshot_download(repo, allow_patterns=None, local_files_only=False):
        assert allow_patterns == prefetch.WHISPER_FILES
        if local_files_only:
            if repo not in cached:
                raise FileNotFoundError(repo)
            return "/cache/" + repo
        downloads.append(repo)
        return "/cache/" + repo

    files = [types.SimpleNamespace(rfilename="model.bin", size=3_000_000_000),
             types.SimpleNamespace(rfilename="README.md", size=10_000_000_000),  # не потрібен — не рахуємо
             types.SimpleNamespace(rfilename="vocabulary.json", size=1_000_000)]
    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = snapshot_download
    hub.HfApi = lambda: types.SimpleNamespace(model_info=lambda repo, files_metadata: types.SimpleNamespace(siblings=files))
    hub.constants = types.SimpleNamespace(HF_HUB_CACHE="/nowhere/hub")
    utils = types.ModuleType("faster_whisper.utils")
    utils._MODELS = {"large-v3": "Systran/faster-whisper-large-v3"}
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "faster_whisper", types.ModuleType("faster_whisper"))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    monkeypatch.setattr(prefetch, "_free_gb", lambda folder: free_gb)
    return downloads


def test_whisper_download_uses_the_program_cache_and_checks_disk(monkeypatch):
    said, shown = [], []
    downloads = fake_hub(monkeypatch)
    assert prefetch.fetch_whisper("large-v3", "після пари", said.append, shown.append)
    assert downloads == ["Systran/faster-whisper-large-v3"] and "~3.0 ГБ" in said[0]
    assert shown[-1] == "  100% з 3.0 ГБ"
    downloads = fake_hub(monkeypatch, cached=["Systran/faster-whisper-large-v3"])
    assert prefetch.fetch_whisper("large-v3", "після пари", said.append) and downloads == []
    downloads = fake_hub(monkeypatch, free_gb=4.0)  # 3 ГБ + 2 ГБ запасу не влазить
    assert not prefetch.fetch_whisper("large-v3", "після пари", said.append)
    assert downloads == [] and "лише 4.0 ГБ" in said[-1]


def test_run_without_ollama_still_gets_whisper_and_reports_honestly(monkeypatch):
    said = []
    computer(monkeypatch, 4, 8)
    downloads = fake_hub(monkeypatch)
    sys.modules["faster_whisper.utils"]._MODELS["large-v3-turbo"] = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    monkeypatch.setattr(prefetch, "ollama_ready", lambda url, session: False)
    assert not prefetch.run(Config(), said.append, session=Ollama(up=False))
    assert downloads == ["mobiuslabsgmbh/faster-whisper-large-v3-turbo"]
    assert any("Ollama не запущена" in line and "gemma3:4b" in line for line in said)
    assert said[-1].startswith("Не все вийшло")


def test_ctrl_c_skips_quietly(monkeypatch):
    said = []

    def interrupted(config):
        raise KeyboardInterrupt

    monkeypatch.setattr(prefetch, "whisper_models", interrupted)
    assert not prefetch.run(Config(), said.append, session=Ollama())
    assert "Перервано" in said[-1]


def test_progress_follows_the_growing_model_folder(tmp_path):
    shown = []
    folder = tmp_path / "models--Systran--faster-whisper-large-v3"
    (folder / "blobs").mkdir(parents=True)
    (folder / "blobs" / "old").write_bytes(b"x" * 100)  # що вже було — не рахуємо
    stop = prefetch._watch(folder, 1000, shown.append)
    (folder / "blobs" / "model.bin.incomplete").write_bytes(b"x" * 500)
    deadline = prefetch.time.monotonic() + 5
    while not shown and prefetch.time.monotonic() < deadline:
        prefetch.time.sleep(0.1)
    stop()
    assert shown and shown[0] == "  50% з 0.0 ГБ"
