"""Заходить у Google Meet / Zoom з вимкненими мікрофоном і камерою.

Головне правило безпеки: якщо не вдалося *переконатися*, що мікрофон вимкнено,
ми забороняємо сторінці доступ до мікрофона й камери на рівні браузера
і заходимо вже так. Інтерфейс Meet/Zoom змінюється часто, а «гарячий» мікрофон
на парі — найгірший можливий результат.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Callable

from playwright.sync_api import BrowserContext, Locator, Page, sync_playwright
from playwright.sync_api import Error as PlaywrightError

from .assistant import ChatMessage
from .config import JoinSettings
from .ears import TAP_SCRIPT
from .leaving import Observation
from .links import meet_url, zoom_app_url, zoom_web_url
from .planner import Visit
from .session import JoinError, Session

log = logging.getLogger(__name__)

MEET_ORIGIN = "https://meet.google.com"
ZOOM_ORIGIN = "https://app.zoom.us"
LOGIN_HINT = "потрібно увійти в Google-акаунт: запусти `autopara login`"

# Інтерфейс Meet/Zoom буває англійською, українською чи російською — шукаємо всіма трьома.
JOIN_RE = re.compile(
    r"^\s*(join now|join|ask to join|приєднатися( зараз)?|попросити\b.*приєдна\w*"
    r"|присоединиться( сейчас)?|попросить\b.*присоедин\w*)\s*$",
    re.I,
)
CONTINUE_WITHOUT_RE = re.compile(r"continue without|продовжити без|продолжить без", re.I)
DISMISS_RE = re.compile(r"^\s*(got it|dismiss|зрозуміло|закрити|понятно|закрыть)\s*$", re.I)
NAME_RE = re.compile(r"name|ім.я|имя", re.I)
LEAVE_RE = re.compile(r"leave|покинути|вийти з|покинуть|выйти из", re.I)
DENIED_RE = re.compile(
    r"can.?t join this|cannot join this|check your meeting code|declined|removed from the meeting"
    r"|не вдається приєднатися|не можете приєднатися|відхилено|перевірте код"
    r"|не удается присоединиться|отклонен|проверьте код",
    re.I,
)
NOT_STARTED_RE = re.compile(r"hasn.?t started|not started yet|ще не почал|ещ[её] не начал", re.I)
PASSCODE_RE = re.compile(r"passcode|код доступу|пароль", re.I)

# Кнопка «Учасники» в Meet, на якій висить значок з їхньою кількістю.
PEOPLE_RE = re.compile(r"people|everyone|participant|учасник|люди|всіх|усіх|всех|участник", re.I)
# Кнопки екрана, який Meet показує після кінця зустрічі (або коли тебе з неї видалили).
MEET_ENDED_RE = re.compile(
    r"^\s*(return to home screen|rejoin|повернутися на головний екран|приєднатися знову|повторно приєднатися"
    r"|вернуться на главный экран|присоединиться (снова|ещ[её] раз))\s*$",
    re.I,
)
ZOOM_ENDED_RE = re.compile(
    r"ended by (the )?host|meeting has (been )?ended|завершив організатор|зустріч завершено|конференцію завершено"
    r"|встреча завершена|конференция завершена",
    re.I,
)
ZOOM_MIC_RE = re.compile(r"micro|мікрофон|микрофон|\b(un)?mute\b|звук", re.I)
ZOOM_CAM_RE = re.compile(r"camera|video|камер|відео|видео", re.I)
# Підпис кнопки описує дію, а не стан: «Увімкнути мікрофон» означає, що зараз він вимкнений.
CURRENTLY_OFF_RE = re.compile(r"turn on|unmute|start video|увімкн|включ|запуст", re.I)
CURRENTLY_ON_RE = re.compile(r"turn off|\bmute\b|stop video|вимкн|выключ|отключ|зупин|останов", re.I)


# --- Дрібні помічники Playwright ---------------------------------------------


def _first_visible(locator: Locator, accept: Callable[[Locator], bool] | None = None) -> Locator | None:
    try:
        count = min(locator.count(), 20)
        for index in range(count):
            item = locator.nth(index)
            if item.is_visible() and (accept is None or accept(item)):
                return item
    except PlaywrightError:
        pass
    return None


def _find(page: Page, make: Callable, accept: Callable[[Locator], bool] | None = None) -> Locator | None:
    """Шукає видимий елемент на сторінці та в усіх її фреймах (Zoom живе в iframe)."""
    for frame in page.frames:
        if found := _first_visible(make(frame), accept):
            return found
    return None


def _visible_text(page: Page, pattern: re.Pattern) -> bool:
    return _find(page, lambda f: f.get_by_text(pattern)) is not None


def _label(item: Locator) -> str:
    try:
        parts = [item.get_attribute("aria-label"), item.get_attribute("data-tooltip")]
        return " ".join(p for p in parts if p) or item.inner_text(timeout=1000)
    except PlaywrightError:
        return ""


def _is_off(item: Locator) -> bool | None:
    """True — пристрій вимкнено, False — увімкнено, None — не зрозуміло."""
    try:
        muted = item.get_attribute("data-is-muted")
    except PlaywrightError:
        return None
    if muted in ("true", "false"):
        return muted == "true"
    label = _label(item)
    if CURRENTLY_OFF_RE.search(label):
        return True
    if CURRENTLY_ON_RE.search(label):
        return False
    return None


def _has_state(item: Locator) -> bool:
    return _is_off(item) is not None


def _ensure_off(page: Page, find_toggle: Callable[[Page], Locator | None]) -> bool:
    for _ in range(3):
        toggle = find_toggle(page)
        if toggle is None:
            return False
        state = _is_off(toggle)
        if state is True:
            return True
        if state is None:
            return False
        try:
            toggle.click(timeout=3000)
        except PlaywrightError:
            return False
        page.wait_for_timeout(700)
    toggle = find_toggle(page)
    return toggle is not None and _is_off(toggle) is True


def _devices_off(page: Page, settings: JoinSettings, mic, cam) -> bool:
    """Вимикає й перевіряє пристрої двічі з паузою: Meet вмикає камеру асинхронно після завантаження."""
    for _ in range(2):
        if settings.mute_microphone and not _ensure_off(page, mic):
            return False
        if settings.camera_off and not _ensure_off(page, cam):
            return False
        page.wait_for_timeout(1500)
    return True


def _block_media(context: BrowserContext, page: Page, origin: str) -> None:
    """Забороняє сайту мікрофон і камеру — після перезавантаження він фізично не зможе їх увімкнути.

    Заборона діє, поки живе CDP-сесія, тобто поки відкрита вкладка: тому сесію не від'єднуємо.
    Закрили вкладку — Chromium скасовує заборону, і наступна пара починається з чистого аркуша.
    """
    try:
        cdp = context.new_cdp_session(page)
        for name in ("microphone", "camera"):
            cdp.send(
                "Browser.setPermission",
                {"origin": origin, "permission": {"name": name}, "setting": "denied"},
            )
    except PlaywrightError as e:
        log.warning("Не вдалося заборонити доступ через CDP (%s), просто скидаю дозволи", e)
        context.clear_permissions()


def _now(deadline: datetime) -> datetime:
    return datetime.now(deadline.tzinfo)


def _wait_in_call(page: Page, deadline: datetime, in_call: Callable[[Page], bool]) -> None:
    waiting_logged = False
    while _now(deadline) < deadline:
        if in_call(page):
            return
        if _visible_text(page, DENIED_RE):
            raise JoinError("організатор не впустив або зустріч недоступна", retry=False)
        if not waiting_logged:
            log.info("Чекаю, поки впустять у зустріч...")
            waiting_logged = True
        page.wait_for_timeout(2000)
    raise JoinError("так і не впустили до кінця пари", retry=False)


# --- Google Meet -------------------------------------------------------------


def _meet_device(device_re: re.Pattern) -> Callable[[Page], Locator | None]:
    def find(page: Page) -> Locator | None:
        def matches(item: Locator) -> bool:
            return bool(device_re.search(_label(item)))

        return _first_visible(page.locator("[data-is-muted]"), matches) or _first_visible(
            page.get_by_role("button", name=device_re), _has_state
        )

    return find


MEET_MIC = _meet_device(re.compile(r"micro|мікрофон|микрофон", re.I))
MEET_CAM = _meet_device(re.compile(r"camera|камер", re.I))


def _meet_in_call(page: Page) -> bool:
    return (
        _first_visible(page.get_by_role("button", name=LEAVE_RE)) is not None
        or _first_visible(page.locator("button i", has_text=re.compile(r"^call_end$"))) is not None
    )


def _count_in(text: str) -> int | None:
    numbers = re.findall(r"\d+", text)
    return int(numbers[-1]) if numbers else None


def meet_participants(page: Page) -> int | None:
    """Кількість учасників (разом з тобою) зі значка на кнопці «Учасники»."""
    buttons = page.get_by_role("button", name=PEOPLE_RE)
    for index in range(min(buttons.count(), 5)):
        button = buttons.nth(index)
        if button.is_visible() and (count := _count_in(button.inner_text(timeout=1000))):
            return count
    if badge := _first_visible(page.locator(".uGOf1d")):  # клас значка в інтерфейсі Meet 2022–2024
        return _count_in(badge.inner_text(timeout=1000))
    return None


def observe_meet(page: Page) -> Observation:
    if _meet_in_call(page):
        mic = MEET_MIC(page)
        return Observation(participants=meet_participants(page), mic_on=mic is not None and _is_off(mic) is False)
    ended = _first_visible(page.get_by_role("button", name=MEET_ENDED_RE)) is not None
    return Observation(ended=ended)


CHAT_BUTTON_RE = re.compile(r"chat|чат", re.I)
CHAT_INPUT_RE = re.compile(r"send a message|message|повідомлення|сообщение", re.I)
# Повідомлення в чаті Meet мають data-message-id; автора шукаємо в заголовку групи повідомлень.
READ_MEET_CHAT_JS = """() => {
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  return [...document.querySelectorAll('[data-message-id]')].map((el) => {
    let author = '';
    let node = el;
    for (let depth = 0; node && depth < 5 && !author; depth++, node = node.parentElement) {
      author = clean(node.getAttribute && node.getAttribute('data-sender-name'));
      for (let sib = node.previousElementSibling; sib && !author; sib = sib.previousElementSibling) {
        if (sib.hasAttribute('data-message-id') || sib.querySelector('[data-message-id]')) break;
        const text = clean(sib.innerText);
        if (text && text.length <= 80) author = text.replace(/\\s*\\d{1,2}:\\d{2}(\\s*[AP]M)?$/i, '');
      }
    }
    return {id: el.getAttribute('data-message-id'), author, text: clean(el.innerText)};
  });
}"""


def _meet_chat_input(page: Page) -> Locator | None:
    return _first_visible(page.get_by_role("textbox", name=CHAT_INPUT_RE))


def open_meet_chat(page: Page) -> bool:
    if _meet_chat_input(page) is not None:
        return True  # уже відкритий: повторний клік його б закрив
    button = _first_visible(page.get_by_role("button", name=CHAT_BUTTON_RE))
    if button is None:
        return False
    button.click()
    page.wait_for_timeout(1000)
    return _meet_chat_input(page) is not None


def read_meet_chat(page: Page) -> list[ChatMessage]:
    if not open_meet_chat(page):
        return []
    rows = page.evaluate(READ_MEET_CHAT_JS)
    return [ChatMessage(row["id"], row["author"], row["text"]) for row in rows if row.get("id") and row.get("text")]


def send_meet_chat(page: Page, text: str) -> bool:
    if not open_meet_chat(page):
        return False
    box = _meet_chat_input(page)
    if box is None or not box.is_enabled():
        return False
    box.fill(text)
    box.press("Enter")
    page.wait_for_timeout(500)
    return True


def _meet_prejoin(page: Page, settings: JoinSettings, deadline: datetime) -> Locator:
    """Чекає на кнопку «Приєднатися», попутно закриваючи діалоги та заповнюючи ім'я гостя."""
    stuck_since = monotonic()
    while _now(deadline) < deadline:
        if "accounts.google.com" in page.url:
            raise JoinError(LOGIN_HINT, retry=False)
        if _visible_text(page, DENIED_RE):
            raise JoinError("Meet каже, що приєднатися до цієї зустрічі не можна", retry=False)
        if _visible_text(page, NOT_STARTED_RE):
            log.info("Зустріч ще не почалася — перевірю знову за 30 с")
            page.wait_for_timeout(30_000)
            page.reload(wait_until="domcontentloaded")
            stuck_since = monotonic()
            continue
        for pattern in (CONTINUE_WITHOUT_RE, DISMISS_RE):
            if button := _first_visible(page.get_by_role("button", name=pattern)):
                button.click()
        name_box = _first_visible(page.get_by_role("textbox", name=NAME_RE))
        if name_box is not None and not name_box.input_value():
            if not settings.display_name:
                raise JoinError(f"Meet просить ім'я гостя — {LOGIN_HINT} (або задай display_name)", retry=False)
            name_box.fill(settings.display_name)
        if button := _first_visible(page.get_by_role("button", name=JOIN_RE)):
            if button.is_enabled():
                return button
        if monotonic() - stuck_since > 90:
            raise JoinError("не дочекався кнопки «Приєднатися» в Meet")
        page.wait_for_timeout(1000)
    raise JoinError("пара вже закінчилась", retry=False)


