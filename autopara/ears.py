"""«Вуха»: слухають пару й перетворюють мову на текст — локально, нічого нікуди не надсилаючи.

Звідки береться звук — два шляхи:
- **з браузера** (будь-яка ОС): сторінка пари сама збирає голоси учасників (ears_tap.js) і віддає
  їх програмі. Працює, навіть коли звук у браузері вимкнений;
- **системний звук Linux** (PipeWire/PulseAudio): віртуальний вихід ``autopara`` (module-null-sink),
  браузер запускаємо з PULSE_SINK=autopara, записуємо його через parec. Якщо mute_sound = false,
  звук ще й дублюється в динаміки (module-loopback).
Далі однаково: шматки по ~10 с → мова в текст (faster-whisper).
"""

from __future__ import annotations

import base64
import logging
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
SOURCES = ("auto", "browser", "pulse")  # звідки брати звук: auto — системний на Linux, інакше з браузера
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
    """"pulse" чи "browser" для налаштування [ears] source."""
    if setting == "auto":
        return "pulse" if pulse_available() is None else "browser"
    return setting


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


class BrowserAudio:
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


def is_silence(pcm: bytes) -> bool:
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return True
    step = max(1, len(samples) // 4000)  # для оцінки гучності вистачить кожного n-го відліку
    picked = samples[::step]
    return sqrt(sum(s * s for s in picked) / len(picked)) < SILENCE_RMS


def clean_text(text: str) -> str:
    text = " ".join(text.split())
    return "" if not text or HALLUCINATIONS.search(text) else text


@dataclass(frozen=True)
class Heard:
    text: str
    confident: bool  # чи сама модель упевнена, що почула саме це


def _letters(text: str) -> str:
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


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
        self._load(model)

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
        options = dict(language=self.language, vad_filter=True, beam_size=1, condition_on_previous_text=False)
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
    """Whisper іноді «вимовляє» саму підказку на тиші. Справжня мова рідко містить разом
    кілька її частин (твоє ім'я, викладача й назву пари), а сам виклик «Ляшко Денис» — лишаємо."""
    letters = _letters(text)
    parts = [_letters(part) for part in hint.split(",")]
    return sum(1 for part in parts if part and part in letters) >= 2


def is_confident(avg_logprob: float, no_speech_prob: float) -> bool:
    """Whisper сам оцінює, наскільки певен. Нижче цих порогів — швидше «почулось», ніж почув."""
    return avg_logprob > CONFIDENT_LOGPROB and no_speech_prob < CONFIDENT_NO_SPEECH


class Ears:
    """Фоновий потік: звук → шматки по ~10 с → текст → on_text(текст, чи впевнено).

    ``source`` — пристрій PulseAudio (рядок, наприклад "autopara.monitor") або готовий потік
    (BrowserAudio).
    """

    def __init__(
        self,
        transcribe: Callable[[bytes], list[Heard]],
        on_text: Callable[[str, bool], None],
        source: str | AudioStream,
        on_audio: Callable[[bytes], None] | None = None,
    ):
        self.transcribe = transcribe
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
    if shutil.which("ffmpeg") is None:
        raise EarsError("щоб читати файли, потрібен ffmpeg")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-t", str(seconds), "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
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
