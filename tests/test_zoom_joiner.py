"""Zoom: клієнт через zoommtg:// і веб-клієнт (у iframe, як на app.zoom.us) на фейковій сторінці."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("playwright")

from autopara.config import JoinSettings  # noqa: E402
from autopara.joiner import Joiner  # noqa: E402
from autopara.planner import Visit  # noqa: E402

from .conftest import make_lesson  # noqa: E402

TZ = ZoneInfo("Europe/Kyiv")
ZOOM = "https://us05web.zoom.us/j/12345678901?pwd=AbC"

FRAME = """<!doctype html><body>
<input id="input-for-name" placeholder="Your Name">
<button id="preview-audio-control-button" aria-label="Mute">mic</button>
<button id="preview-video-control-button" aria-label="Stop Video">cam</button>
<button class="preview-join-button">Join</button>
<div id="call" hidden><button class="footer__leave-btn">Leave</button></div>
<script>
const $ = (id) => document.getElementById(id);
$("preview-audio-control-button").onclick = (e) =>
  e.target.setAttribute("aria-label", e.target.getAttribute("aria-label") === "Mute" ? "Unmute" : "Mute");
$("preview-video-control-button").onclick = (e) =>
  e.target.setAttribute("aria-label", e.target.getAttribute("aria-label") === "Stop Video" ? "Start Video" : "Stop Video");
document.querySelector(".preview-join-button").onclick = () => {
  top.joinedWith = {
    mic: $("preview-audio-control-button").getAttribute("aria-label"),
    cam: $("preview-video-control-button").getAttribute("aria-label"),
    name: $("input-for-name").value,
  };
  $("call").hidden = false;
};
</script></body>"""


def visit(url):
    now = datetime.now(TZ)
    return Visit(url, "zoom", [make_lesson()], now, now + timedelta(minutes=5))


def test_zoom_app_mode_opens_client_directly(tmp_path):
    opened = []
    joiner = Joiner(JoinSettings(zoom_mode="app"), tmp_path, opener=opened.append)
    session = joiner.join(visit(ZOOM))
    assert opened == ["zoommtg://us05web.zoom.us/join?action=join&confno=12345678901&pwd=AbC"]
    assert session.page is None
    joiner.leave(session)  # нічого не робить і не падає


def test_zoom_web_mode_mutes_inside_iframe(tmp_path):
    settings = JoinSettings(zoom_mode="browser", browser_channel="", display_name="Тест Тестович")
    joiner = Joiner(settings, tmp_path, headless=True)
    try:
        context = joiner.browser.context()
        context.route(
            "https://app.zoom.us/wc/join/**",
            lambda route: route.fulfill(body='<iframe id="webclient" src="/frame"></iframe>', content_type="text/html"),
        )
        context.route("https://app.zoom.us/frame", lambda route: route.fulfill(body=FRAME, content_type="text/html"))
        session = joiner.join(visit(ZOOM))
        assert session.page.url == "https://app.zoom.us/wc/join/12345678901?pwd=AbC"
        assert session.page.evaluate("window.joinedWith") == {
            "mic": "Unmute",
            "cam": "Start Video",
            "name": "Тест Тестович",
        }
    finally:
        joiner.close()