def join_meet(context: BrowserContext, page: Page, url: str, settings: JoinSettings, deadline: datetime) -> None:
    context.grant_permissions(["microphone", "camera"], origin=MEET_ORIGIN)
    page.goto(meet_url(url, settings.meet_account), wait_until="domcontentloaded")
    join_button = _meet_prejoin(page, settings, deadline)
    if (settings.mute_microphone or settings.camera_off) and not _devices_off(page, settings, MEET_MIC, MEET_CAM):
        log.warning("Не вдалося переконатися, що мікрофон/камера вимкнені — забороняю Meet доступ до них")
        _block_media(context, page, MEET_ORIGIN)
        page.reload(wait_until="domcontentloaded")
        join_button = _meet_prejoin(page, settings, deadline)
    join_button.click()
    _wait_in_call(page, deadline, _meet_in_call)
    if settings.mute_microphone and MEET_MIC(page) is not None and not _ensure_off(page, MEET_MIC):
        log.error("УВАГА: у дзвінку мікрофон, схоже, увімкнений — вимкни його вручну!")


# --- Zoom у браузері (експериментально) --------------------------------------


def _zoom_toggle(selector: str, name_re: re.Pattern) -> Callable[[Page], Locator | None]:
    def find(page: Page) -> Locator | None:
        return _find(page, lambda f: f.locator(selector)) or _find(
            page, lambda f: f.get_by_role("button", name=name_re), _has_state
        )

    return find


