"""«Вуха»: слухають пару й перетворюють мову на текст — локально, нічого нікуди не надсилаючи.

Звідки береться звук (усе потрапляє в одну чергу ``AudioQueue``):
- **пара в браузері** (Meet, Zoom у браузері; будь-яка ОС): вкладка пари сама збирає голоси учасників
  (ears_tap.js) і віддає їх програмі. Лише ця вкладка — ні YouTube, ні інші вкладки сюди не потраплять;
- **Zoom-клієнт**: на Linux на час пари переносимо *лише його* аудіопотоки у віртуальний вихід
  (PipeWire/PulseAudio) і записуємо звідти; на Windows і macOS — записуємо віртуальний кабель
  (VB-Cable, BlackHole), у який Zoom виводить звук;
- **запасний шлях на Linux** (``[ears] source = "pulse"``): увесь звук браузера програми йде у віртуальний
  вихід ``autopara`` (PULSE_SINK), і вуха слухають його.
Якщо mute_sound = false, звук з віртуального виходу ще й дублюється в динаміки (module-loopback).
Далі однаково: шматки по ~10 с → мова в текст (faster-whisper).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from array import array
from collections import deque
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from time import monotonic
from typing import Callable, Protocol

log = logging.getLogger(__name__)

SINK = "autopara"
RATE = 16_000  # Гц, моно, 16 біт — те, що їсть whisper
CHUNK_SECONDS = 10
SILENCE_RMS = 150
DEBUG_AUDIO = Path.home() / ".autopara" / "debug-ears.raw"  # лише в autopara -v: сирий звук 16 кГц моно s16le
SOURCES = ("auto", "browser", "pulse")  # звук пар у браузері: зі сторінки пари чи весь звук браузера (Linux)
APP_STREAM_RE = re.compile(r"zoom", re.I)  # аудіопотоки Zoom-клієнта в PipeWire/PulseAudio
TAP_SCRIPT = Path(__file__).with_name("ears_tap.js")
# Якщо модель не встигає за мовою — на яку переходити (turbo зазвичай не повільніший за medium).
LIGHTER_MODEL = {
    "large-v3": "large-v3-turbo",
    "large-v2": "large-v3-turbo",
    "large-v3-turbo": "small",
    "turbo": "small",
    "medium": "small",
    "small": "base",
}
# Пороги впевненості Whisper: середня log-імовірність слів і ймовірність, що це взагалі не мова.
CONFIDENT_LOGPROB = -0.7
CONFIDENT_NO_SPEECH = 0.5

# Whisper на тиші чи музиці любить «галюцинувати» ці фрази.
HALLUCINATIONS = re.compile(
    r"дякую за перегляд|субтитр|продовження в наступн|підписуйтесь|amara\.org|"
    r"редактор субтитрів|спасибо за просмотр|thanks for watching",
    re.I,
)


class EarsError(Exception):
    pass


def pulse_available() -> str | None:
    """None, якщо можна слухати системний звук Linux (PipeWire/PulseAudio), інакше — чого бракує."""
    if not sys.platform.startswith("linux"):
        return "системний звук слухаю лише на Linux"
    missing = [tool for tool in ("pactl", "parec") if shutil.which(tool) is None]
    if missing:
        return f"бракує {', '.join(missing)} — встанови пакет libpulse (працює і з PipeWire)"
    return None


def whisper_available() -> str | None:
    import importlib.util

    if importlib.util.find_spec("faster_whisper") is None:
        return 'немає faster-whisper — встанови: pip install -e ".[ears]"'
    return None


def pick_source(setting: str) -> str:
    """"pulse" чи "browser" для налаштування [ears] source.

    За замовчуванням на Linux — звук самого браузера програми через PipeWire/PulseAudio: так вуха чують
    рівно те, що браузер грає, як би сторінка всередині не була влаштована (справжній Meet віддає збирачу
    на сторінці тишу). У браузері програми відкрита лише пара, тож чужого туди не потрапляє.
    Деінде (і якщо системного звуку немає) — зі сторінки пари.
    """
    if setting == "browser":
        return "browser"
    if setting == "pulse":
        return "pulse"
    return "pulse" if pulse_available() is None else "browser"


class AudioStream(Protocol):
    def read(self, size: int) -> bytes | None:
        """До ``size`` байтів; b"" — поки нічого (тиша, не на парі); None — потік закрився."""

    def close(self) -> None: ...


class PulseStream:
    """Запис системного звуку Linux через parec."""

    def __init__(self, device: str):
        self.process = record_process(device)

    def read(self, size: int) -> bytes | None:
        return self.process.stdout.read(size) or None

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()


class AudioQueue:
    """Звук пари просто з браузера: сторінка (ears_tap.js) кладе сюди шматки по ~0,5 с.

    ``feed`` викликає потік браузера, ``read`` — потік вух. Якщо розпізнавання не встигає,
    найстаріший звук відкидаємо, щоб не відставати від пари все більше.
    """

    def __init__(self, max_seconds: int = 120):
        self.max_bytes = RATE * 2 * max_seconds
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._closed = False
        self._ready = threading.Condition()
        self._warned = False

    def feed(self, pcm: bytes) -> None:
        with self._ready:
            if self._closed or not pcm:
                return
            self._chunks.append(pcm)
            self._size += len(pcm)
            while self._size > self.max_bytes:
                self._size -= len(self._chunks.popleft())
                if not self._warned:
                    self._warned = True
                    log.warning("Розпізнавання не встигає за звуком пари — пропускаю найстаріше")
            self._ready.notify()

    def feed_base64(self, data: str) -> None:
        self.feed(base64.b64decode(data))

    def read(self, size: int, timeout: float = 1.0) -> bytes | None:
        with self._ready:
            if not self._chunks and not self._closed:
                self._ready.wait(timeout)
            if self._closed:
                return None
            out = bytearray()
            while self._chunks and len(out) < size:
                chunk = self._chunks.popleft()
                take = size - len(out)
                out += chunk[:take]
                if len(chunk) > take:
                    self._chunks.appendleft(chunk[take:])
            self._size -= len(out)
            return bytes(out)

    def close(self) -> None:
        with self._ready:
            self._closed = True
            self._ready.notify_all()


class NullSink:
    """Віртуальний аудіовихід, куди браузер віддає звук пари.

    З ``loopback=True`` звук звідти ще й дублюється в звичайні динаміки/навушники (module-loopback),
    тож пару чуєш і ти, і вуха. Гучність цього дубля видно в налаштуваннях звуку GNOME як окремий застосунок.
    """

    def __init__(self, name: str = SINK, loopback: bool = False):
        self.name = name
        self.loopback = loopback
        self._modules: list[str] = []

    @property
    def monitor(self) -> str:
        return f"{self.name}.monitor"

    def _load(self, *args: str) -> None:
        result = subprocess.run(["pactl", "load-module", *args], capture_output=True, text=True)
        if result.returncode != 0:
            self.close()
            raise EarsError(f"не вдалося налаштувати звук ({args[0]}): {result.stderr.strip()}")
        self._modules.append(result.stdout.strip())

    def create(self) -> None:
        # Якщо минулий запуск упав, його модулі могли лишитись — прибираємо, щоб не плодити дублікати.
        modules = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True).stdout
        for line in modules.splitlines():
            module_id, _, rest = line.partition("\t")
            arguments = rest.split()
            ours = (
                ("module-null-sink" in rest and f"sink_name={self.name}" in arguments)
                or ("module-loopback" in rest and f"source={self.monitor}" in arguments)
            )
            if ours:
                subprocess.run(["pactl", "unload-module", module_id], capture_output=True)
        self._load("module-null-sink", f"sink_name={self.name}", f"sink_properties=device.description={self.name}")
        if self.loopback:
            # Без sink= дубль іде на поточний вихід і сам перемикається, коли підключаєш навушники.
            self._load("module-loopback", f"source={self.monitor}", "latency_msec=60")

    def close(self) -> None:
        for module in reversed(self._modules):
            subprocess.run(["pactl", "unload-module", module], capture_output=True)
        self._modules = []


def loudness(pcm: bytes) -> int:
    """Гучність шматка (RMS у відліках 16 біт): тиша — до ~150, звичайна мова — тисячі."""
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return 0
    step = max(1, len(samples) // 4000)  # для оцінки гучності вистачить кожного n-го відліку
    picked = samples[::step]
    return int(sqrt(sum(s * s for s in picked) / len(picked)))


def is_silence(pcm: bytes) -> bool:
    return loudness(pcm) < SILENCE_RMS


def clean_text(text: str) -> str:
    text = " ".join(text.split())
    return "" if not text or HALLUCINATIONS.search(text) else text


@dataclass(frozen=True)
class Heard:
    text: str
    confident: bool  # чи сама модель упевнена, що почула саме це


def _letters(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def total_ram_gb() -> float:
    """Скільки оперативної пам'яті в комп'ютері (0 — не вдалося дізнатися)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong)
                    for name in ("total", "available", "total_page", "available_page", "total_virtual", "available_virtual", "extended")
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
            return status.total / 2**30
        if sys.platform == "darwin":
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 2**30
        with open("/proc/meminfo", encoding="ascii") as f:
            return int(f.readline().split()[1]) / 2**20
    except (OSError, ValueError, AttributeError):
        return 0.0


