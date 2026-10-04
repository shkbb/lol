"""Заходить у Google Meet / Zoom з вимкненими мікрофоном і камерою.

Головне правило безпеки: якщо не вдалося *переконатися*, що мікрофон вимкнено,
ми забороняємо сторінці доступ до мікрофона й камери на рівні браузера
і заходимо вже так. Інтерфейс Meet/Zoom змінюється часто, а «гарячий» мікрофон
на парі — найгірший можливий результат.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit
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
from .links import meet_url, with_account, zoom_app_url, zoom_web_url
from .planner import Visit
from .screen import Screen, Viewer
from .session import JoinDenied, JoinError, Session

log = logging.getLogger(__name__)

MEET_ORIGIN = "https://meet.google.com"
ZOOM_ORIGIN = "https://app.zoom.us"
LOGIN_HINT = "потрібно увійти в Google: «Налаштування» → «Акаунти» → «Увійти в Google»"

# Інтерфейс Meet/Zoom буває англійською, українською чи російською — шукаємо всіма трьома.
# Ці шаблони перевіряє браузер (JavaScript), а там \w і \b не розуміють кирилиці — тож лише \s, \S і «.*».
JOIN_RE = re.compile(
    r"^\s*(join now|join|ask to join|request to join|приєднатися( зараз)?|попросити\s.*приєдна\S*"
    r"|надіслати запит\s.*приєдна\S*|присоединиться( сейчас)?|попросить\s.*присоедин\S*"
    r"|отправить запрос\s.*присоедин\S*)\s*$",
    re.I,
)
# Кнопки, яких мозок навіть не бачить, коли обирає «увійти»: мікрофон, камера, чат, вихід, акаунт тощо.
NEVER_ASK_RE = re.compile(
    r"micro|мікрофон|микрофон|camera|камер|video|відео|видео|chat|чат|leave|покинути|вийти|выйти|end call|hang"
    r"|present|демонстр|екран|экран|record|запис|raise|підня|подня|react|реакц|caption|субтитр|share|поділит|поделит"
    r"|setting|налаштуван|настро|people|люди|учасник|участник|more|\bще\b|ещ[её]|other|інші|другие|effect|ефект|эффект"
    r"|background|фон|switch|змін|смен|account|акаунт|обліков|аккаунт|companion|супутн|cancel|скасу|отмен|help|довідк|справк"
    r"|home|головн|главн|support|підтримк|поддержк|apps|додатк|приложен"
    r"|audio|звук|sound|check|перевір|провер|test|report|скарг|поскарж|жалоб|feedback|відгук|отзыв",
    re.I,
)
CONTINUE_WITHOUT_RE = re.compile(r"continue without|продовжити без|продолжить без", re.I)
DISMISS_RE = re.compile(r"^\s*(got it|dismiss|зрозуміло|закрити|понятно|закрыть)\s*$", re.I)
# Підказки поверх зустрічі під час пари — лише «зрозуміло»: «Закрити» там закрив би, наприклад, чат.
TIP_RE = re.compile(r"^\s*(got it|зрозуміло|понятно)\s*$", re.I)
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
            raise JoinDenied("організатор не впустив або зустріч недоступна")
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


def _dismiss_popups(page: Page) -> None:
    """Підказки Meet поверх зустрічі («Користувачі можуть бачити ваше відео інакше… Зрозуміло») заважають чату."""
    for _ in range(3):
        button = _first_visible(page.get_by_role("button", name=TIP_RE))
        if button is None:
            return
        try:
            button.click(timeout=2000)
        except PlaywrightError:
            return
        page.wait_for_timeout(300)


def open_meet_chat(page: Page) -> bool:
    if _meet_chat_input(page) is not None:
        return True  # уже відкритий: повторний клік його б закрив
    _dismiss_popups(page)
    button = _first_visible(page.get_by_role("button", name=CHAT_BUTTON_RE))
    if button is not None:
        try:
            button.click(timeout=3000)
        except PlaywrightError:
            pass
        page.wait_for_timeout(1000)
    if _meet_chat_input(page) is None:
        # Вузьке вікно — Meet ховає кнопку чату в «⋮». Тоді — його ж клавіші: Ctrl+Alt+C відкриває чат.
        page.keyboard.press("Control+Alt+c")
        page.wait_for_timeout(1000)
    return _meet_chat_input(page) is not None


def describe_controls(page: Page) -> str:
    """Які кнопки й поля видно на сторінці — для журналу, коли щось не знайшлося (верстку Meet змінили)."""
    try:
        names = page.eval_on_selector_all(
            "button, [role=button], textarea, input, [contenteditable=true]",
            "els => els.filter(e => e.offsetParent).map(e => e.tagName.toLowerCase() + ':' +"
            " (e.getAttribute('aria-label') || e.getAttribute('placeholder') || e.innerText || '').trim().slice(0, 40))",
        )
    except PlaywrightError:
        return "?"
    return ", ".join(dict.fromkeys(name for name in names if not name.endswith(":")))


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


class ButtonHelper:
    """Знайомої кнопки «Приєднатися» немає (інша мова, Google перейменував) — мозок обирає її зі списку кнопок
    на сторінці. Мікрофон, камеру, чат і подібне він не бачить зовсім, а перед натисканням програма однаково
    перевіряє, що мікрофон і камера вимкнені. Вдалий вибір запам'ятовуємо — далі без мозку."""

    QUESTION = ("Це сторінка входу в онлайн-зустріч (Google Meet). Яка з цих кнопок приєднує до зустрічі "
                "(входить у дзвінок або надсилає запит на вхід)?")

    def __init__(self, choose: Callable[[str, list[str]], int | None], learned_file: Path):
        self.choose = choose
        self.learned_file = learned_file
        self.pending = ""

    def learned(self) -> list[str]:
        try:
            return [label for label in json.loads(self.learned_file.read_text(encoding="utf-8")) if isinstance(label, str)]
        except (OSError, ValueError, TypeError):
            return []

    def known_button(self, page: Page) -> Locator | None:
        for label in self.learned():
            button = _first_visible(page.get_by_role("button", name=label, exact=True))
            if button is not None and button.is_enabled():
                return button
        return None

    def ask(self, page: Page) -> Locator | None:
        names = page.eval_on_selector_all(
            "button, [role=button]",
            "els => els.filter(e => e.offsetParent !== null).map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())",
        )
        options = list(dict.fromkeys(name for name in names if name and len(name) < 80 and not NEVER_ASK_RE.search(name)))
        if not options:
            return None
        log.info("Не впізнаю кнопку входу — питаю мозок (кнопки: %s)", ", ".join(f"«{o}»" for o in options))
        try:
            index = self.choose(self.QUESTION, options)
        except Exception as e:  # noqa: BLE001 — мозок лише допомагає; без нього вхід іде далі як завжди
            log.warning("Мозок не відповів: %s", e)
            index = None
        if index is None or not 0 <= index < len(options):
            log.warning("Мозок не знайшов кнопку входу")
            return None
        log.info("Мозок: вхід — кнопка «%s»", options[index])
        self.pending = options[index]
        return _first_visible(page.get_by_role("button", name=options[index], exact=True))

    def confirm(self) -> None:
        """Зайшли — запам'ятати кнопку, щоб наступного разу натиснути її одразу."""
        if self.pending and self.pending not in self.learned():
            self.learned_file.parent.mkdir(parents=True, exist_ok=True)
            self.learned_file.write_text(json.dumps(self.learned() + [self.pending], ensure_ascii=False), encoding="utf-8")
            log.info("Запам'ятав кнопку входу «%s»", self.pending)
        self.pending = ""


