"""Вуха через браузер: сторінка пари віддає звук учасників програмі — на будь-якій ОС і навіть без звуку."""

import struct

import pytest

pytest.importorskip("playwright")

from autopara.ears import BrowserAudio  # noqa: E402
from autopara.joiner import Browser  # noqa: E402

# Як у Meet: голос приходить доріжкою WebRTC і грає через <audio>.
MEET_LIKE = """<!doctype html><meta charset="utf-8"><audio id="voice" autoplay></audio><script>
(async () => {
  const ctx = new AudioContext();
  const tone = ctx.createOscillator();
  tone.frequency.value = 440;
  const out = ctx.createMediaStreamDestination();
  tone.connect(out);
  tone.start();
  const sender = new RTCPeerConnection(), receiver = new RTCPeerConnection();
  sender.onicecandidate = (e) => e.candidate && receiver.addIceCandidate(e.candidate);
  receiver.onicecandidate = (e) => e.candidate && sender.addIceCandidate(e.candidate);
  receiver.ontrack = (e) => { const el = document.getElementById("voice"); el.srcObject = e.streams[0]; el.play(); };
  out.stream.getTracks().forEach((track) => sender.addTrack(track, out.stream));
  const offer = await sender.createOffer();
  await sender.setLocalDescription(offer);
  await receiver.setRemoteDescription(offer);
  const answer = await receiver.createAnswer();
  await receiver.setLocalDescription(answer);
  await sender.setRemoteDescription(answer);
})();
</script>"""

# Як у Zoom у браузері: звук грає через Web Audio, без <audio>.
WEB_AUDIO = """<!doctype html><script>
const ctx = new AudioContext();
const tone = ctx.createOscillator();
tone.frequency.value = 440;
const volume = ctx.createGain();
tone.connect(volume);
volume.connect(ctx.destination);
tone.start();
</script>"""


def pitch_and_loudness(pcm: bytes) -> tuple[float, float]:
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / 16_000), rms


@pytest.mark.parametrize("html", [MEET_LIKE, WEB_AUDIO], ids=["webrtc-audio-element", "web-audio"])
def test_meeting_audio_reaches_the_ears_even_when_muted(tmp_path, html):
    stream = BrowserAudio()
    browser = Browser(tmp_path, channel="", mute_sound=True, headless=True, audio_tap=stream.feed_base64)
    try:
        page = browser.new_page()
        page.route("https://meet.test/**", lambda route: route.fulfill(body=html, content_type="text/html"))
        page.goto("https://meet.test/abc-defg-hij")
        page.wait_for_timeout(5000)  # тим часом браузер передає звук
        pcm = stream.read(10**9)
    finally:
        browser.close()
    assert len(pcm) >= 16_000 * 2 * 3  # щонайменше 3 секунди звуку
    frequency, loudness = pitch_and_loudness(pcm[16_000 * 2 :])
    assert 400 < frequency < 480 and loudness > 5000  # той самий тон, без задвоєння й тиші