def resolve_model(model: str) -> str:
    """"auto" → модель за потужністю: точна large-v3-turbo там, де вона встигатиме, інакше small.

    Якщо все ж не встигатиме за мовою, вуха самі перейдуть на легшу (див. LIGHTER_MODEL).
    """
    if model != "auto":
        return model
    strong = (os.cpu_count() or 1) >= 8 and total_ram_gb() >= 12
    return "large-v3-turbo" if strong else "small"


class WhisperTranscriber:
    """Whisper (faster-whisper, локально на процесорі). Для кожного шматка повертає ще й упевненість.

    ``hints`` — підказка з іменами (викладач, ти, назва пари): так Whisper рідше перекручує прізвища.
    """

    def __init__(self, model: str = "small", language: str = "uk", hints: Callable[[], str] | None = None):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise EarsError('немає faster-whisper — встанови: pip install -e ".[ears]"') from e
        self._factory = WhisperModel
        self.language = language
        self.hints = hints
        self._hotwords = True
        self._load(resolve_model(model))

    def _load(self, model: str) -> None:
        log.info("Завантажую модель розпізнавання мови %r (перший раз — кілька хвилин)...", model)
        self.name = model
        self.model = self._factory(model, device="cpu", compute_type="int8")

    def downgrade(self) -> bool:
        """Перейти на легшу модель, якщо ця не встигає за парою. False — легшої немає."""
        lighter = LIGHTER_MODEL.get(self.name)
        if lighter is None:
            return False
        log.warning("Модель %s не встигає за парою — переходжу на %s", self.name, lighter)
        self._load(lighter)
        return True

    def __call__(self, pcm: bytes) -> list[Heard]:
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # temperature=0 — один прохід: на нечіткому звуці Whisper інакше перерозпізнає шматок до 6 разів
        # (0,2…1,0) — і точна модель перестає встигати за парою, а «гарячіші» спроби ще й вигадують.
        options = dict(language=self.language, vad_filter=True, beam_size=1, condition_on_previous_text=False,
                       temperature=0.0)
        hint = self.hints() if self.hints else ""
        if hint and self._hotwords:
            options["hotwords"] = hint
        try:
            segments, _ = self.model.transcribe(audio, **options)
        except TypeError:  # стара версія faster-whisper без hotwords
            self._hotwords = False
            options.pop("hotwords", None)
            segments, _ = self.model.transcribe(audio, **options)
        return [
            Heard(segment.text.strip(), is_confident(segment.avg_logprob, segment.no_speech_prob))
            for segment in segments
            if segment.text.strip() and not is_hint_echo(segment.text, hint)
        ]