def _meet_prejoin(page: Page, settings: JoinSettings, deadline: datetime, helper: ButtonHelper | None = None) -> Locator:
    """Чекає на кнопку «Приєднатися», попутно закриваючи діалоги та заповнюючи ім'я гостя."""
    stuck_since = monotonic()
    asked = False
    while _now(deadline) < deadline:
        if "accounts.google.com" in page.url:
            raise JoinError(LOGIN_HINT, retry=False)
        if _visible_text(page, DENIED_RE):
            raise JoinDenied("Meet каже, що приєднатися до цієї зустрічі не можна")
        if _visible_text(page, NOT_STARTED_RE):
            log.info("Зустріч ще не почалася — перевірю знову за 30 с")
            page.wait_for_timeout(30_000)
            page.reload(wait_until="domcontentloaded")
            stuck_since = monotonic()
            continue
        for pattern in (CONTINUE_WITHOUT_RE, DISMISS_RE):
            if button := _first_visible(page.get_by_role("button", name=pattern)):
                button.click()
        # Вимикаємо одразу, щойно Meet показав кнопки, — щоб камера не світилась, поки чекаємо.
        # Остаточна перевірка однаково буде перед самим входом (join_meet → _devices_off).
        if settings.mute_microphone:
            _ensure_off(page, MEET_MIC)
        if settings.camera_off:
            _ensure_off(page, MEET_CAM)
        name_box = _first_visible(page.get_by_role("textbox", name=NAME_RE))
        if name_box is not None and not name_box.input_value():
            if not settings.display_name:
                raise JoinError(f"Meet просить ім'я гостя — {LOGIN_HINT} (або впиши «Ім'я для входу гостем» в «Акаунтах»)", retry=False)
            name_box.fill(settings.display_name)
        if button := _first_visible(page.get_by_role("button", name=JOIN_RE)):
            if button.is_enabled():
                return button
        if helper is not None:
            if button := helper.known_button(page):
                return button
            if not asked and monotonic() - stuck_since > 20:
                asked = True
                if (button := helper.ask(page)) is not None and button.is_enabled():
                    return button
        if monotonic() - stuck_since > 90:
            raise JoinError("не дочекався кнопки «Приєднатися» в Meet")
        page.wait_for_timeout(1000)
    raise JoinError("пара вже закінчилась", retry=False)


