import shutil
import struct
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from autopara import media
from autopara.media import AudioBuffer, Clipper, FrameBuffer, build_clip

TZ = ZoneInfo("Europe/Kyiv")


def jpeg(color):
    """Справжній маленький JPEG — щоб ffmpeg мав що кодувати."""
    if shutil.which("ffmpeg") is None:
        return b"\xff\xd8fake"
    result = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s=320x200", "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
        capture_output=True, check=True,
    )
    return result.stdout


def tone(seconds):
    return struct.pack(f"<{int(16000 * seconds)}h", *([3000, -3000] * int(8000 * seconds)))


def test_frame_buffer_thins_and_forgets():
    frames = FrameBuffer(seconds=10, fps=2)
    for i in range(100):
        frames.add(b"f%d" % i, at=i * 0.1)  # 10 кадрів/с → лишаємо 2/с
    window = frames.window(5.0, 9.9)
    assert [t for t, _ in window] == [4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.5, 9.0, 9.5]
    frames.add(b"late", at=30)
    assert frames.window(0, 30) == [(30, b"late")]


def test_audio_buffer_cuts_exact_window():
    audio = AudioBuffer()
    for second in range(1, 11):
        audio.add(bytes([second]) * media.BYTES_PER_SECOND, at=float(second))  # шматок [s-1, s]
    pcm = audio.window(2.5, 4.5)
    assert len(pcm) == 2 * media.BYTES_PER_SECOND
    assert set(pcm[: media.BYTES_PER_SECOND // 2]) == {3} and set(pcm[-media.BYTES_PER_SECOND // 2 :]) == {5}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="немає ffmpeg")
def test_clip_with_video_and_sound(tmp_path):
    frames = [(0.0, jpeg("red")), (2.0, jpeg("blue")), (4.0, jpeg("green"))]
    clip = build_clip(frames, tone(6), 0.0, 6.0, tmp_path)
    assert clip is not None and clip.suffix == ".mp4"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(clip)],
        capture_output=True, text=True,
    ).stdout.split()
    assert sorted(probe) == ["audio", "video"]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="немає ffmpeg")
def test_voice_when_no_frames(tmp_path):
    clip = build_clip([], tone(3), 0.0, 3.0, tmp_path)
    assert clip is not None and clip.suffix == ".ogg"


def test_frame_and_quote_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    sent = []

    class Media:
        def photo(self, content, caption):
            sent.append(("photo", content, caption))

    frames = FrameBuffer()
    frames.add(b"jpeg-bytes")
    monkeypatch.setattr(media, "sleep", lambda seconds: None)
    clipper = Clipper(frames, None, Media(), lambda key, start, end: "Ляшко, ви тут? Відповідайте в чат.")
    clipper._make("pair", "🎬 Момент з «Бази даних», 10:12", datetime(2026, 10, 5, 10, 12, tzinfo=TZ), media.monotonic())
    [(kind, content, caption)] = sent
    assert kind == "photo" and content == b"jpeg-bytes"
    assert caption == "🎬 Момент з «Бази даних», 10:12\n\n📝 Ляшко, ви тут? Відповідайте в чат."


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="немає ffmpeg")
def test_clipper_sends_video(monkeypatch):
    sent = []

    class Media:
        def video(self, content, filename, caption):
            sent.append((filename, len(content), caption))

    now = media.monotonic()
    frames, audio = FrameBuffer(), AudioBuffer()
    for i in range(10):
        frames.add(jpeg("red" if i % 2 else "blue"), at=now - 5 + i * 0.5)
    audio.add(tone(5), at=now)
    monkeypatch.setattr(media, "sleep", lambda seconds: None)
    clipper = Clipper(frames, audio, Media(), lambda key, start, end: "", before=5, after=0)
    clipper._make("pair", "🎬 Момент", datetime.now(TZ), now)
    [(filename, size, caption)] = sent
    assert filename == "moment.mp4" and size > 1000 and caption == "🎬 Момент"