def is_hint_echo(text: str, hint: str) -> bool:
    """Whisper іноді «вимовляє» саму підказку на тиші чи незрозумілому звуці («Денис, Тестовий вхід»).
    Справжня мова рідко складається майже лише зі слів підказки, ще й з кількох її частин (твоє ім'я,
    викладач, назва пари), а сам виклик «Ляшко Денис» — лишаємо."""
    letters = _letters(text)
    parts = [_letters(part) for part in hint.split(",")]
    if sum(1 for part in parts if part and part in letters) >= 2:
        return True
    words = [word for word in letters.split() if len(word) > 1]  # «з», «і» нічого не важать
    part_words = [set(part.split()) for part in parts if part]
    known = set().union(*part_words) if part_words else set()
    if not words or not known:
        return False
    share = sum(1 for word in words if word in known) / len(words)
    touched = sum(1 for part in part_words if part & set(words))
    return share >= 0.75 and touched >= 2


def is_confident(avg_logprob: float, no_speech_prob: float) -> bool:
    """Whisper сам оцінює, наскільки певен. Нижче цих порогів — швидше «почулось», ніж почув."""
    return avg_logprob > CONFIDENT_LOGPROB and no_speech_prob < CONFIDENT_NO_SPEECH


class PulseRecorder:
    """Фоновий потік: системний звук (parec з монітора віртуального виходу) → черга вух."""

    def __init__(self, device: str, queue: AudioQueue):
        self.device = device
        self.queue = queue
        self._stream: PulseStream | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="pulse-recorder", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            self._stream.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stream = PulseStream(self.device)
            while not self._stop.is_set():
                data = self._stream.read(RATE)
                if data is None:
                    break
                self.queue.feed(data)
            self._stream.close()
            if not self._stop.is_set():
                log.warning("Запис звуку обірвався — перезапускаю за 5 с")
                self._stop.wait(5)