ZOOM_MIC = _zoom_toggle("#preview-audio-control-button", ZOOM_MIC_RE)
ZOOM_CAM = _zoom_toggle("#preview-video-control-button", ZOOM_CAM_RE)


def _zoom_in_call(page: Page) -> bool:
    return (
        _find(page, lambda f: f.locator(".footer__leave-btn, .footer__leave-btn-container button")) is not None
        or _find(page, lambda f: f.get_by_role("button", name=re.compile(r"^\s*(leave|вийти|выйти)\b", re.I)))
        is not None
    )


def zoom_participants(page: Page) -> int | None:
    if counter := _find(page, lambda f: f.locator(".footer-button__number-counter")):
        return _count_in(counter.inner_text(timeout=1000))
    if button := _find(page, lambda f: f.get_by_role("button", name=re.compile(r"participant|учасник|участник", re.I))):
        return _count_in(f"{_label(button)} {button.inner_text(timeout=1000)}")
    return None


def observe_zoom(page: Page) -> Observation:
    if _zoom_in_call(page):
        mic = ZOOM_MIC(page)
        return Observation(participants=zoom_participants(page), mic_on=mic is not None and _is_off(mic) is False)
    ended = "postattendee" in page.url or _visible_text(page, ZOOM_ENDED_RE)
    return Observation(ended=ended)


