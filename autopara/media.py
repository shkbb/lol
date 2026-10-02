"""Відеовирізки моментів пари: «тебе кличуть» → відео з кількох секунд до й після, з цитатою.

Браузер безперервно віддає кадри сторінки (CDP screencast, кілька на секунду), а вуха — звук.
Останні ~90 секунд того й іншого тримаємо в пам'яті (кілька мегабайтів). Коли тебе назвали,
чекаємо ще ~15 секунд (що спитали далі?), склеюємо вирізку через ffmpeg і шлемо в Telegram.
Без ffmpeg — останній кадр і цитата.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Callable

log = logging.getLogger(__name__)

FPS = 2.5
BYTES_PER_SECOND = 16_000 * 2  # 16 кГц, моно, 16 біт — те, що пишуть вуха


class FrameBuffer:
    def __init__(self, seconds: float = 90, fps: float = FPS):
        self.seconds = seconds
        self.min_interval = 1 / fps
        self._frames: deque[tuple[float, bytes]] = deque()
        self._lock = threading.Lock()

    def add(self, jpeg: bytes, at: float | None = None) -> None:
        at = monotonic() if at is None else at
        with self._lock:
            if self._frames and at - self._frames[-1][0] < self.min_interval:
                return  # браузер шле частіше, ніж нам треба
            self._frames.append((at, jpeg))
            while self._frames and at - self._frames[0][0] > self.seconds:
                self._frames.popleft()

    def window(self, start: float, end: float) -> list[tuple[float, bytes]]:
        """Кадри між start і end, плюс останній кадр перед start (щоб відео не починалось з порожнечі)."""
        with self._lock:
            before = [frame for frame in self._frames if frame[0] < start][-1:]
            return before + [frame for frame in self._frames if start <= frame[0] <= end]


class AudioBuffer:
    def __init__(self, seconds: float = 90):
        self.seconds = seconds
        self._chunks: deque[tuple[float, bytes]] = deque()  # (коли шматок закінчився, PCM)
        self._lock = threading.Lock()

    def add(self, pcm: bytes, at: float | None = None) -> None:
        at = monotonic() if at is None else at
        with self._lock:
            self._chunks.append((at, pcm))
            while self._chunks and at - self._chunks[0][0] > self.seconds:
                self._chunks.popleft()

    def window(self, start: float, end: float) -> bytes:
        parts = []
        with self._lock:
            for chunk_end, pcm in self._chunks:
                chunk_start = chunk_end - len(pcm) / BYTES_PER_SECOND
                if chunk_end <= start or chunk_start >= end:
                    continue
                first = max(0.0, start - chunk_start)
                last = min(chunk_end, end) - chunk_start
                parts.append(pcm[_offset(first) : _offset(last)])
        return b"".join(parts)


def _offset(seconds: float) -> int:
    position = int(seconds * BYTES_PER_SECOND)
    return position - position % 2


def ffmpeg_path() -> str | None:
    """ffmpeg з системи, а якщо його немає — той, що йде разом з програмою (imageio-ffmpeg)."""
    if found := shutil.which("ffmpeg"):
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError):
        return None


def ffmpeg_available() -> bool:
    return ffmpeg_path() is not None


def build_clip(frames: list[tuple[float, bytes]], pcm: bytes, start: float, end: float, folder: Path) -> Path | None:
    """MP4 з кадрів і звуку (або OGG-голосове, якщо кадрів немає). None — якщо нема з чого або нема ffmpeg."""
    if not ffmpeg_available() or (not frames and not pcm):
        return None
    command = [ffmpeg_path(), "-y", "-loglevel", "error"]
    if frames:
        # Кадри приходять нерівно — розкладаємо їх у рівну сітку FPS, дублюючи, де треба.
        index = 0
        for slot in range(max(1, int((end - start) * FPS))):
            moment = start + slot / FPS
            while index + 1 < len(frames) and frames[index + 1][0] <= moment:
                index += 1
            (folder / f"f{slot:05d}.jpg").write_bytes(frames[index][1])
        command += ["-framerate", str(FPS), "-i", str(folder / "f%05d.jpg")]
    if pcm:
        (folder / "audio.raw").write_bytes(pcm)
        command += ["-f", "s16le", "-ar", "16000", "-ac", "1", "-i", str(folder / "audio.raw")]
    if frames:
        output = folder / "moment.mp4"
        command += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p"]
        command += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart"]
        command += ["-c:a", "aac", "-b:a", "48k", "-shortest"] if pcm else ["-an"]
    else:
        output = folder / "moment.ogg"
        command += ["-c:a", "libopus", "-b:a", "32k"]
    result = subprocess.run([*command, str(output)], capture_output=True, text=True)
    if result.returncode != 0 or not output.exists():
        log.warning("ffmpeg не зміг зібрати вирізку: %s", result.stderr.strip()[-300:])
        return None
    return output


class Clipper:
    """Збирає й надсилає вирізку моменту у фоновому потоці."""

    def __init__(
        self,
        frames: FrameBuffer | None,
        audio: AudioBuffer | None,
        media,
        quote: Callable[[str, datetime, datetime], str],
        before: float = 45,
        after: float = 15,
    ):
        self.frames = frames
        self.audio = audio
        self.media = media  # Notifier
        self.quote = quote
        self.before = before
        self.after = after
        if not ffmpeg_available():
            log.warning("Немає ffmpeg — замість відеовирізок надсилатиму кадр і цитату (перевстанови autopara)")

    def capture(self, key: str, headline: str, when: datetime) -> None:
        threading.Thread(target=self._make, args=(key, headline, when, monotonic()), daemon=True).start()

    def _make(self, key: str, headline: str, when: datetime, moment: float) -> None:
        sleep(self.after)  # що спитали далі?
        quote = self.quote(key, when - timedelta(seconds=60), when + timedelta(seconds=self.after))
        caption = headline + (f"\n\n📝 {quote}" if quote else "")
        start, end = moment - self.before, moment + self.after
        frames = self.frames.window(start, end) if self.frames else []
        pcm = self.audio.window(start, end) if self.audio else b""
        with tempfile.TemporaryDirectory(prefix="autopara-") as folder:
            clip = build_clip(frames, pcm, start, end, Path(folder))
            if clip is not None and clip.suffix == ".mp4":
                self.media.video(clip.read_bytes(), clip.name, caption)
            elif clip is not None:
                self.media.voice(clip.read_bytes(), clip.name, caption)
            elif frames:
                self.media.photo(frames[-1][1], caption)
            elif quote:
                self.media(caption, urgent=True)