def _pactl(*args: str) -> subprocess.CompletedProcess:
    # LC_ALL=C — щоб вивід pactl не був перекладений і його можна було розібрати.
    return subprocess.run(["pactl", *args], capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"})


def sink_inputs() -> list[dict]:
    """Аудіопотоки програм: [{"index", "sink", "app"}] — app — назва й файл програми."""
    result = _pactl("-f", "json", "list", "sink-inputs")
    if result.returncode == 0:
        try:
            return [
                {
                    "index": int(item["index"]),
                    "sink": item.get("sink"),
                    "app": " ".join(
                        str((item.get("properties") or {}).get(key, ""))
                        for key in ("application.name", "application.process.binary")
                    ),
                }
                for item in json.loads(result.stdout)
            ]
        except (ValueError, KeyError, TypeError):
            pass
    return parse_sink_inputs(_pactl("list", "sink-inputs").stdout)  # старий pactl без JSON


def parse_sink_inputs(text: str) -> list[dict]:
    streams = []
    for block in re.split(r"^Sink Input #", text, flags=re.M)[1:]:
        index = re.match(r"(\d+)", block)
        sink = re.search(r"^\s*Sink:\s*(\d+)", block, re.M)
        names = re.findall(r'^\s*application\.(?:name|process\.binary)\s*=\s*"([^"]*)"', block, re.M)
        if index:
            streams.append({"index": int(index.group(1)), "sink": int(sink.group(1)) if sink else None, "app": " ".join(names)})
    return streams


class PulseAppCapture:
    """Linux: звук Zoom-клієнта на час пари — переносимо *лише його* потоки у віртуальний вихід і слухаємо.

    Після пари потоки повертаються туди, де були (у навушники чи динаміки). ``sink`` — уже наявний
    вихід (запасний шлях, де його й так записують); інакше створюємо свій на час пари.
    """

    def __init__(self, queue: AudioQueue, hear_too: bool, sink: NullSink | None = None):
        self.queue = queue
        self.hear_too = hear_too
        self._shared = sink
        self._sink: NullSink | None = None
        self._recorder: PulseRecorder | None = None
        self._moved: dict[int, object] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        target = self._shared
        if target is None:
            target = NullSink(name=f"{SINK}_zoom", loopback=self.hear_too)
            try:
                target.create()
            except EarsError as e:
                log.warning("Не можу слухати Zoom-клієнт: %s", e)
                return
            self._sink = target
            self._recorder = PulseRecorder(target.monitor, self.queue)
            self._recorder.start()
        self._target = target.name
        self._stop.clear()
        self._thread = threading.Thread(target=self._follow, name="zoom-audio", daemon=True)
        self._thread.start()

    def _follow(self) -> None:
        # Zoom створює потоки не одразу й може пересоздати їх (змінив навушники) — стежимо всю пару.
        while not self._stop.is_set():
            try:
                self.move_streams()
            except OSError as e:
                log.debug("pactl: %s", e)
            self._stop.wait(3)

    def move_streams(self) -> None:
        for stream in sink_inputs():
            if stream["index"] in self._moved or not APP_STREAM_RE.search(stream["app"]):
                continue
            if _pactl("move-sink-input", str(stream["index"]), self._target).returncode == 0:
                if not self._moved:
                    log.info("👂 Слухаю Zoom-клієнт")
                self._moved[stream["index"]] = stream["sink"]

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=10)
        self._thread = None
        for index, original in self._moved.items():
            back = str(original) if original is not None else "@DEFAULT_SINK@"
            if _pactl("move-sink-input", str(index), back).returncode != 0:
                _pactl("move-sink-input", str(index), "@DEFAULT_SINK@")
        self._moved = {}
        if self._recorder is not None:
            self._recorder.stop()
            self._recorder = None
        if self._sink is not None:
            self._sink.close()
            self._sink = None


