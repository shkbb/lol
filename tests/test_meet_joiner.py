"""Логіка входу в Meet на фейковій сторінці, що імітує розмітку Meet, у справжньому Chromium."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("playwright")

from autopara.config import JoinSettings  # noqa: E402
from autopara.ears import AudioQueue  # noqa: E402
from autopara.assistant import ChatMessage  # noqa: E402
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
  <button id="chatbtn" aria-label="Chat with everyone"><i>chat</i></button>
  <button aria-label="Leave call"><i>call_end</i></button>
  <div id="chatpanel" hidden>
    <div id="messages">
      <div><div>Іван Коваль <span>10:23</span></div>
        <div><div data-message-id="m1">Поставте плюс</div><div data-message-id="m2">+</div></div></div>
      <div><div>Олена Бойко 10:24</div><div><div data-message-id="m3">+</div></div></div>
    </div>
    <textarea id="chatbox" aria-label="Send a message"></textarea>
  </div>
</div>
<div id="ended" hidden>You left the meeting <button>Rejoin</button><button>Return to home screen</button></div>
<div id="cover" hidden style="position:fixed;inset:0;background:#0003"></div>
<div id="tip" hidden style="position:fixed;top:30%;left:30%;z-index:10;background:#fff">Users may see your video differently
  <button id="gotit">Got it</button></div>
<script>
const scenario = location.pathname.slice(1);
const $ = (id) => document.getElementById(id);
if (scenario === "login") location.replace("https://accounts.google.com/signin");
if (scenario === "ask-uk") $("join").innerHTML = "<span>Надіслати запит на приєднання</span>";  // так Meet українською
if (scenario === "renamed") $("join").innerHTML = "<span>Увійти до зустрічі</span>";  // назва, якої програма не знає

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

$("chatbtn").onclick = () => { $("chatpanel").hidden = !$("chatpanel").hidden; };
// Як справжній Meet: Ctrl+Alt+C відкриває чат; у вузькому вікні кнопки чату не видно, а поверх буває підказка.
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.altKey && e.key.toLowerCase() === "c") $("chatpanel").hidden = false;
});
$("gotit").onclick = () => { $("tip").hidden = true; $("cover").hidden = true; };
let sent = 0;
$("chatbox").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  e.preventDefault();
  const group = document.createElement("div");
  group.innerHTML = `<div>You 10:30</div><div><div data-message-id="me${++sent}"></div></div>`;
  group.querySelector("[data-message-id]").textContent = $("chatbox").value;
  $("messages").appendChild(group);
  $("chatbox").value = "";
});
let incomingId = 0;
window.incoming = (author, text) => {
  const group = document.createElement("div");
  group.innerHTML = `<div></div><div><div data-message-id="in${++incomingId}"></div></div>`;
  group.firstElementChild.textContent = `${author} 10:31`;
  group.querySelector("[data-message-id]").textContent = text;
  $("messages").appendChild(group);
};
window.endCall = () => { $("call").hidden = true; $("ended").hidden = false; };
$("join").onclick = () => {
  window.joinedWith = {
    mic: $("mic").dataset.isMuted, cam: $("cam").dataset.isMuted,
    perm: window.micPermission, name: $("name").value,
  };
  $("mic2").dataset.isMuted = $("mic").dataset.isMuted;
  $("prejoin").hidden = true;
  const account = new URLSearchParams(location.search).get("authuser") || "";
  if (scenario === "denied" || (scenario === "only-gmail" && !account.endsWith("@gmail.com"))) {
    $("denied").hidden = false;
    return;
  }
  if (scenario === "tip") {
    $("chatbtn").hidden = true;
    $("tip").hidden = false;
    $("cover").hidden = false;
  }
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


@pytest.fixture(scope="module", params=[False, True], ids=["plain", "with-ears"])
def joiner(tmp_path_factory, request):
    """Двічі: звичайний браузер і з вухами через браузер — збирач звуку не має заважати входу."""
    settings = JoinSettings(display_name="Тест Тестович")
    tap = AudioQueue().feed_base64 if request.param else None
    joiner = Joiner(settings, tmp_path_factory.mktemp("profile"), headless=True, audio_tap=tap)
    joiner.browser.close_if_idle = lambda: None  # імітація Meet живе в цьому вікні — не закривати після кожної «пари»
    context = joiner.browser.context()
    context.route("https://meet.google.com/**", lambda route: route.fulfill(body=FAKE_MEET, content_type="text/html; charset=utf-8"))
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


def test_ukrainian_ask_to_join_button(joiner):
    """Зустріч, куди треба просити впустити: українською кнопка зветься «Надіслати запит на приєднання»."""
    state = join(joiner, "ask-uk")
    assert (state["mic"], state["cam"]) == ("true", "true")


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
    with pytest.raises(JoinError, match="Увійти в Google") as error:
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


def test_chat_is_read_with_authors_and_written(joiner):
    session = joiner.join(visit("normal"))
    try:
        assert joiner.read_chat(session) == [
            ChatMessage("m1", "Іван Коваль", "Поставте плюс"),
            ChatMessage("m2", "Іван Коваль", "+"),
            ChatMessage("m3", "Олена Бойко", "+"),
        ]
        assert joiner.send_chat(session, "+")
        # Чат уже відкритий — повторне «відкриття» не має його закрити.
        assert joiner.read_chat(session)[-1] == ChatMessage("me1", "You", "+")
    finally:
        joiner.leave(session)


def test_chat_behind_a_tip_and_without_a_chat_button(joiner):
    """Підказка «Зрозуміло» поверх зустрічі й вузьке вікно без кнопки чату — однаково пишемо в чат."""
    session = joiner.join(visit("tip"))
    try:
        joiner.observe(session)  # під час пари підказки закриваються самі
        assert session.page.locator("#tip").is_hidden()
        assert joiner.send_chat(session, "Так, я тут")
        assert joiner.read_chat(session)[-1] == ChatMessage("me1", "You", "Так, я тут")
    finally:
        joiner.leave(session)


def test_whole_chain_group_plus_is_mirrored(joiner):
    from autopara.assistant import Assistant
    from autopara.config import Config, MeSettings, ScheduleSettings
    from autopara.planner import Plan
    from autopara.runner import Runner

    class NoGuard:
        def hold(self, needed):
            pass

        def release(self):
            pass

    inbox = []
    assistant = Assistant(MeSettings(name="Ляшко Денис"), lambda text, urgent=False: inbox.append(text))
    runner = Runner(Config(schedule=ScheduleSettings(group="ТЦР-32")), None, joiner, sleep_guard=NoGuard(), assistant=assistant)
    now = datetime.now(TZ)
    meeting = Visit("https://meet.google.com/normal", "meet", [make_lesson()], now, now + timedelta(minutes=30))
    runner.plan = Plan(visits=[meeting])
    runner.next_refresh = meeting.leave_at
    runner.tick()
    page = runner.active[meeting.key].page
    try:
        page.evaluate("([a, t]) => incoming(a, t)", ["Петренко П.П.", "Відмітьтесь плюсом, хто на парі"])
        for author in ("Іван Коваль", "Олена Бойко", "Марко Лис"):
            page.evaluate("([a, t]) => incoming(a, t)", [author, "+"])
        runner.tick()
        assert joiner.read_chat(runner.active[meeting.key])[-1] == ChatMessage("me1", "You", "+")
        assert inbox[-1].startswith("💬 Написав «+» у чат")
    finally:
        joiner.leave(runner.active[meeting.key])


def test_frames_flow_while_idle(joiner):
    from autopara.media import FrameBuffer

    joiner.frames = FrameBuffer()  # у програмі буфер передається при створенні Joiner
    try:
        session = joiner.join(visit("normal"))
        session.page.evaluate("setInterval(() => { document.body.style.background = `hsl(${Date.now() % 360},50%,50%)`; }, 50)")
        joiner.idle(3)
        got = joiner.frames.window(0, float("inf"))
        assert len(got) >= 3 and all(jpeg.startswith(b"\xff\xd8") for _, jpeg in got)
        joiner.leave(session)
    finally:
        joiner.frames = None
