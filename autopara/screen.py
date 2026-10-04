"""Браузер програми — у її вікні, а не окремим вікном.

Chromium програми працює невидимо, а вікно показує вибрану вкладку живою картинкою (трансляція CDP)
й передає їй мишку та клавіатуру. Так однаково на Linux, Windows і macOS: ні під час пари, ні під час
входу в Google окреме вікно браузера не з'являється.

Playwright можна чіпати лише з потоку, що його запустив (потік контролера), а Qt-віджети — лише з потоку
вікна. Тож між ними тільки ``Screen``: вікно кладе туди дії людини й свій розмір, а ``Viewer`` (потік
контролера) забирає їх і віддає кадри та список вкладок.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from collections import deque
from typing import Callable

log = logging.getLogger(__name__)

# Частіше картинка не оновлюється: трансляція коштує браузеру процесора, а він потрібен вухам.
# Кадр підтверджуємо не раніше, ніж за 1/15 с після попереднього, — проміжні браузер просто пропускає,
# а останній стан сторінки однаково надсилає.
FRAME_INTERVAL = 1 / 15

# Що виділено на сторінці (у полі введення виділення живе окремо від getSelection).
SELECTION_JS = """(() => {
  const e = document.activeElement;
  if (e && typeof e.selectionStart === 'number' && typeof e.value === 'string') {
    return e.value.slice(e.selectionStart, e.selectionEnd);
  }
  return String(getSelection() || '');
})()"""


class Screen:
    """Спільне для вікна й браузера. Методи без підкреслення — для потоку вікна."""

    def __init__(self, size: tuple[int, int] = (1280, 800), scale: float = 1.0):
        self._lock = threading.Lock()
        self._input: deque = deque()  # мишка, клавіатура, «копіювати» — виконуються щойно можна
        self._commands: deque = deque()  # вкладки, «назад», «оновити»
        self.watching = False  # браузер зараз видно у вікні — транслювати
        self.size = size  # розмір області перегляду, логічні пікселі
        self.scale = scale  # щільність пікселів екрана (HiDPI) — з нею браузер і запускається
        # Від браузера (з потоку контролера):
        self.on_frame: Callable[[bytes, dict], None] | None = None  # JPEG і metadata кадру
        self.on_tabs: Callable[[list[dict], int], None] | None = None  # вкладки й поточна (-1 — браузер закритий)
        self.on_copy: Callable[[str], None] | None = None  # скопійований зі сторінки текст

    # --- Потік вікна -------------------------------------------------------------------------

    def send(self, method: str, params: dict) -> None:
        """Подія введення CDP (Input.dispatchMouseEvent, Input.dispatchKeyEvent, Input.insertText)."""
        self._input.append(("input", method, params))

    def copy(self) -> None:
        self._input.append(("copy", "", {}))

    def command(self, name: str, value: int = -1) -> None:
        """tab (перейти на вкладку value) · close (закрити вкладку value) · back · reload."""
        self._commands.append((name, value))

    def watch(self, on: bool) -> None:
        self.watching = on

    def resize(self, width: int, height: int, scale: float | None = None) -> None:
        self.size = (max(200, int(width)), max(150, int(height)))
        if scale:
            self.scale = scale

    # --- Потік браузера ----------------------------------------------------------------------

    def take_input(self) -> list[tuple[str, str, dict]]:
        items = []
        while self._input:
            items.append(self._input.popleft())
        return coalesce_moves(items)

    def take_commands(self) -> list[tuple[str, int]]:
        items = []
        while self._commands:
            items.append(self._commands.popleft())
        return items

    def emit(self, name: str, *args) -> None:
        callback = getattr(self, f"on_{name}")
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 — вікно закрили чи воно зламалось: браузер працює далі
            log.debug("Вікно не прийняло %s", name, exc_info=True)


def coalesce_moves(items: list[tuple[str, str, dict]]) -> list[tuple[str, str, dict]]:
    """З кількох рухів мишки поспіль лишаємо останній — браузер не мусить наздоганяти кожен піксель."""
    out: list[tuple[str, str, dict]] = []
    for item in items:
        if out and _is_move(item) and _is_move(out[-1]):
            out[-1] = item
        else:
            out.append(item)
    return out


def _is_move(item: tuple[str, str, dict]) -> bool:
    return item[1] == "Input.dispatchMouseEvent" and item[2].get("type") == "mouseMoved"


class Viewer:
    """Сторона браузера (потік контролера): транслює вибрану вкладку у вікно й виконує дії людини."""

    def __init__(self, screen: Screen, context):
        self.screen = screen
        self.context = context
        self.page = None  # вкладка, яку видно у вікні
        self.own: list = []  # вкладки, які відкрила сама програма (пара, вхід) — людина їх не закриває
        self._cdp = None  # CDP-сесія вкладки у вікні: трансляція й введення
        self._casting = False
        self._size = screen.size
        self._tabs_sent: tuple | None = None
        self._titles: dict = {}
        self._next_titles = 0.0
        self._busy = False
        self._pending_ack: tuple | None = None
        self._last_ack = 0.0
        self.pumping = False  # контролер саме чекає в Browser.pump (а не посеред входу на пару)
        context.on("page", self._opened)

    # --- Вкладки ---------------------------------------------------------------------------------

    def show(self, page, own: bool = False) -> None:
        """Програма відкрила вкладку (пара, вхід у Google) — її й показуємо."""
        if own and page not in self.own:
            self.own.append(page)
        self._select(page)

    def _opened(self, page) -> None:
        # Сайт відкрив нову вкладку (посилання, вікно входу) — як і звичайний браузер, переходимо на неї.
        self._resize_page(page)
        self._select(page)

    def _pages(self) -> list:
        try:
            return [page for page in self.context.pages if not page.is_closed()]
        except Exception:  # noqa: BLE001 — браузер саме закривається
            return []

    def _select(self, page) -> None:
        if page is self.page:
            return
        self._stop()
        self.page = page
        self._tabs_sent = None  # список вкладок треба оновити у вікні

    # --- Головне: викликається, поки контролер чекає ------------------------------------------------

    def serve(self) -> None:
        pages = self._pages()
        self.own = [page for page in self.own if page in pages]
        if self.page not in pages:
            self._select(pages[-1] if pages else None)
        for name, value in self.screen.take_commands():
            self._command(name, value, pages)
            pages = self._pages()
        if self.screen.size != self._size:
            self._size = self.screen.size
            for page in pages:
                self._resize_page(page)
        want = self.screen.watching and self.page is not None
        if want and not self._casting:
            self._start()
        elif not want and self._casting:
            self._stop()
        self.serve_input()
        self.serve_acks()
        self._send_tabs(pages)

    def serve_acks(self) -> None:
        if self._pending_ack is not None and time.monotonic() >= self._last_ack + FRAME_INTERVAL:
            self._ack()

    def _ack(self) -> None:
        pending, self._pending_ack = self._pending_ack, None
        if pending is None:
            return
        cdp, session_id = pending
        self._last_ack = time.monotonic()
        try:
            cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
        except Exception:  # noqa: BLE001 — вкладку закрили
            pass

    def serve_input(self) -> None:
        """Дії людини — і з serve(), і щойно прийшов кадр (так їх видно навіть посеред довгого входу на пару)."""
        if self._busy:
            return
        self._busy = True
        try:
            for kind, method, params in self.screen.take_input():
                if self._cdp is None:
                    continue  # вкладку закрили — дії для неї вже ні до чого
                try:
                    if kind == "copy":
                        result = self._cdp.send("Runtime.evaluate", {"expression": SELECTION_JS, "returnByValue": True})
                        text = (result.get("result") or {}).get("value") or ""
                        if text:
                            self.screen.emit("copy", text)
                    else:
                        self._cdp.send(method, params)
                except Exception as e:  # noqa: BLE001 — вкладка закрилась посеред дії
                    log.debug("Дія у вкладці не вдалася: %s", e)
        finally:
            self._busy = False

    def _command(self, name: str, value: int, pages: list) -> None:
        try:
            if name == "tab" and 0 <= value < len(pages):
                self._select(pages[value])
            elif name == "close" and 0 <= value < len(pages) and pages[value] not in self.own:
                pages[value].close()
            elif name == "back" and self.page is not None:
                self.page.evaluate("() => history.back()")
            elif name == "reload" and self._cdp is not None:
                self._cdp.send("Page.reload", {})
        except Exception as e:  # noqa: BLE001
            log.debug("Команда браузера %s не вдалася: %s", name, e)

    # --- Трансляція ---------------------------------------------------------------------------

    def _start(self) -> None:
        page = self.page
        try:
            cdp = page.context.new_cdp_session(page)
            cdp.on("Page.screencastFrame", lambda params, cdp=cdp: self._frame(cdp, params))
            width, height = self._size
            scale = self.screen.scale
            cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90,
                                              "maxWidth": int(width * scale), "maxHeight": int(height * scale)})
        except Exception as e:  # noqa: BLE001 — вкладка закрилась саме зараз; спробуємо ще раз на наступному колі
            log.debug("Не вдалося показати вкладку: %s", e)
            return
        self._cdp = cdp
        self._casting = True
        self._tabs_sent = None

    def _stop(self) -> None:
        cdp, self._cdp, self._casting = self._cdp, None, False
        self._pending_ack = None
        if cdp is None:
            return
        try:
            cdp.send("Page.stopScreencast")
            cdp.detach()
        except Exception:  # noqa: BLE001 — вкладку вже закрито
            pass

    def _frame(self, cdp, params: dict) -> None:
        if cdp is not self._cdp:  # кадр від вкладки, яку вже не показуємо
            try:
                cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:  # noqa: BLE001
                pass
            return
        self.screen.emit("frame", base64.b64decode(params["data"]), params.get("metadata") or {})
        self._pending_ack = (cdp, params["sessionId"])
        # Посеред довгого входу на пару pump не крутиться — тоді підтверджуємо одразу, щоб картинка жила.
        if not self.pumping:
            self._ack()
        else:
            self.serve_acks()
        self.serve_input()

    def _resize_page(self, page) -> None:
        width, height = self._size
        try:
            page.set_viewport_size({"width": width, "height": height})
        except Exception as e:  # noqa: BLE001
            log.debug("Не вдалося змінити розмір вкладки: %s", e)
        if page is self.page and self._casting:
            self._stop()  # трансляцію перезапускаємо з новим розміром кадру
            self._start()

    def _send_tabs(self, pages: list) -> None:
        now = time.monotonic()
        if self.screen.watching and now >= self._next_titles:
            self._next_titles = now + 1.5
            for page in pages:
                try:
                    self._titles[page] = page.title()
                except Exception:  # noqa: BLE001 — сторінка саме переходить кудись
                    pass
        self._titles = {page: title for page, title in self._titles.items() if page in pages}
        tabs = tuple((self._titles.get(page) or "", page.url, page not in self.own) for page in pages)
        current = pages.index(self.page) if self.page in pages else -1
        if (tabs, current) == self._tabs_sent:
            return
        self._tabs_sent = (tabs, current)
        self.screen.emit("tabs", [{"title": title, "url": url, "closable": closable} for title, url, closable in tabs],
                         current)

    def closed(self) -> None:
        """Браузер закрився — у вікні порожньо."""
        self._cdp = None
        self._casting = False
        self.page = None
        self.screen.emit("tabs", [], -1)
