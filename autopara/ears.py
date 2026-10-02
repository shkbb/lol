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
from dataclasses import dataclass
from math import sqrt
from time import monotonic
from typing import Callable

log = logging.getLogger(__name__)

SINK = "autopara"
RATE = 16_000  # Гц, моно, 16 біт — те, що їсть whisper
CHUNK_SECONDS = 10
SILENCE_RMS = 150
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


def ears_available() -> str | None:
    """None, якщо можна слухати, інакше — чого бракує."""
    if not sys.platform.startswith("linux"):
        return "слухати пару поки вміє лише на Linux (решта працює й без вух)"
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
    """Фоновий потік: запис → шматки по ~10 с → текст → on_text(текст, чи впевнено)."""

    def __init__(
        self,
        transcribe: Callable[[bytes], list[Heard]],
        on_text: Callable[[str, bool], None],
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
        self._slow = 0

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _record(self) -> subprocess.Popen:
        return record_process(self.source)

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


def record(source: str, seconds: float) -> bytes:
    """Записати кілька секунд з аудіоджерела (для listen-test)."""
    process = record_process(source)
    try:
        return process.stdout.read(int(RATE * 2 * seconds))
    finally:
        process.terminate()