def _zoom_prejoin(page: Page, settings: JoinSettings) -> Locator:
    started = monotonic()
    while monotonic() - started < 90:
        if _find(page, lambda f: f.get_by_role("textbox", name=PASSCODE_RE)):
            raise JoinError("Zoom просить код доступу — додай ?pwd=... у посилання в конфігу", retry=False)
        name_box = _find(page, lambda f: f.locator("#input-for-name")) or _find(
            page, lambda f: f.get_by_role("textbox", name=NAME_RE)
        )
        if name_box is not None and not name_box.input_value():
            if not settings.display_name:
                raise JoinError("Zoom у браузері просить ім'я — задай display_name у [join]", retry=False)
            name_box.fill(settings.display_name)
        button = _find(page, lambda f: f.locator("button.preview-join-button")) or _find(
            page, lambda f: f.get_by_role("button", name=JOIN_RE)
        )
        if button is not None and button.is_enabled():
            return button
        page.wait_for_timeout(1000)
    raise JoinError("не дочекався кнопки «Join» у веб-клієнті Zoom")


def join_zoom_web(context: BrowserContext, page: Page, url: str, settings: JoinSettings, deadline: datetime) -> None:
    context.grant_permissions(["microphone", "camera"], origin=ZOOM_ORIGIN)
    page.goto(zoom_web_url(url), wait_until="domcontentloaded")
    join_button = _zoom_prejoin(page, settings)
    if (settings.mute_microphone or settings.camera_off) and not _devices_off(page, settings, ZOOM_MIC, ZOOM_CAM):
        log.warning("Не вдалося переконатися, що мікрофон/камера вимкнені — забороняю Zoom доступ до них")
        _block_media(context, page, ZOOM_ORIGIN)
        page.reload(wait_until="domcontentloaded")
        join_button = _zoom_prejoin(page, settings)
    join_button.click()
    _wait_in_call(page, deadline, _zoom_in_call)


