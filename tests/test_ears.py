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

    class FakeStream:
        data = io.BytesIO(audio)

        def read(self, size):
            return self.data.read(size) or None

        def close(self):
            pass

    def transcribe(chunk):
        chunks.append(len(chunk))
        if len(chunks) == 1:
            return [Heard("Ляшко, ви тут?", True), Heard("щось нерозбірливе", False)]
        return [Heard("Дякую за перегляд", True)]

    listener = Ears(transcribe, lambda text, confident: heard.append((text, confident)), source="autopara.monitor")
    monkeypatch.setattr(listener, "_record", lambda: FakeStream())
    monkeypatch.setattr(listener._stop, "wait", lambda seconds: done.set() or listener._stop.set())
    listener._run()
    # Різ — у паузі після першої фрази (10 с мови + пів секунди тиші), тиша сама по собі не розпізнається,
    # а друга фраза — коли звук скінчився.
    assert chunks[0] == ears.RATE * 2 * 10 + ears.RATE and len(chunks) == 2
    assert 10 <= chunks[1] / ears.RATE / 2 <= 11  # з секундою тиші перед фразою, не більше
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
    # Наживо на «ар ар ар» з Discord Whisper «почув» шматок імені й назву пари — це вигадка, не виклик.
    hint = "Ляшко Денис, Тестовий вхід"
    assert ears.is_hint_echo("Денис, Тестовий вхід", hint)
    assert ears.is_hint_echo("Тестовий вхід, Денис, Тестовий вхід, момент з Тестовий вхід", hint)
    assert not ears.is_hint_echo("Ляшко Денис, ви тут?", hint)
    assert not ears.is_hint_echo("Денисе, а що скажете про тестовий вхід у базу?", hint)


def test_old_faster_whisper_without_hotwords(monkeypatch):
    calls = fake_whisper(monkeypatch, [Segment("Добрий день")], supports_hotwords=False)
    transcriber = WhisperTranscriber("small", "uk", hints=lambda: "Ляшко Денис")
    assert transcriber(tone(1, 3000)) == [Heard("Добрий день", True)]
    assert "hotwords" not in calls[0]


def test_segments_end_at_pauses_not_mid_word():
    from autopara.ears import Segmenter

    segmenter = Segmenter()
    # Фраза 4 с, пауза 1 с, фраза 5 с, пауза — різ лише в паузі після 6 с, а не посеред фрази.
    ready = segmenter.feed(tone(4, 3000) + tone(1, 0) + tone(5, 3000))
    assert ready == []
    ready = segmenter.feed(tone(1, 0))
    assert [len(chunk) / 32000 for chunk in ready] == [10.5]
    assert segmenter.feed(tone(10, 0)) == [] and segmenter.flush() == b""  # тиша нікуди не йде

    # Говорять без пауз — ріжемо не пізніше ніж за 25 с, у найтихішому місці.
    quieter = tone(1, 3000)[:16000] + tone(1, 400)[:16000]
    ready = segmenter.feed(tone(20, 3000) + quieter + tone(10, 3000))
    assert len(ready) == 1 and 20 <= len(ready[0]) / 32000 <= 25
    assert len(segmenter.flush()) / 32000 > 5  # решта — у наступному шматку, нічого не загублено


def test_whisper_loops_are_collapsed():
    assert clean_text("Тестування, Тестування, Тестування, Тестування, Тестування далі") == "Тестування, далі"
    assert clean_text("так, так, так") == "так, так, так"  # звичайне повторення лишається


def test_warns_when_model_is_too_slow(caplog):
    listener = Ears(lambda chunk: [], lambda text, confident: None, source="x")
    for _ in range(4):
        listener._watch_speed(12)  # трохи відстає — це нормально: точність важливіша, наздожене в паузах
    assert "не встигає" not in caplog.text
    for _ in range(4):
        listener._watch_speed(35)  # утричі повільніше за мову — тут уже відставання росло б без меж
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
        listener._watch_speed(12)
    assert transcriber.name == "large-v3-turbo"  # трохи повільніше — лишаємо точну
    for _ in range(3):
        listener._watch_speed(35)
    assert transcriber.name == "small" and transcriber.model.name == "small"
    listener._watch_speed(2.0)
    assert transcriber.name == "small"


