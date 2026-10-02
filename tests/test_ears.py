import io
import struct
import sys
import threading
import types

from autopara import ears
from autopara.ears import Ears, Heard, WhisperTranscriber, clean_text, is_silence


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
        if len(chunks) == 1:
            return [Heard("Ляшко, ви тут?", True), Heard("щось нерозбірливе", False)]
        return [Heard("Дякую за перегляд", True)]

    listener = Ears(transcribe, lambda text, confident: heard.append((text, confident)), source="autopara.monitor")
    monkeypatch.setattr(listener, "_record", lambda: FakeParec())
    monkeypatch.setattr(listener._stop, "wait", lambda seconds: done.set() or listener._stop.set())
    listener._run()
    assert chunks == [ears.RATE * 2 * 10] * 2  # тихий шматок навіть не розпізнавали
    assert heard == [("Ляшко, ви тут?", True), ("щось нерозбірливе", False)]  # галюцинацію відкинуто


class Segment:
    def __init__(self, text, avg_logprob=-0.3, no_speech_prob=0.05):
        self.text, self.avg_logprob, self.no_speech_prob = text, avg_logprob, no_speech_prob


def fake_whisper(monkeypatch, segments, supports_hotwords=True):
    calls = []

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            self.name = name

        def transcribe(self, audio, **options):
            if "hotwords" in options and not supports_hotwords:
                raise TypeError("unexpected keyword argument 'hotwords'")
            calls.append(options)
            return iter(segments), None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return calls


def test_transcriber_reports_confidence_and_uses_name_hints(monkeypatch):
    calls = fake_whisper(monkeypatch, [
        Segment(" Ляшко, ви тут? "),
        Segment("плюс чи мінус", avg_logprob=-1.2),
        Segment("ммм", no_speech_prob=0.9),
        Segment("Ляшко Денис, Петренко П.П."),  # Whisper «вимовив» підказку на тиші
    ])
    transcriber = WhisperTranscriber("small", "uk", hints=lambda: "Ляшко Денис, Петренко П.П., Бази даних")
    result = transcriber(tone(1, 3000))
    assert result == [Heard("Ляшко, ви тут?", True), Heard("плюс чи мінус", False), Heard("ммм", False)]
    assert calls[0]["hotwords"] == "Ляшко Денис, Петренко П.П., Бази даних" and calls[0]["language"] == "uk"


def test_real_call_by_name_is_not_mistaken_for_hint_echo():
    assert not ears.is_hint_echo("Ляшко Денис", "Ляшко Денис, Петренко П.П., Бази даних")
    assert ears.is_hint_echo("Ляшко Денис. Бази даних.", "Ляшко Денис, Петренко П.П., Бази даних")


def test_old_faster_whisper_without_hotwords(monkeypatch):
    calls = fake_whisper(monkeypatch, [Segment("Добрий день")], supports_hotwords=False)
    transcriber = WhisperTranscriber("small", "uk", hints=lambda: "Ляшко Денис")
    assert transcriber(tone(1, 3000)) == [Heard("Добрий день", True)]
    assert "hotwords" not in calls[0]


def test_warns_when_model_is_too_slow(caplog):
    listener = Ears(lambda chunk: [], lambda text, confident: None, source="x")
    for _ in range(4):
        listener._watch_speed(9.5)
    assert caplog.text.count("не встигає") == 1


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


def test_loopback_lets_you_hear_the_class_too(monkeypatch):
    loaded, unloaded = [], []

    class Result:
        def __init__(self, stdout=""):
            self.stdout, self.returncode, self.stderr = stdout, 0, ""

    def run(args, **kwargs):
        if args[:2] == ["pactl", "load-module"]:
            loaded.append(args[2:])
            return Result(f"{len(loaded)}\n")
        if args[:2] == ["pactl", "unload-module"]:
            unloaded.append(args[2])
        return Result()

    monkeypatch.setattr(ears.subprocess, "run", run)
    sink = ears.NullSink(loopback=True)
    sink.create()
    assert loaded[1] == ["module-loopback", "source=autopara.monitor", "latency_msec=60"]
    sink.close()
    assert unloaded == ["2", "1"]  # спершу дубль, потім сам вихід


def test_too_slow_model_switches_to_a_lighter_one(monkeypatch):
    fake_whisper(monkeypatch, [])
    transcriber = WhisperTranscriber("large-v3-turbo", "uk")
    listener = Ears(transcriber, lambda text, confident: None, source="x")
    for _ in range(3):
        listener._watch_speed(9.5)
    assert transcriber.name == "small" and transcriber.model.name == "small"
    listener._watch_speed(2.0)
    assert transcriber.name == "small"