def to_pcm16(samples, rate: int) -> bytes:
    """Моно float -1..1 з частотою ``rate`` → 16 кГц, 16 біт (як хоче whisper)."""
    import numpy as np

    samples = np.asarray(samples, dtype=np.float32)
    if rate % RATE == 0 and rate != RATE:
        factor = rate // RATE  # середнє по вікну — заодно прибирає частоти, яких whisper не чує
        samples = samples[: len(samples) - len(samples) % factor].reshape(-1, factor).mean(axis=1)
    elif rate != RATE and len(samples) > 1:
        count = max(1, int(round(len(samples) * RATE / rate)))
        samples = np.interp(np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples)
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def _sounddevice():
    try:
        import sounddevice
    except (ImportError, OSError) as e:
        raise EarsError('немає sounddevice — встанови: pip install -e ".[ears]"') from e
    return sounddevice


CABLE_RE = re.compile(r"cable output|blackhole|vb-audio", re.I)


def find_cable() -> str:
    """Встановлений віртуальний кабель (VB-Cable, BlackHole) — щоб не вибирати його вручну."""
    return next((name for name in list_input_devices() if CABLE_RE.search(name)), "")


def list_input_devices() -> list[str]:
    """Назви пристроїв запису — для вибору віртуального кабелю у вікні програми."""
    try:
        sd = _sounddevice()
        names = [device["name"] for device in sd.query_devices() if device["max_input_channels"] > 0]
    except Exception:  # noqa: BLE001 — немає звукової системи чи бібліотеки: просто порожній список
        return []
    return list(dict.fromkeys(names))


class DeviceAppCapture:
    """Windows/macOS: звук Zoom-клієнта з віртуального кабелю (VB-Cable, BlackHole).

    У Zoom динаміком обрано кабель, а ми записуємо його «вихід» — тож чуємо лише Zoom, а не YouTube
    чи інші програми.
    """

    def __init__(self, queue: AudioQueue, device: str):
        self.queue = queue
        self.device = device
        self._stream = None

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            sd = _sounddevice()
            matches = [
                index
                for index, info in enumerate(sd.query_devices())
                if info["max_input_channels"] > 0 and self.device.casefold() in info["name"].casefold()
            ]
            if not matches:
                raise EarsError(f"пристрій «{self.device}» не знайдено — перевір, що кабель встановлено")
            info = sd.query_devices(matches[0])
            rate = int(info["default_samplerate"])
            channels = max(1, min(2, int(info["max_input_channels"])))

            def callback(indata, frames, moment, status) -> None:
                self.queue.feed(to_pcm16(indata.mean(axis=1), rate))

            self._stream = sd.InputStream(
                device=matches[0], channels=channels, samplerate=rate, dtype="float32", blocksize=rate // 2,
                callback=callback,
            )
            self._stream.start()
            log.info("👂 Слухаю Zoom-клієнт через «%s»", info["name"])
        except Exception as e:  # noqa: BLE001 — без звуку Zoom решта пари однаково працює
            self._stream = None
            log.warning("Не можу слухати Zoom-клієнт: %s", e)

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None


class NoAppCapture:
    """Zoom-клієнт почути нема як — кажемо, що робити."""

    def __init__(self, reason: str):
        self.reason = reason

    def start(self) -> None:
        log.warning("Zoom-клієнт вуха не чують: %s", self.reason)

    def stop(self) -> None:
        pass


