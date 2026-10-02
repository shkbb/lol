import io
import struct
import threading

from autopara import ears
from autopara.ears import Ears, clean_text, is_silence


def tone(seconds, amplitude):
    return struct.pack(f"<{16000 * seconds}h", *([amplitude, -amplitude] * (8000 * seconds)))


def test_silence_detection():
    assert is_silence(tone(1, 10))
    assert not is_silence(tone(1, 3000))


def test_whisper_hallucinations_are_dropped():
    assert clean_text("  Дякую за перегляд!  ") == ""
    assert clean_text("Субтитри створено спільнотою") == ""
    assert clean_text("Поставте   плюс у чат") == "Поставте плюс у чат"


def test_ears_transcribe_loud_chunks_only(monkeypatch):
    audio = tone(10, 3000) + tone(10, 5) + tone(10, 3000)
    heard, chunks = [], []
    done = threading.Event()

    class FakeParec:
        stdout = io.BytesIO(audio)

        def poll(self):
            return 0

    def transcribe(chunk):
        chunks.append(len(chunk))
        return "Ляшко, ви тут?" if len(chunks) == 1 else "Дякую за перегляд"

    def on_text(text):
        heard.append(text)

    listener = Ears(transcribe, on_text, source="autopara.monitor")
    monkeypatch.setattr(listener, "_record", lambda: FakeParec())
    monkeypatch.setattr(listener._stop, "wait", lambda seconds: done.set() or listener._stop.set())
    listener._run()
    assert chunks == [ears.RATE * 2 * 10] * 2  # тихий шматок навіть не розпізнавали
    assert heard == ["Ляшко, ви тут?"]  # галюцинацію відкинуто


def test_null_sink_cleans_up_stale_one(monkeypatch):
    calls = []

    class Result:
        def __init__(self, stdout="", returncode=0):
            self.stdout, self.returncode, self.stderr = stdout, returncode, ""

    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["pactl", "list", "short"]:
            return Result("7\tmodule-null-sink\tsink_name=autopara sink_properties=device.description=autopara\n"
                          "8\tmodule-null-sink\tsink_name=other\n")
        if args[:2] == ["pactl", "load-module"]:
            return Result("42\n")
        return Result()

    monkeypatch.setattr(ears.subprocess, "run", run)
    sink = ears.NullSink()
    sink.create()
    sink.close()
    assert ["pactl", "unload-module", "7"] in calls and ["pactl", "unload-module", "8"] not in calls
    assert calls[-1] == ["pactl", "unload-module", "42"]