def test_browser_audio_stream():
    stream = ears.AudioQueue(max_seconds=1)
    assert stream.read(100, timeout=0.01) == b""  # не на парі — просто тиша
    stream.feed(b"\x01\x00" * 10)
    stream.feed_base64("AgADAA==")  # два відліки з браузера
    assert stream.read(8) == b"\x01\x00" * 4
    assert stream.read(100) == b"\x01\x00" * 6 + b"\x02\x00\x03\x00"
    stream.feed(b"\x00" * (ears.RATE * 2 * 2))  # більше, ніж влазить, — старе відкидається
    assert len(stream.read(10**9)) <= ears.RATE * 2
    stream.close()
    assert stream.read(10) is None


def test_ears_flush_the_tail_when_the_class_ends():
    """Звук з браузера припинився — хвіст розпізнаємо одразу, а не разом з наступною парою."""
    chunks = []
    stream = ears.AudioQueue()
    listener = Ears(lambda chunk: chunks.append(len(chunk)) or [], lambda text, confident: None, stream)
    stream.feed(tone(4, 3000))
    listener.start()
    for _ in range(100):
        if chunks:
            break
        threading.Event().wait(0.1)
    listener.stop()
    assert chunks == [ears.RATE * 2 * 4]


def test_audio_source_choice(monkeypatch):
    monkeypatch.setattr(ears, "pulse_available", lambda: None)
    assert ears.pick_source("auto") == "pulse"  # Linux: звук самого браузера програми — так чути справжній Meet
    monkeypatch.setattr(ears, "pulse_available", lambda: "немає pactl")
    assert ears.pick_source("auto") == "browser"  # без системного звуку (Windows, macOS) — зі сторінки пари
    assert ears.pick_source("browser") == "browser"
    assert ears.pick_source("pulse") == "pulse"


ZOOM_JSON = """[
  {"index": 41, "sink": 0, "properties": {"application.name": "Firefox", "application.process.binary": "firefox"}},
  {"index": 42, "sink": 3, "properties": {"application.name": "ZOOM VoiceEngine", "application.process.binary": "zoom"}}
]"""

ZOOM_TEXT = """Sink Input #41
\tSink: 0
\tProperties:
\t\tapplication.name = "YouTube Music"
Sink Input #42
\tSink: 3
\tProperties:
\t\tapplication.name = "ZOOM VoiceEngine"
\t\tapplication.process.binary = "zoom"
"""


class FakePactl:
    def __init__(self, json_ok=True):
        self.calls = []
        self.json_ok = json_ok

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("-f", "json"):
            return types.SimpleNamespace(returncode=0 if self.json_ok else 1, stdout=ZOOM_JSON if self.json_ok else "")
        if args[:2] == ("list", "sink-inputs"):
            return types.SimpleNamespace(returncode=0, stdout=ZOOM_TEXT)
        return types.SimpleNamespace(returncode=0, stdout="")


def test_sink_inputs_from_json_or_old_text(monkeypatch):
    monkeypatch.setattr(ears, "_pactl", FakePactl(json_ok=True))
    assert [(s["index"], s["sink"]) for s in ears.sink_inputs() if "zoom" in s["app"]] == [(42, 3)]
    monkeypatch.setattr(ears, "_pactl", FakePactl(json_ok=False))
    streams = ears.sink_inputs()
    assert streams == [{"index": 41, "sink": 0, "app": "YouTube Music"}, {"index": 42, "sink": 3, "app": "ZOOM VoiceEngine zoom"}]


def test_only_zoom_is_moved_and_then_returned(monkeypatch):
    """Вухам потрапляє лише Zoom: Firefox з YouTube лишається, де був."""
    pactl = FakePactl()
    monkeypatch.setattr(ears, "_pactl", pactl)
    capture = ears.PulseAppCapture(ears.AudioQueue(), hear_too=True, sink=types.SimpleNamespace(name="autopara"))
    capture._target = "autopara"
    capture._thread = threading.Thread(target=lambda: None)  # як після start(), але без стеження у фоні
    capture._thread.start()
    capture.move_streams()
    capture.move_streams()  # уже перенесений — вдруге не чіпаємо
    capture.stop()
    moves = [args for args in pactl.calls if args[0] == "move-sink-input"]
    assert moves == [("move-sink-input", "42", "autopara"), ("move-sink-input", "42", "3")]


