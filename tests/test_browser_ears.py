"""Вуха через браузер: сторінка пари віддає звук учасників програмі — на будь-якій ОС і навіть без звуку."""

import struct

import pytest

pytest.importorskip("playwright")

from autopara.ears import AudioQueue  # noqa: E402
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

# Як справжній Meet: доріжка WebRTC є, але тиха, а голос грає через Web Audio.
MEET_REAL = MEET_LIKE.replace("tone.frequency.value = 440;", "tone.frequency.value = 440; const mute = ctx.createGain(); mute.gain.value = 0;").replace(
    "tone.connect(out);", "tone.connect(mute); mute.connect(out);"
).replace("})();\n</script>", """  const player = new AudioContext();
  const voice = player.createOscillator();
  voice.frequency.value = 440;
  voice.connect(player.destination);
  voice.start();
})();
</script>""")

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


def listen(tmp_path, url, html, enable=True, seconds=5):
    """Відкрити сторінку в браузері програми й повернути, що почули вуха."""
    stream = AudioQueue()
    browser = Browser(tmp_path, mute_sound=True, headless=True, audio_tap=stream.feed_base64)
    try:
        page = browser.new_page()
        page.route(url, lambda route: route.fulfill(body=html, content_type="text/html"))
        page.goto(url)
        if enable:
            # Так робить програма у вкладці, де сама зайшла на пару.
            page.evaluate("() => window.__autoparaTap ? window.__autoparaTap.enable() : false")
        page.wait_for_timeout(seconds * 1000)  # тим часом браузер передає звук
        return stream.read(10**9)
    finally:
        browser.close()


@pytest.mark.parametrize(
    "url, html",
    [("https://meet.google.com/abc-defg-hij", MEET_LIKE), ("https://us02web.zoom.us/wc/123", WEB_AUDIO),
     ("https://meet.google.com/abc-defg-hij", MEET_REAL)],
    ids=["meet-webrtc-audio-element", "zoom-web-audio", "meet-silent-track-web-audio-voice"],
)
def test_meeting_audio_reaches_the_ears_even_when_muted(tmp_path, url, html):
    pcm = listen(tmp_path, url, html)
    assert len(pcm) >= 16_000 * 2 * 3  # щонайменше 3 секунди звуку
    frequency, loudness = pitch_and_loudness(pcm[16_000 * 2 :])
    assert 400 < frequency < 480 and loudness > 5000  # той самий тон, без задвоєння й тиші


def test_youtube_in_the_same_browser_is_not_heard(tmp_path):
    assert listen(tmp_path, "https://www.youtube.com/watch?v=x", WEB_AUDIO, seconds=3) == b""


def test_other_meet_tab_is_not_heard_until_the_app_joins_there(tmp_path):
    assert listen(tmp_path, "https://meet.google.com/xyz-abcd-efg", MEET_LIKE, enable=False, seconds=3) == b""


# Голос (440 Гц) і писк 12 кГц, якого whisper не чує: після перетворення в 16 кГц писк має зникнути,
# а не «накластися» на мову десь біля 4 кГц.
HIGH_PITCH = """<!doctype html><script>
const ctx = new AudioContext();
for (const [frequency, volume] of [[440, 0.3], [12000, 0.3]]) {
  const tone = ctx.createOscillator();
  tone.frequency.value = frequency;
  const gain = ctx.createGain();
  gain.gain.value = volume;
  tone.connect(gain);
  gain.connect(ctx.destination);
  tone.start();
}
</script>"""


def test_tab_sound_is_resampled_without_aliasing(tmp_path):
    import numpy as np

    stream = AudioQueue()
    browser = Browser(tmp_path, mute_sound=True, audio_tap=stream.feed_base64)
    try:
        page = browser.new_page()
        page.route("https://meet.google.com/**", lambda route: route.fulfill(body=HIGH_PITCH, content_type="text/html"))
        page.goto("https://meet.google.com/abc-defg-hij")
        page.evaluate("() => window.__autoparaTap.enable()")
        cdp = page.context.new_cdp_session(page)
        result = cdp.send("Runtime.evaluate", {"expression": "window.__autoparaTap.captureTab(true)",
                                               "awaitPromise": True, "userGesture": True, "returnByValue": True})
        assert result["result"]["value"] == "ок"
        page.wait_for_timeout(4000)
        pcm = stream.read(10**9)
    finally:
        browser.close()
    samples = np.frombuffer(pcm[16_000 * 2:], dtype=np.int16).astype(np.float64)
    assert len(samples) >= 16_000 * 2
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    freqs = np.fft.rfftfreq(len(samples), 1 / 16_000)
    voice = spectrum[(freqs > 400) & (freqs < 480)].max()
    alias = spectrum[(freqs > 3800) & (freqs < 4200)].max()  # 16 − 12 = 4 кГц
    assert voice > 1000 * alias


def test_ears_can_be_repaired_by_capturing_the_tab_again(tmp_path):
    """Сторож вух: давно тиша — розбудити звук сторінки й захопити вкладку наново (звук знову йде)."""
    from datetime import datetime, timedelta

    from autopara.config import JoinSettings
    from autopara.joiner import Joiner
    from autopara.planner import Visit
    from autopara.session import Session

    stream = AudioQueue()
    joiner = Joiner(JoinSettings(mute_sound=True), tmp_path, audio_tap=stream.feed_base64)
    try:
        page = joiner.browser.new_page()
        page.route("https://meet.google.com/**", lambda route: route.fulfill(body=HIGH_PITCH, content_type="text/html"))
        page.goto("https://meet.google.com/abc-defg-hij")
        joiner._enable_ears(page)
        page.wait_for_timeout(1500)
        first = stream.read(10**9)
        now = datetime.now()
        session = Session(Visit("https://meet.google.com/abc-defg-hij", "meet", [], now, now + timedelta(hours=1)),
                          now + timedelta(hours=1), page)
        assert joiner.repair_ears(session)
        page.wait_for_timeout(2500)
        again = stream.read(10**9)
    finally:
        joiner.close()
    assert len(first) > 16_000 and len(again) >= 16_000 * 2 * 2  # після ремонту звук іде далі