# --- Zoom-клієнт -------------------------------------------------------------


def open_uri(uri: str) -> None:
    if sys.platform == "win32":
        os.startfile(uri)  # noqa: S606 — так Windows відкриває zoommtg:// у Zoom
    elif sys.platform == "darwin":
        subprocess.Popen(["open", uri])
    else:
        subprocess.Popen(["xdg-open", uri])


# --- Браузер і фасад ---------------------------------------------------------


ANCHOR_PAGE = """<!doctype html><meta charset="utf-8"><title>autopara</title>
<body style="font: 16px sans-serif; padding: 2em">
<h2>autopara</h2><p>Тут відкриватимуться пари. Не закривай це вікно, поки працює autopara.</p></body>"""


class Browser:
    """Один Chrome з окремим постійним профілем: логін у Google зберігається між запусками."""

    def __init__(
        self,
        profile_dir: Path,
        channel: str = "chrome",
        mute_sound: bool = True,
        headless: bool = False,
        executable_path: str = "",
        audio_sink: str = "",
        audio_tap: Callable[[str], None] | None = None,
    ):
        self.profile_dir = profile_dir
        self.channel = channel
        self.executable_path = executable_path
        self.audio_sink = audio_sink  # куди віддавати звук, щоб «вуха» могли його слухати
        self.audio_tap = audio_tap  # або: сторінка пари сама віддає звук сюди (base64 PCM 16 кГц)
        self.mute_sound = mute_sound
        self.headless = headless
        self._playwright = None
        self._context: BrowserContext | None = None

    def context(self) -> BrowserContext:
        if self._context is None:
            self._context = self._launch()
            self._context.on("close", lambda _: setattr(self, "_context", None))
            if self.audio_tap is not None:
                tap = self.audio_tap
                self._context.expose_binding("__autoparaAudio", lambda source, data: tap(data))
                self._context.add_init_script(script=TAP_SCRIPT.read_text(encoding="utf-8"))
            # Перша вкладка — «якір»: пари відкриваються в нових вкладках і закриваються,
            # а вікно браузера лишається.
            if self._context.pages:
                self._context.pages[0].set_content(ANCHOR_PAGE)
        return self._context

    def new_page(self) -> Page:
        return self.context().new_page()

    def any_page(self) -> Page | None:
        """Будь-яка відкрита вкладка, не запускаючи браузер."""
        if self._context is None:
            return None
        try:
            return next((page for page in self._context.pages if not page.is_closed()), None)
        except PlaywrightError:
            return None

    def _launch(self) -> BrowserContext:
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        args = ["--disable-blink-features=AutomationControlled", "--no-default-browser-check"]
        if self.audio_tap is not None:
            args.append("--autoplay-policy=no-user-gesture-required")  # щоб збирач звуку стартував одразу
        env = None
        if self.audio_sink:
            # Звук іде у віртуальний вихід: у динаміках тиша, а вуха його чують.
            env = {**os.environ, "PULSE_SINK": self.audio_sink}
        elif self.mute_sound:
            args.append("--mute-audio")
        options = dict(
            env=env,
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            args=args,
            # Без цього прапорця Google не пускає логінитись («браузер може бути небезпечним»).
            ignore_default_args=["--enable-automation"],
            no_viewport=True,
        )
        preferred = {"executable_path": self.executable_path} if self.executable_path else {}
        if not preferred and self.channel:
            preferred = {"channel": self.channel}
        if preferred:
            try:
                return self._playwright.chromium.launch_persistent_context(**preferred, **options)
            except PlaywrightError as e:
                which = next(iter(preferred.values()))
                log.warning(
                    "Не вдалося запустити браузер %r (%s). Беру вбудований Chromium — "
                    "логін у Google, зроблений у %r, там може не зберегтися.",
                    which,
                    str(e).splitlines()[0],
                    which,
                )
        return self._playwright.chromium.launch_persistent_context(**options)

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except PlaywrightError:
                pass
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None


