"""«Вуха»: слухають пару й перетворюють мову на текст — локально, нічого нікуди не надсилаючи.

Як це працює (Linux + PipeWire/PulseAudio):
1. створюємо віртуальний «нульовий» аудіовихід ``autopara`` (pactl load-module module-null-sink);
2. браузер запускаємо з PULSE_SINK=autopara — звук пари йде туди; якщо mute_sound = false,
   він ще й дублюється в динаміки (module-loopback), тож чуєш і ти;
3. записуємо цей вихід (parec) і шматками по ~10 с розпізнаємо мову (faster-whisper).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
from array import array
from math import sqrt
from typing import Callable

log = logging.getLogger(__name__)

SINK = "autopara"
RATE = 16_000  # Гц, моно, 16 біт — те, що їсть whisper
CHUNK_SECONDS = 10
SILENCE_RMS = 150

# Whisper на тиші чи музиці любить «галюцинувати» ці фрази.
HALLUCINATIONS = re.compile(
    r"дякую за перегляд|субтитр|продовження в наступн|підписуйтесь|amara\.org|"
    r"редактор субтитрів|спасибо за просмотр|thanks for watching",
    re.I,
)


class EarsError(Exception):
    pass


def ears_available() -> str | None:
    """None, якщо можна слухати, інакше — чого бракує."""
    if not sys.platform.startswith("linux"):
        return "слухати пару вміє лише на Linux"
    missing = [tool for tool in ("pactl", "parec") if shutil.which(tool) is None]
    if missing:
        return f"бракує {', '.join(missing)} — встанови пакет libpulse (працює і з PipeWire)"
    return None


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


class WhisperTranscriber:
    def __init__(self, model: str = "small", language: str = "uk"):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise EarsError('немає faster-whisper — встанови: pip install -e ".[ears]"') from e
        log.info("Завантажую модель розпізнавання мови %r (перший раз — кілька хвилин)...", model)
        self.model = WhisperModel(model, device="cpu", compute_type="int8")
        self.language = language

    def __call__(self, pcm: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio, language=self.language, vad_filter=True, beam_size=1, condition_on_previous_text=False
        )
        return " ".join(segment.text.strip() for segment in segments)


class Ears:
    """Фоновий потік: запис → шматки по ~10 с → текст → on_text(текст)."""

    def __init__(
        self,
        transcribe: Callable[[bytes], str],
        on_text: Callable[[str], None],
        source: str,
        on_audio: Callable[[bytes], None] | None = None,
    ):
        self.transcribe = transcribe
        self.on_text = on_text
        self.source = source
        self.on_audio = on_audio  # сирий звук — для відеовирізок
        self._process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ears", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _record(self) -> subprocess.Popen:
        return subprocess.Popen(
            ["parec", f"--device={self.source}", f"--rate={RATE}", "--channels=1", "--format=s16le", "--raw"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _run(self) -> None:
        chunk_bytes = RATE * 2 * CHUNK_SECONDS
        while not self._stop.is_set():
            self._process = self._record()
            buffer = b""
            while not self._stop.is_set():
                data = self._process.stdout.read(RATE)  # ~0,5 с
                if not data:
                    break
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
        try:
            text = clean_text(self.transcribe(chunk))
        except Exception as e:  # noqa: BLE001 — одна невдала спроба не має вбивати вуха
            log.warning("Не вдалося розпізнати мову: %s", e)
            return
        if text:
            log.debug("👂 %s", text)
            self.on_text(text)