def join_meet(context: BrowserContext, page: Page, url: str, settings: JoinSettings, deadline: datetime,
              helper: ButtonHelper | None = None) -> None:
    context.grant_permissions(["microphone", "camera"], origin=MEET_ORIGIN)
    page.goto(meet_url(url, settings.meet_account), wait_until="domcontentloaded")
    join_button = _meet_prejoin(page, settings, deadline, helper)
    if (settings.mute_microphone or settings.camera_off) and not _devices_off(page, settings, MEET_MIC, MEET_CAM):
        log.warning("Не вдалося переконатися, що мікрофон/камера вимкнені — забороняю Meet доступ до них")
        _block_media(context, page, MEET_ORIGIN)
        page.reload(wait_until="domcontentloaded")
        join_button = _meet_prejoin(page, settings, deadline, helper)
    join_button.click()
    _wait_in_call(page, deadline, _meet_in_call)
    if helper is not None:
        helper.confirm()
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


def disallow_browser_signin(profile_dir: Path) -> None:
    """У Google входимо лише на сайтах, а не в сам Chrome («Дозволити вхід у Chrome» — вимкнено).

    Інакше Chrome переносить акаунт організації (університетська пошта, Google Workspace)
    в окремий профіль Chrome, і в нашому профілі після перезапуску входу вже немає.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        prefs = {}
    except (OSError, ValueError) as e:
        log.warning("Не вдалося прочитати налаштування браузера %s: %s", prefs_file, e)
        return
    signin = prefs.get("signin")
    if not isinstance(signin, dict):
        signin = prefs["signin"] = {}
    # Перемикач у налаштуваннях Chrome — це allowed_on_next_startup; при запуску Chrome
    # переписує ним allowed, тож ставимо обидва.
    if signin.get("allowed") is False and signin.get("allowed_on_next_startup") is False:
        return
    signin["allowed"] = signin["allowed_on_next_startup"] = False
    prefs_file.parent.mkdir(parents=True, exist_ok=True)
    prefs_file.write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")


LIST_ACCOUNTS = "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def chrome_user_agent() -> str:
    """Підпис звичайного Chrome тієї ж версії, що й браузер програми (невидимий підписується «HeadlessChrome»)."""
    try:
        import playwright

        data = json.loads((Path(playwright.__file__).parent / "driver" / "package" / "browsers.json").read_text())
        version = next(b["browserVersion"] for b in data["browsers"] if b["name"] == "chromium").split(".")[0]
    except (OSError, ValueError, KeyError, StopIteration):
        return ""
    system = {"win32": "Windows NT 10.0; Win64; x64", "darwin": "Macintosh; Intel Mac OS X 10_15_7"}.get(
        sys.platform, "X11; Linux x86_64")
    return f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}.0.0.0 Safari/537.36"


def parse_google_accounts(text: str) -> list[str]:
    """Відповідь ListAccounts: ["gaia.l.a.r", [["gaia.l.a", 1, "Ім'я", "пошта", ...], ...]] — беремо пошти."""
    try:
        entries = json.loads(text)[1]
    except (ValueError, IndexError, TypeError):
        entries = [text]
    found = []
    for entry in entries if isinstance(entries, list) else []:
        match = EMAIL_RE.search(json.dumps(entry, ensure_ascii=False))
        if match and match.group(0).lower() not in found:
            found.append(match.group(0).lower())
    return found


class Browser:
    """Вбудований Chromium програми з окремим постійним профілем: вхід у Google зберігається між запусками.

    Працює невидимо, а його вкладки видно у вікні програми (див. screen.py) — окремого вікна браузера немає.
    Запускається, лише коли потрібен (пара, вхід у Google), і закривається, щойно стає непотрібним.
    """

    def __init__(
        self,
        profile_dir: Path,
        mute_sound: bool = True,
        headless: bool = True,
        audio_sink: str = "",
        audio_tap: Callable[[str], None] | None = None,
        screen: Screen | None = None,
    ):
        self.profile_dir = profile_dir
        self.audio_sink = audio_sink  # куди віддавати звук, щоб «вуха» могли його слухати
        self.audio_tap = audio_tap  # або: сторінка пари сама віддає звук сюди (base64 PCM 16 кГц)
        self.mute_sound = mute_sound
        # Для розробки: AUTOPARA_BROWSER_WINDOW=1 — звичайне вікно браузера замість картинки у вікні програми.
        self.headless = headless and not os.environ.get("AUTOPARA_BROWSER_WINDOW")
        self.screen = screen if self.headless else None
        self._playwright = None
        self._context: BrowserContext | None = None
        self._viewer: Viewer | None = None
        self._blank: Page | None = None  # порожня вкладка щойно відкритого вікна — беремо її, без зайвих вкладок

    def context(self) -> BrowserContext:
        if self._context is None:
            self._context = self._launch()
            self._context.on("close", lambda _: self._forget())
            if self.audio_tap is not None:
                tap = self.audio_tap
                self._context.expose_binding("__autoparaAudio", lambda source, data: tap(data))
                self._context.add_init_script(script=TAP_SCRIPT.read_text(encoding="utf-8"))
            self._blank = self._context.pages[0] if self._context.pages else None
            if self.screen is not None:
                self._viewer = Viewer(self.screen, self._context)
        return self._context

    def new_page(self) -> Page:
        context = self.context()
        page, self._blank = self._blank, None
        if page is None or page.is_closed():
            page = context.new_page()
        if self._viewer is not None:
            self._viewer.show(page, own=True)  # у вікні програми видно саме її
        return page

    def any_page(self) -> Page | None:
        """Будь-яка відкрита вкладка, не запускаючи браузер."""
        if self._context is None:
            return None
        try:
            return next((page for page in self._context.pages if not page.is_closed()), None)
        except PlaywrightError:
            return None

    def google_accounts(self) -> list[str] | None:
        """У які Google-акаунти увійдено в браузері програми — запитом з його кукі, без жодної вкладки.
        None — браузер закритий або Google не відповів."""
        if self._context is None:
            return None
        try:
            response = self._context.request.post(LIST_ACCOUNTS, data=" ", timeout=10_000)
            if not response.ok:
                return None
            return parse_google_accounts(response.text())
        except PlaywrightError:
            return None

    def is_open(self) -> bool:
        return self._context is not None

    def close_if_idle(self) -> None:
        """Пара скінчилась: якщо у вікні більше нічого немає — закриваємо його, а не тримаємо порожнім."""
        if self._context is None:
            return
        try:
            busy = any(not page.is_closed() and page.url != "about:blank" for page in self._context.pages)
        except PlaywrightError:
            busy = False
        if not busy:
            self.close()

    def pump(self, seconds: float) -> None:
        """Чекає ``seconds``, але весь час обслуговує браузер: доставляє події (кадри для вирізок), а коли
        браузер видно у вікні програми — трансляцію й дії людини, без помітної затримки."""
        deadline = monotonic() + seconds
        while True:
            viewer = self._viewer
            if viewer is not None:
                viewer.serve()
            left = deadline - monotonic()
            if left <= 0:
                return
            page = self.any_page()
            if page is None:
                time.sleep(left)
                return
            step = min(left, 0.03) if self.screen is not None and self.screen.watching else left
            if viewer is not None:
                viewer.pumping = True
            try:
                page.wait_for_timeout(step * 1000)
            except PlaywrightError:
                time.sleep(min(left, 0.05))
            finally:
                if viewer is not None:
                    viewer.pumping = False

    def _forget(self) -> None:
        self._context = None
        self._blank = None
        if self._viewer is not None:
            self._viewer.closed()
            self._viewer = None

    def _launch(self) -> BrowserContext:
        if self._playwright is None:
            self._playwright = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        disallow_browser_signin(self.profile_dir)
        args = ["--disable-blink-features=AutomationControlled", "--no-default-browser-check"]
        # Без цього прапорця Google не пускає логінитись («браузер може бути небезпечним»).
        ignore = ["--enable-automation"]
        if self.audio_tap is not None:
            args.append("--autoplay-policy=no-user-gesture-required")  # щоб збирач звуку стартував одразу
            # Вуха беруть звук вкладки пари (getDisplayMedia) — без запитання на екрані. «Без звуку» тоді робить
            # сам збирач (suppressLocalAudioPlayback): з --mute-audio записувалася б тиша.
            args.append("--auto-accept-this-tab-capture")
        env = None
        if self.audio_sink:
            # Звук іде у віртуальний вихід: у динаміках тиша, а вуха його чують.
            env = {**os.environ, "PULSE_SINK": self.audio_sink}
        mute = self.mute_sound and not self.audio_sink and self.audio_tap is None
        options: dict = {}
        if self.headless:
            # Невидимий Chromium — справжній Chrome, лише без вікна. Але без відеокарти Meet гальмує,
            # а з підписом «HeadlessChrome» Google може не пустити — тож вмикаємо її й підписуємось як Chrome.
            args.append("--enable-gpu")
            agent = chrome_user_agent()
            if agent:
                args.append(f"--user-agent={agent}")
            ignore.append("--hide-scrollbars")  # людині у вікні програми смуги прокрутки потрібні
            if not mute:
                ignore.append("--mute-audio")  # Playwright глушить невидимий браузер сам
            width, height = self.screen.size if self.screen is not None else (1280, 800)
            options = {"channel": "chromium", "viewport": {"width": width, "height": height},
                       "device_scale_factor": self.screen.scale if self.screen is not None else 1}
        else:
            if mute:
                args.append("--mute-audio")
            options = {"no_viewport": True}
        return self._playwright.chromium.launch_persistent_context(
            env=env,
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            args=args,
            ignore_default_args=ignore,
            **options,
        )

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except PlaywrightError:
                pass
            self._forget()
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None


class Joiner:
    def __init__(
        self,
        settings: JoinSettings,
        profile_dir: Path,
        headless: bool = True,
        opener=open_uri,
        audio_sink: str = "",
        frames=None,
        audio_tap: Callable[[str], None] | None = None,
        zoom_app_heard: bool = True,
        chooser: Callable[[str, list[str]], int | None] | None = None,
        screen: Screen | None = None,
    ):
        self.settings = settings
        self.frames = frames  # FrameBuffer для відеовирізок, або None
        # zoom_mode = "auto": клієнт, якщо вуха його чують; інакше браузер. Посилання, де браузер
        # не пустив, запам'ятовуємо — туди надалі одразу клієнтом.
        self.zoom_app_heard = zoom_app_heard
        self._app_links_file = profile_dir.parent / "zoom-app-links.json"
        self.browser = Browser(profile_dir, settings.mute_sound, headless, audio_sink, audio_tap, screen)
        # Мозок — запасний вихід, коли знайомої кнопки входу немає (див. ButtonHelper).
        self.helper = ButtonHelper(chooser, profile_dir.parent / "learned-buttons.json") if chooser else None
        self._open_uri = opener

    def join(self, visit: Visit) -> Session:
        if visit.platform == "zoom" and self._zoom_in_app(visit.url):
            return self._open_zoom_app(visit)
        page = self.browser.new_page()
        try:
            try:
                if visit.platform == "meet":
                    self._join_meet(page, visit)
                elif visit.platform == "zoom":
                    join_zoom_web(self.browser.context(), page, visit.url, self.settings, visit.leave_at)
                else:
                    page.goto(visit.url, wait_until="domcontentloaded")
                    log.warning("%s — не Meet і не Zoom: сторінку відкрито, мікрофон перевір сам", visit.url)
            except PlaywrightError as e:
                raise JoinError(f"помилка браузера: {str(e).splitlines()[0]}") from e
        except JoinError as e:
            self._snapshot(page)
            self._discard(page)
            self.browser.close_if_idle()  # не лишати порожнє вікно після невдалої спроби
            if visit.platform == "zoom" and self.settings.zoom_mode == "auto":
                # На пару важливіше потрапити: якщо браузер не пустив — Zoom-клієнт.
                log.warning("Zoom у браузері не пустив (%s) — відкриваю Zoom-клієнт", e)
                if not e.retry:
                    self._remember_app_link(visit.url)
                return self._open_zoom_app(visit)
            raise
        self._screencast(page)
        self._enable_ears(page)
        return Session(visit, visit.leave_at, page)

    def _join_meet(self, page: Page, visit: Visit) -> None:
        """Meet не пускає з основного акаунта (напр., університетський Workspace не пускає на зовнішні
        зустрічі) — пробуємо інші акаунти, в які увійдено: на пару важливіше потрапити."""
        query = parse_qs(urlsplit(visit.url).query)
        first = (query.get("authuser") or [self.settings.meet_account])[0]
        others = [a for a in [self.settings.meet_account, *self.settings.accounts] if a and a != first]
        try:
            join_meet(self.browser.context(), page, visit.url, self.settings, visit.leave_at, self.helper)
            return
        except JoinDenied as e:
            if not others:
                raise
            denied = e
        for account in dict.fromkeys(others):
            log.warning("Meet не пускає з %s (%s) — пробую з %s", first or "основного акаунта", denied, account)
            try:
                join_meet(self.browser.context(), page, with_account(visit.url, account), self.settings, visit.leave_at,
                          self.helper)
                log.info("Зайшов з акаунта %s", account)
                return
            except JoinDenied as e:
                denied, first = e, account
        raise denied

    def _snapshot(self, page: Page, name: str = "last-join-failure.png") -> None:
        """Що показав браузер, коли щось не вдалося, — щоб причину було видно, а не вгадувати."""
        path = self._app_links_file.parent / name
        try:
            if not page.is_closed():
                page.screenshot(path=str(path))
                log.info("Що показав браузер: %s", path)
        except PlaywrightError:
            pass

    def _open_zoom_app(self, visit: Visit) -> Session:
        self._open_uri(zoom_app_url(visit.url) or visit.url)
        return Session(visit, visit.leave_at)

    def _zoom_in_app(self, url: str) -> bool:
        if self.settings.zoom_mode != "auto":
            return self.settings.zoom_mode == "app"
        return self.zoom_app_heard or url in self._app_links()

    def _app_links(self) -> list[str]:
        try:
            return json.loads(self._app_links_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _remember_app_link(self, url: str) -> None:
        links = self._app_links()
        if url not in links:
            self._app_links_file.parent.mkdir(parents=True, exist_ok=True)
            self._app_links_file.write_text(json.dumps(links + [url], ensure_ascii=False), encoding="utf-8")
            log.info("Запам'ятав: на %s надалі заходжу Zoom-клієнтом", url)

    def _enable_ears(self, page: Page) -> None:
        """Вуха слухають лише вкладку пари, куди зайшла програма, — не YouTube чи інші вкладки."""
        if self.browser.audio_tap is None:
            return
        try:
            if not page.evaluate("() => window.__autoparaTap ? window.__autoparaTap.enable() : false"):
                return
            # Звук самої вкладки — так чути справжній Meet на будь-якій ОС. Потрібен «жест користувача»,
            # тож через CDP з userGesture; запит на дозвіл браузер програми не показує (див. Browser._launch).
            cdp = page.context.new_cdp_session(page)
            try:
                suppress = "true" if self.settings.mute_sound else "false"
                result = cdp.send("Runtime.evaluate", {
                    "expression": f"window.__autoparaTap.captureTab({suppress})",
                    "awaitPromise": True, "userGesture": True, "returnByValue": True,
                })
            finally:
                cdp.detach()
            outcome = (result.get("result") or {}).get("value")
            if outcome == "ок":
                log.info("👂 Слухаю звук вкладки пари")
            elif outcome != "уже":
                log.warning("Звук вкладки пари недоступний (%s) — слухаю, як вийде", outcome)
        except PlaywrightError as e:
            log.debug("Не вдалося увімкнути вуха у вкладці: %s", e)

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
        """Чекає, але дає браузеру доставляти події (кадри, дії людини у вікні). time.sleep їх би затримав."""
        self.browser.pump(seconds)

    def observe(self, session: Session) -> Observation:
        """Що зараз у зустрічі. Якщо щось пішло не так — «нічого не відомо», і ніхто нікуди не виходить."""
        page = session.page
        if page is None:
            return Observation()
        self._enable_ears(page)  # якщо Meet перезавантажив сторінку, збирач звуку треба ввімкнути знову
        if self.browser.audio_tap is not None and log.isEnabledFor(logging.DEBUG):
            try:
                log.debug("👂 збирач звуку: %s", page.evaluate("() => window.__autoparaTap && window.__autoparaTap.stats()"))
            except PlaywrightError:
                pass
        try:
            if session.visit.platform == "meet":
                _dismiss_popups(page)  # підказки Meet закривають зустріч у вирізках і заважають чату
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
            if send_meet_chat(session.page, text):
                return True
            log.warning("Не знайшов чат Meet. На сторінці видно: %s", describe_controls(session.page))
        except PlaywrightError as e:
            log.warning("Не вдалося написати в чат: %s", e)
        self._snapshot(session.page, "last-chat-failure.png")
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
        self.browser.close_if_idle()

    def _discard(self, page: Page) -> None:
        try:
            if not page.is_closed():
                page.close()
        except PlaywrightError:
            pass

    def close(self) -> None:
        self.browser.close()
