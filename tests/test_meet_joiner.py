"""Логіка входу в Meet на фейковій сторінці, що імітує розмітку Meet, у справжньому Chromium."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("playwright")

from autopara.config import JoinSettings  # noqa: E402
from autopara.joiner import LOGIN_HINT, Joiner  # noqa: E402
from autopara.leaving import Observation  # noqa: E402
from autopara.planner import Visit  # noqa: E402
from autopara.session import JoinError  # noqa: E402

from .conftest import make_lesson  # noqa: E402

FAKE_MEET = """<!doctype html>
<html><body>
<div id="prejoin">
  <input id="name" type="text" aria-label="Your name" hidden>
  <div id="mic" role="button" tabindex="0" data-is-muted="false" aria-label="Turn off microphone (ctrl + d)">🎤</div>
  <div id="cam" role="button" tabindex="0" data-is-muted="false" aria-label="Turn off camera (ctrl + e)">📷</div>
  <button id="join"><span>Join now</span></button>
</div>
<div id="lobby" hidden>Asking to be let in…</div>
<div id="denied" hidden>You can't join this video call</div>
<div id="call" hidden>
  <div id="mic2" role="button" tabindex="0" data-is-muted="true" aria-label="Turn on microphone (ctrl + d)">🎤</div>
  <button aria-label="People"><i>people</i><div class="badge">7</div></button>
  <button aria-label="Leave call"><i>call_end</i></button>
</div>
<div id="ended" hidden>You left the meeting <button>Rejoin</button><button>Return to home screen</button></div>
<script>
const scenario = location.pathname.slice(1);
const $ = (id) => document.getElementById(id);
if (scenario === "login") location.replace("https://accounts.google.com/signin");

function setState(btn, what, muted) {
  btn.dataset.isMuted = String(muted);
  const key = what === "microphone" ? "d" : "e";
  btn.setAttribute("aria-label", `${muted ? "Turn on" : "Turn off"} ${what} (ctrl + ${key})`);
}
function toggle(btn, what) {
  if (scenario === "broken") return;  // інтерфейс «змінився», кліки нічого не роблять
  setState(btn, what, btn.dataset.isMuted !== "true");
}
$("mic").onclick = () => toggle($("mic"), "microphone");
$("cam").onclick = () => toggle($("cam"), "camera");
$("mic2").onclick = () => toggle($("mic2"), "microphone");

if (scenario === "late-camera") {
  // Meet вмикає камеру не одразу, а коли браузер її віддасть.
  setState($("cam"), "camera", true);
  setTimeout(() => setState($("cam"), "camera", false), 800);
}
if (scenario === "guest") {
  $("name").hidden = false;
  $("join").firstChild.textContent = "Ask to join";
}

window.micPermission = "unknown";
navigator.permissions.query({name: "microphone"}).then((p) => {
  window.micPermission = p.state;
  if (p.state === "denied") {
    $("mic").dataset.isMuted = "true";
    $("mic").setAttribute("aria-label", "Microphone problem. Show more info");
  }
});

window.endCall = () => { $("call").hidden = true; $("ended").hidden = false; };
$("join").onclick = () => {
  window.joinedWith = {
    mic: $("mic").dataset.isMuted, cam: $("cam").dataset.isMuted,
    perm: window.micPermission, name: $("name").value,
  };
  $("mic2").dataset.isMuted = $("mic").dataset.isMuted;
  $("prejoin").hidden = true;
  if (scenario === "denied") { $("denied").hidden = false; return; }
  if (scenario === "lobby") {
    $("lobby").hidden = false;
    setTimeout(() => { $("lobby").hidden = true; $("call").hidden = false; }, 2500);
    return;
  }
  $("call").hidden = false;
};
</script>
</body></html>"""

TZ = ZoneInfo("Europe/Kyiv")


@pytest.fixture(scope="module")
def joiner(tmp_path_factory):
    settings = JoinSettings(browser_channel="", display_name="Тест Тестович")
    joiner = Joiner(settings, tmp_path_factory.mktemp("profile"), headless=True)
    context = joiner.browser.context()
    context.route("https://meet.google.com/**", lambda route: route.fulfill(body=FAKE_MEET, content_type="text/html"))
    context.route("https://accounts.google.com/**", lambda route: route.fulfill(body="Sign in", content_type="text/html"))
    yield joiner
    joiner.close()


def visit(scenario):
    now = datetime.now(TZ)
    return Visit(f"https://meet.google.com/{scenario}", "meet", [make_lesson()], now, now + timedelta(minutes=5))


def join(joiner, scenario):
    session = joiner.join(visit(scenario))
    state = session.page.evaluate("window.joinedWith")
    joiner.leave(session)
    return state


def test_mic_and_camera_are_turned_off_before_joining(joiner):
    assert join(joiner, "normal") == {"mic": "true", "cam": "true", "perm": "granted", "name": ""}


def test_camera_that_turns_on_late_is_caught(joiner):
    assert join(joiner, "late-camera")["cam"] == "true"


def test_guest_name_is_filled(joiner):
    state = join(joiner, "guest")
    assert (state["name"], state["mic"]) == ("Тест Тестович", "true")


def test_waits_in_lobby(joiner):
    assert join(joiner, "lobby")["mic"] == "true"


def test_unverifiable_mic_falls_back_to_blocked_devices(joiner):
    # Кнопки не реагують — не ризикуємо: браузер забороняє Meet доступ до мікрофона.
    assert join(joiner, "broken")["perm"] == "denied"
    # Наступна пара знову починається з нормальних дозволів.
    assert join(joiner, "normal")["perm"] == "granted"


def test_denied_meeting_is_not_retried(joiner):
    with pytest.raises(JoinError) as error:
        joiner.join(visit("denied"))
    assert not error.value.retry


def test_login_required(joiner):
    with pytest.raises(JoinError, match="autopara login") as error:
        joiner.join(visit("login"))
    assert not error.value.retry
    assert LOGIN_HINT in str(error.value)


def test_observe_counts_people_and_sees_the_end(joiner):
    session = joiner.join(visit("normal"))
    try:
        assert joiner.observe(session) == Observation(participants=7)
        session.page.click("#mic2")  # ти сам увімкнув мікрофон
        assert joiner.observe(session).mic_on
        session.page.evaluate("endCall()")
        assert joiner.observe(session) == Observation(ended=True)
    finally:
        joiner.leave(session)