def test_resampling_to_whisper_rate():
    import math

    for rate in (48_000, 44_100, 16_000):
        tone = [0.5 * math.sin(2 * math.pi * 440 * i / rate) for i in range(rate)]  # 1 с, 440 Гц
        pcm = ears.to_pcm16(tone, rate)
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
        assert abs(len(samples) - ears.RATE) <= 1
        crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
        assert 860 <= crossings <= 900  # той самий тон
        assert 15_000 < max(samples) < 17_000


def test_virtual_cable_for_zoom_client(monkeypatch):
    import numpy as np

    streams = []

    class InputStream:
        def __init__(self, **options):
            self.options = options
            self.running = False
            streams.append(self)

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

        def close(self):
            pass

    devices = [
        {"name": "Microphone (Realtek)", "max_input_channels": 2, "default_samplerate": 48000.0},
        {"name": "Speakers", "max_input_channels": 0, "default_samplerate": 48000.0},
        {"name": "CABLE Output (VB-Audio Virtual Cable)", "max_input_channels": 2, "default_samplerate": 48000.0},
    ]
    fake = types.ModuleType("sounddevice")
    fake.query_devices = lambda index=None: devices if index is None else devices[index]
    fake.InputStream = InputStream
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    assert ears.list_input_devices() == ["Microphone (Realtek)", "CABLE Output (VB-Audio Virtual Cable)"]
    queue = ears.AudioQueue()
    capture = ears.DeviceAppCapture(queue, "cable output")
    capture.start()
    [stream] = streams
    assert stream.options["device"] == 2 and stream.running
    stream.options["callback"](np.full((4800, 2), 0.25, dtype=np.float32), 4800, None, None)
    assert len(queue.read(10**6)) == 1600 * 2  # 0,1 с при 48 кГц → 1600 відліків при 16 кГц
    capture.stop()
    assert not stream.running


def test_missing_cable_is_reported_not_fatal(monkeypatch, caplog):
    fake = types.ModuleType("sounddevice")
    fake.query_devices = lambda index=None: []
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    ears.DeviceAppCapture(ears.AudioQueue(), "CABLE Output").start()
    assert "не знайдено" in caplog.text


def test_model_is_picked_by_computer_power(monkeypatch):
    monkeypatch.setattr(ears.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(ears, "total_ram_gb", lambda: 16)
    assert ears.resolve_model("auto") == "large-v3-turbo"  # наживо головне — встигати
    assert ears.resolve_refine_model("auto", "auto") == "large-v3"  # після пари — точніша
    assert ears.resolve_refine_model("off", "auto") == "off"
    monkeypatch.setattr(ears, "total_ram_gb", lambda: 8)
    assert ears.resolve_refine_model("auto", "auto") == "large-v3-turbo"  # слабшому — та сама, що наживо
    monkeypatch.setattr(ears.os, "cpu_count", lambda: 2)
    assert ears.resolve_model("auto") == "small"
    assert ears.resolve_model("medium") == "medium"
    assert ears.total_ram_gb.__module__  # справжня функція теж працює на цій ОС
    monkeypatch.undo()
    assert ears.total_ram_gb() > 0


def test_model_download_is_retried_until_it_works(monkeypatch):
    """Ноут увімкнули ще без інтернету — модель не завантажилась; вуха спробують ще раз, а не здадуться."""
    tries = []

    def load():
        tries.append(1)
        if len(tries) == 1:
            raise OSError("немає мережі")
        return lambda chunk: []

    ready = []
    listener = Ears(None, lambda text, confident: None, ears.AudioQueue(), load=load)
    listener.on_ready = lambda: (ready.append(True), listener._stop.set())  # готово — далі не слухаємо
    monkeypatch.setattr(listener._stop, "wait", lambda seconds: None)
    listener._run()
    assert len(tries) == 2 and ready == [True]


def test_cable_is_found_without_asking(monkeypatch):
    devices = [
        {"name": "Microphone Array", "max_input_channels": 2},
        {"name": "BlackHole 2ch", "max_input_channels": 2},
    ]
    fake = types.ModuleType("sounddevice")
    fake.query_devices = lambda index=None: devices
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    assert ears.find_cable() == "BlackHole 2ch"
    devices.pop()
    assert ears.find_cable() == ""