class Ears:
    """Фоновий потік: звук → шматки по ~10 с → текст → on_text(текст, чи впевнено).

    ``source`` — пристрій PulseAudio (рядок, наприклад "autopara.monitor") або готовий потік
    (AudioQueue).
    """

    def __init__(
        self,
        transcribe: Callable[[bytes], list[Heard]] | None,
        on_text: Callable[[str, bool], None],
        source: str | AudioStream,
        on_audio: Callable[[bytes], None] | None = None,
        load: Callable[[], Callable[[bytes], list[Heard]]] | None = None,
        on_ready: Callable[[], None] | None = None,
    ):
        # Модель можна передати готову (transcribe) або завантажити у фоні (load): перший раз це
        # кілька хвилин завантаження, і програма тим часом уже працює.
        self.transcribe = transcribe
        self.load = load
        self.on_ready = on_ready
        self.on_text = on_text
        self.source = source
        self.on_audio = on_audio  # сирий звук — для відеовирізок
        self._stream: AudioStream | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ears", daemon=True)
        self._slow = 0
        self._hearing = False

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._stream is not None:
            self._stream.close()

    def _record(self) -> AudioStream:
        return PulseStream(self.source) if isinstance(self.source, str) else self.source

    def _run(self) -> None:
        while self.transcribe is None and not self._stop.is_set():
            try:
                self.transcribe = self.load()
                log.info("Вуха готові (модель %s)", getattr(self.transcribe, "name", "?"))
            except EarsError as e:
                log.warning("Вуха вимкнено: %s", e)
                return
            except Exception as e:  # noqa: BLE001 — модель не завантажилась (ще немає інтернету тощо)
                log.warning("Вуха: не вдалося завантажити модель розпізнавання (%s) — спробую ще раз за 10 хв", e)
                self._stop.wait(600)
        if self._stop.is_set():
            return
        if self.on_ready:
            self.on_ready()
        chunk_bytes = RATE * 2 * CHUNK_SECONDS
        while not self._stop.is_set():
            self._stream = self._record()
            buffer = b""
            quiet = 0
            while not self._stop.is_set():
                data = self._stream.read(RATE)  # ~0,5 с
                if data is None:
                    break
                if not data:
                    # Звук перестав надходити (пара скінчилась) — дорозпізнати хвіст, щоб він
                    # не злипся з початком наступної пари.
                    quiet += 1
                    if quiet >= 3 and len(buffer) >= RATE * 2 * 2:
                        self._hear(buffer)
                        buffer = b""
                    continue
                if quiet >= 30 or not self._hearing:
                    log.info("👂 Чую пару — звук надходить до вух")
                self._hearing = True
                quiet = 0
                if self.on_audio:
                    self.on_audio(data)
                buffer += data
                if len(buffer) >= chunk_bytes:
                    chunk, buffer = buffer[:chunk_bytes], buffer[chunk_bytes:]
                    self._hear(chunk)
            if not self._stop.is_set():
                log.warning("Запис звуку обірвався — перезапускаю за 5 с")
                self._stop.wait(5)

    def _hear(self, chunk: bytes) -> None:
        if log.isEnabledFor(logging.DEBUG):  # autopara -v: рівень кожного шматка й сирий звук — щоб розібратись, що чують вуха
            log.debug("👂 шматок %.0f с, рівень %d%s", len(chunk) / RATE / 2, loudness(chunk),
                      " (тиша)" if is_silence(chunk) else "")
            with open(DEBUG_AUDIO, "ab") as raw:
                raw.write(chunk)
        if is_silence(chunk):
            return
        started = monotonic()
        try:
            heard = self.transcribe(chunk)
        except Exception as e:  # noqa: BLE001 — одна невдала спроба не має вбивати вуха
            log.warning("Не вдалося розпізнати мову: %s", e)
            return
        self._watch_speed(monotonic() - started)
        for item in heard:
            if text := clean_text(item.text):
                log.debug("👂 %s%s", "" if item.confident else "[?] ", text)
                self.on_text(text, item.confident)

    def _watch_speed(self, seconds: float) -> None:
        """Якщо модель розпізнає повільніше, ніж говорять, — текст відставатиме все більше."""
        self._slow = self._slow + 1 if seconds > CHUNK_SECONDS * 0.8 else 0
        if self._slow < 3:
            return
        downgrade = getattr(self.transcribe, "downgrade", None)
        if downgrade is not None and downgrade():
            self._slow = 0
        elif self._slow == 3:
            log.warning(
                "Розпізнавання не встигає за парою (%.0f с на %d с звуку) — постав меншу модель у [ears] model "
                "(порівняти: autopara listen-test)",
                seconds,
                CHUNK_SECONDS,
            )


def record_process(source: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["parec", f"--device={source}", f"--rate={RATE}", "--channels=1", "--format=s16le", "--raw"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )


def decode_file(path: Path, seconds: float) -> bytes:
    """Перші ``seconds`` секунд звуку з будь-якого аудіо- чи відеофайла (через ffmpeg) — для listen-test."""
    if not path.is_file():
        raise EarsError(f"файл {path} не знайдено")
    from .media import ffmpeg_path

    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise EarsError("щоб читати файли, потрібен ffmpeg")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path), "-t", str(seconds), "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
        capture_output=True,
    )
    if result.returncode != 0:
        raise EarsError(f"ffmpeg не прочитав {path.name}: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def record(source: str, seconds: float) -> bytes:
    """Записати кілька секунд з аудіоджерела (для listen-test)."""
    process = record_process(source)
    try:
        return process.stdout.read(int(RATE * 2 * seconds))
    finally:
        process.terminate()