class Joiner:
    def __init__(
        self,
        settings: JoinSettings,
        profile_dir: Path,
        headless: bool = False,
        opener=open_uri,
        audio_sink: str = "",
        frames=None,
        audio_tap: Callable[[str], None] | None = None,
    ):
        self.settings = settings
        self.frames = frames  # FrameBuffer для відеовирізок, або None
        self.browser = Browser(
            profile_dir, settings.browser_channel, settings.mute_sound, headless, settings.browser_path, audio_sink,
            audio_tap,
        )
        self._open_uri = opener

    def join(self, visit: Visit) -> Session:
        if visit.platform == "zoom" and self.settings.zoom_mode == "app":
            self._open_uri(zoom_app_url(visit.url) or visit.url)
            return Session(visit, visit.leave_at)
        page = self.browser.new_page()
        try:
            if visit.platform == "meet":
                join_meet(self.browser.context(), page, visit.url, self.settings, visit.leave_at)
            elif visit.platform == "zoom":
                join_zoom_web(self.browser.context(), page, visit.url, self.settings, visit.leave_at)
            else:
                page.goto(visit.url, wait_until="domcontentloaded")
                log.warning("%s — не Meet і не Zoom: сторінку відкрито, мікрофон перевір сам", visit.url)
        except JoinError:
            self._discard(page)
            raise
        except PlaywrightError as e:
            self._discard(page)
            raise JoinError(f"помилка браузера: {str(e).splitlines()[0]}") from e
        self._screencast(page)
        return Session(visit, visit.leave_at, page)

    def _screencast(self, page: Page) -> None:
        """Кадри сторінки — у буфер для відеовирізок. Трансляція живе, поки відкрита вкладка."""
        if self.frames is None:
            return
        try:
            cdp = page.context.new_cdp_session(page)

            def on_frame(params: dict) -> None:
                try:
                    cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
                except PlaywrightError:
                    return
                self.frames.add(base64.b64decode(params["data"]))

            cdp.on("Page.screencastFrame", on_frame)
            cdp.send(
                "Page.startScreencast",
                {"format": "jpeg", "quality": 55, "maxWidth": 1280, "maxHeight": 800, "everyNthFrame": 6},
            )
        except PlaywrightError as e:
            log.warning("Не вдалося увімкнути запис кадрів для вирізок: %s", e)

    def idle(self, seconds: float) -> None:
        """Чекає, але дає браузеру доставляти події (кадри для вирізок). time.sleep їх би затримав."""
        page = self.browser.any_page()
        if page is not None:
            try:
                page.wait_for_timeout(seconds * 1000)
                return
            except PlaywrightError:
                pass
        time.sleep(seconds)

    def observe(self, session: Session) -> Observation:
        """Що зараз у зустрічі. Якщо щось пішло не так — «нічого не відомо», і ніхто нікуди не виходить."""
        page = session.page
        if page is None:
            return Observation()
        try:
            if session.visit.platform == "meet":
                return observe_meet(page)
            if session.visit.platform == "zoom":
                return observe_zoom(page)
        except PlaywrightError as e:
            log.debug("Не вдалося роздивитись зустріч: %s", e)
        return Observation()

    def read_chat(self, session: Session) -> list[ChatMessage]:
        if session.page is None or session.visit.platform != "meet":
            return []  # чат Zoom-клієнта нам не видно
        try:
            return read_meet_chat(session.page)
        except PlaywrightError as e:
            log.debug("Не вдалося прочитати чат: %s", e)
            return []

    def send_chat(self, session: Session, text: str) -> bool:
        if session.page is None or session.visit.platform != "meet":
            return False
        try:
            return send_meet_chat(session.page, text)
        except PlaywrightError as e:
            log.warning("Не вдалося написати в чат: %s", e)
            return False

    def screenshot(self, session: Session) -> bytes | None:
        if session.page is None:
            return None
        try:
            return session.page.screenshot(type="jpeg", quality=70)
        except PlaywrightError as e:
            log.warning("Не вдалося зробити скріншот: %s", e)
            return None

    def alive(self, session: Session) -> bool:
        return session.page is None or not session.page.is_closed()

    def leave(self, session: Session) -> None:
        if session.page is None:
            log.info("Zoom-клієнт не закриваю — вийди з конференції сам, коли зручно")
            return
        self._discard(session.page)

    def _discard(self, page: Page) -> None:
        try:
            if not page.is_closed():
                page.close()
        except PlaywrightError:
            pass

    def close(self) -> None:
        self.browser.close()
