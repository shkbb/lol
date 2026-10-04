"""Сторінка «Браузер»: вкладки браузера програми прямо у вікні — пара, вхід у Google.

Окремого вікна браузера немає: він працює невидимо, а тут видно його живу картинку, і мишка
з клавіатурою працюють як у звичайному браузері (див. screen.py).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeySequence, QPainter
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QTabBar, QToolButton, QVBoxLayout, QWidget

from . import theme
from . import widgets as w

# Клавіші без тексту: Qt → (key, code, windowsVirtualKeyCode, text) для CDP.
SPECIAL = {
    Qt.Key.Key_Backspace: ("Backspace", "Backspace", 8, ""),
    Qt.Key.Key_Tab: ("Tab", "Tab", 9, ""),
    Qt.Key.Key_Backtab: ("Tab", "Tab", 9, ""),
    Qt.Key.Key_Return: ("Enter", "Enter", 13, "\r"),
    Qt.Key.Key_Enter: ("Enter", "NumpadEnter", 13, "\r"),
    Qt.Key.Key_Escape: ("Escape", "Escape", 27, ""),
    Qt.Key.Key_Space: (" ", "Space", 32, " "),
    Qt.Key.Key_PageUp: ("PageUp", "PageUp", 33, ""),
    Qt.Key.Key_PageDown: ("PageDown", "PageDown", 34, ""),
    Qt.Key.Key_End: ("End", "End", 35, ""),
    Qt.Key.Key_Home: ("Home", "Home", 36, ""),
    Qt.Key.Key_Left: ("ArrowLeft", "ArrowLeft", 37, ""),
    Qt.Key.Key_Up: ("ArrowUp", "ArrowUp", 38, ""),
    Qt.Key.Key_Right: ("ArrowRight", "ArrowRight", 39, ""),
    Qt.Key.Key_Down: ("ArrowDown", "ArrowDown", 40, ""),
    Qt.Key.Key_Insert: ("Insert", "Insert", 45, ""),
    Qt.Key.Key_Delete: ("Delete", "Delete", 46, ""),
    Qt.Key.Key_Shift: ("Shift", "ShiftLeft", 16, ""),
    Qt.Key.Key_Control: ("Control", "ControlLeft", 17, ""),
    Qt.Key.Key_Alt: ("Alt", "AltLeft", 18, ""),
    Qt.Key.Key_Meta: ("Meta", "MetaLeft", 91, ""),
    **{getattr(Qt.Key, f"Key_F{n}"): (f"F{n}", f"F{n}", 111 + n, "") for n in range(1, 13)},
}
# Українська розкладка: на якій латинській клавіші стоїть літера — щоб Ctrl+С (кирилична) був Ctrl+C.
CYRILLIC = dict(zip("ЙЦУКЕНГШЩЗХЇФІВАПРОЛДЖЄЯЧСМИТЬБЮЫЭЪ", "QWERTYUIOP[]ASDFGHJKL;'ZXCVBNM,.S']"))
BUTTONS = {Qt.MouseButton.LeftButton: "left", Qt.MouseButton.RightButton: "right", Qt.MouseButton.MiddleButton: "middle"}
BUTTON_BITS = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.RightButton: 2, Qt.MouseButton.MiddleButton: 4}


def modifiers(mods) -> int:
    """Маска модифікаторів CDP: Alt=1, Ctrl=2, Meta=4, Shift=8."""
    bits = 0
    for flag, bit in ((Qt.KeyboardModifier.AltModifier, 1), (Qt.KeyboardModifier.ControlModifier, 2),
                      (Qt.KeyboardModifier.MetaModifier, 4), (Qt.KeyboardModifier.ShiftModifier, 8)):
        if mods & flag:
            bits |= bit
    return bits


def latin(key: int) -> str:
    """Латинська літера чи цифра клавіші (у будь-якій розкладці) або ''."""
    char = chr(key).upper() if 0 < key < 0x110000 else ""
    char = CYRILLIC.get(char, char)
    return char if len(char) == 1 and char.isascii() and char.isalnum() else ""


def key_event(key: int, text: str, mods: int) -> dict | None:
    """Параметри Input.dispatchKeyEvent (без type) для натиснутої клавіші, або None — нічого не слати."""
    if key in SPECIAL:
        name, code, vk, typed = SPECIAL[key]
        params = {"key": name, "code": code, "windowsVirtualKeyCode": vk, "modifiers": mods}
        if typed and not mods & (1 | 2 | 4):
            params["text"] = typed
        return params
    letter = latin(key)
    shortcut = bool(mods & (1 | 2 | 4))  # Ctrl/Alt/Meta — це команда (Ctrl+A), а не текст
    vk = ord(letter.upper()) if letter else 0
    code = ("Digit" + letter if letter.isdigit() else "Key" + letter.upper()) if letter else ""
    if shortcut and letter:
        return {"key": letter.lower(), "code": code, "windowsVirtualKeyCode": vk, "modifiers": mods}
    if text and text.isprintable() and not shortcut:
        return {"key": text, "code": code, "text": text, "unmodifiedText": text, "windowsVirtualKeyCode": vk,
                "modifiers": mods}
    return None


class ScreenView(QWidget):
    """Жива картинка вкладки; мишка, колесо й клавіатура йдуть у браузер."""

    frame_ready = Signal()
    copied = Signal(str)

    def __init__(self, screen):
        super().__init__()
        self.screen = screen
        self._lock = threading.Lock()
        self._pending: tuple[bytes, dict] | None = None
        self._image: QImage | None = None
        self._meta: dict = {}
        self._clicks = 1
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setMinimumSize(320, 240)
        self.frame_ready.connect(self.update)
        self.copied.connect(lambda text: QGuiApplication.clipboard().setText(text))
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(150)
        self._resize_timer.timeout.connect(self.report_size)
        screen.on_frame = self._on_frame
        screen.on_copy = self._on_copy

    # --- Від браузера (інший потік) ----------------------------------------------------------

    def _on_frame(self, data: bytes, meta: dict) -> None:
        with self._lock:
            fresh = self._pending is None
            self._pending = (data, meta)
        if fresh:
            self.frame_ready.emit()  # малюємо лише найсвіжіший кадр — старі просто пропускаємо

    def _on_copy(self, text: str) -> None:
        self.copied.emit(text)

    def clear(self) -> None:
        with self._lock:
            self._pending = None
        self._image = None
        self.update()

    # --- Малювання -------------------------------------------------------------------------

    def report_size(self) -> None:
        self.screen.resize(self.width(), self.height(), self.devicePixelRatioF())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._resize_timer.start()

    def _target(self) -> QRectF:
        width = float(self._meta.get("deviceWidth") or (self._image.width() if self._image else self.width()))
        height = float(self._meta.get("deviceHeight") or (self._image.height() if self._image else self.height()))
        scale = min(self.width() / width, self.height() / height) if width and height else 1.0
        if 0.98 < scale < 1.02:
            scale = 1.0  # розмір вкладки вже підігнано під вікно — без розмиття від крихітного масштабу
        drawn_w, drawn_h = width * scale, height * scale
        return QRectF((self.width() - drawn_w) / 2, (self.height() - drawn_h) / 2, drawn_w, drawn_h)

    def paintEvent(self, event) -> None:  # noqa: N802
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            image = QImage.fromData(pending[0], "JPG")
            if not image.isNull():
                self._image, self._meta = image, pending[1]
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.colors()["panel2"])
        if self._image is not None:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(self._target(), self._image)
        painter.end()

    # --- Мишка -----------------------------------------------------------------------------

    def _point(self, position: QPointF) -> tuple[float, float]:
        rect = self._target()
        width = float(self._meta.get("deviceWidth") or rect.width() or 1)
        height = float(self._meta.get("deviceHeight") or rect.height() or 1)
        x = (position.x() - rect.x()) / (rect.width() or 1) * width
        y = (position.y() - rect.y()) / (rect.height() or 1) * height
        return round(max(0.0, min(width - 1, x)), 1), round(max(0.0, min(height - 1, y)), 1)

    def _mouse(self, kind: str, event, button: str | None = None) -> None:
        x, y = self._point(event.position())
        pressed = 0
        for flag, bit in BUTTON_BITS.items():
            if event.buttons() & flag:
                pressed |= bit
        if button is None:  # рух: якщо щось затиснуто — це перетягування цією кнопкою
            button = next((name for flag, name in BUTTONS.items() if event.buttons() & flag), "none")
        self.screen.send("Input.dispatchMouseEvent", {
            "type": kind, "x": x, "y": y, "button": button, "buttons": pressed,
            "clickCount": self._clicks if kind != "mouseMoved" else 0, "modifiers": modifiers(event.modifiers()),
        })

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._clicks = 1
        self._mouse("mousePressed", event, BUTTONS.get(event.button(), "none"))

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self._clicks = 2
        self._mouse("mousePressed", event, BUTTONS.get(event.button(), "none"))

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._mouse("mouseReleased", event, BUTTONS.get(event.button(), "none"))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._mouse("mouseMoved", event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        x, y = self._point(event.position())
        delta = event.pixelDelta()
        if delta.isNull():
            angle = event.angleDelta()
            dx, dy = angle.x() * 100 / 120, angle.y() * 100 / 120  # один «клац» колеса — як у Chrome
        else:
            dx, dy = delta.x(), delta.y()
        self.screen.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": x, "y": y, "deltaX": -dx, "deltaY": -dy, "modifiers": modifiers(event.modifiers()),
        })
        event.accept()

    # --- Клавіатура ----------------------------------------------------------------------------

    def focusNextPrevChild(self, forward: bool) -> bool:  # noqa: N802 — Tab — для сторінки, а не для вікна
        return False

    def event(self, event) -> bool:
        if event.type() == QEvent.Type.ShortcutOverride:
            event.accept()  # усі клавіші — сторінці, поки фокус на ній
            return True
        return super().event(event)

    def _clipboard(self, event) -> bool:
        """Буфер обміну невидимого браузера — свій, тож копіюємо й вставляємо через буфер системи."""
        if event.matches(QKeySequence.StandardKey.Paste) or self._ctrl_letter(event) == "v":
            text = QGuiApplication.clipboard().text()
            if text:
                self.screen.send("Input.insertText", {"text": text})
            return True
        if event.matches(QKeySequence.StandardKey.Copy) or self._ctrl_letter(event) == "c":
            self.screen.copy()
            return True
        if event.matches(QKeySequence.StandardKey.Cut) or self._ctrl_letter(event) == "x":
            self.screen.copy()  # спершу забрати виділене, далі сторінка сама його виріже
        return False

    @staticmethod
    def _ctrl_letter(event) -> str:
        mods = event.modifiers()
        if mods & Qt.KeyboardModifier.ControlModifier and not mods & Qt.KeyboardModifier.AltModifier:
            return latin(event.key()).lower()
        return ""

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._clipboard(event):
            return
        params = key_event(event.key(), event.text(), modifiers(event.modifiers()))
        if params is not None:
            params["autoRepeat"] = event.isAutoRepeat()
            self.screen.send("Input.dispatchKeyEvent", {"type": "keyDown" if "text" in params else "rawKeyDown", **params})

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        if event.isAutoRepeat():
            return
        params = key_event(event.key(), event.text(), modifiers(event.modifiers()))
        if params is not None:
            params.pop("text", None)
            params.pop("unmodifiedText", None)
            self.screen.send("Input.dispatchKeyEvent", {"type": "keyUp", **params})

    def inputMethodEvent(self, event) -> None:  # noqa: N802 — набір через метод введення (акценти, IME)
        if event.commitString():
            self.screen.send("Input.insertText", {"text": event.commitString()})
        event.accept()


class BrowserPage(QWidget):
    """Шапка (що це за браузер і що робити), вкладки й сама сторінка — або пояснення, коли браузер закритий."""

    tabs_changed = Signal(list, int)
    back_to_wizard = Signal()

    def __init__(self, model, bridge):
        super().__init__()
        self.model, self.bridge = model, bridge
        self.screen = bridge.controller.screen
        primary = QGuiApplication.primaryScreen()
        if primary is not None:
            self.screen.scale = primary.devicePixelRatio()  # щоб браузер одразу малював чітко для цього екрана
        self._tabs: list[dict] = []
        self._current = -1
        self._login_open = False
        self._lessons: list[str] = []
        self.wizard_mode = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = w.label("Браузер", "h2", wrap=False)
        self.hint = w.label("", "hint")
        text.addWidget(self.title)
        text.addWidget(self.hint)
        head.addLayout(text, 1)
        self.done = w.button("Скасувати вхід", "", self._close_login)
        self.wizard_back = w.button("← До налаштування", "", self._leave_wizard)
        head.addWidget(self.done)
        head.addWidget(self.wizard_back)
        layout.addLayout(head)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.back = self._tool("←", "Назад", lambda: self.screen.command("back"))
        self.reload = self._tool("⟳", "Оновити сторінку", lambda: self.screen.command("reload"))
        self.tab_bar = QTabBar()
        self.tab_bar.setDocumentMode(True)
        self.tab_bar.setExpanding(False)
        self.tab_bar.setElideMode(Qt.TextElideMode.ElideRight)
        self.tab_bar.setUsesScrollButtons(True)
        self.tab_bar.tabBarClicked.connect(lambda index: self.screen.command("tab", index))
        self.tab_bar.tabCloseRequested.connect(lambda index: self.screen.command("close", index))
        self.host = w.label("", "hint", wrap=False)
        bar.addWidget(self.back)
        bar.addWidget(self.reload)
        bar.addWidget(self.tab_bar, 1)
        bar.addWidget(self.host)
        layout.addLayout(bar)

        self.stack = QStackedWidget()
        self.placeholder = w.label("", "muted")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.view = ScreenView(self.screen)
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(self.view)
        layout.addWidget(self.stack, 1)

        self.tabs_changed.connect(self._show_tabs)
        self.screen.on_tabs = lambda tabs, current: self._emit_tabs(tabs, current)
        self._redraw()

    def _emit_tabs(self, tabs: list, current: int) -> None:
        try:
            self.tabs_changed.emit(tabs, current)
        except RuntimeError:
            pass  # вікно вже закрили

    @staticmethod
    def _tool(text: str, tip: str, action) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoRaise(True)
        button.clicked.connect(lambda *_: action())
        return button

    # --- Видимість: транслюємо, лише коли сторінку видно ---------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.view.report_size()
        self.screen.watch(True)

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        self.screen.watch(False)

    # --- Стан --------------------------------------------------------------------------------

    def _show_tabs(self, tabs: list, current: int) -> None:
        self._tabs, self._current = tabs, current
        while self.tab_bar.count() > len(tabs):
            self.tab_bar.removeTab(self.tab_bar.count() - 1)
        while self.tab_bar.count() < len(tabs):
            self.tab_bar.addTab("")
        self.tab_bar.setTabsClosable(any(tab["closable"] for tab in tabs))
        for index, tab in enumerate(tabs):
            title = tab["title"] or _host(tab["url"]) or "Нова вкладка"
            self.tab_bar.setTabText(index, title if len(title) <= 32 else title[:31] + "…")
            self.tab_bar.setTabToolTip(index, f"{tab['title']}\n{tab['url']}".strip())
            if not tab["closable"]:
                for side in (QTabBar.ButtonPosition.RightSide, QTabBar.ButtonPosition.LeftSide):
                    button = self.tab_bar.tabButton(index, side)
                    if button is not None:
                        button.hide()
        if 0 <= current < len(tabs):
            self.tab_bar.setCurrentIndex(current)
        if not tabs:
            self.view.clear()
        self._redraw()

    def update_state(self, state: dict) -> None:
        self._login_open = bool(state.get("login_open"))
        self._lessons = [s["title"] for s in state.get("active") or [] if s.get("in_browser", True)]
        self._redraw()

    def set_wizard_mode(self, on: bool) -> None:
        self.wizard_mode = on
        self._redraw()

    def _redraw(self) -> None:
        is_open = bool(self._tabs)
        url = self._tabs[self._current]["url"] if 0 <= self._current < len(self._tabs) else ""
        if self._login_open:
            self.title.setText("Вхід у Google")
            self.hint.setText("Увійди в університетську пошту. Програма сама побачить акаунт і закриє вхід — "
                              "робити це треба лише раз.")
        elif self._lessons:
            self.title.setText("Пара: " + ", ".join(self._lessons))
            self.hint.setText("Так програма бачить пару. Можна клацати й писати, як у звичайному браузері — "
                              "мікрофон і камеру програма сама не вмикає.")
        else:
            self.title.setText("Браузер програми")
            self.hint.setText("Тут видно пару, коли програма на неї заходить, і вхід у Google.")
        self.done.setVisible(self._login_open and not self.wizard_mode)  # у майстрі те саме робить «← До налаштування»
        self.wizard_back.setVisible(self.wizard_mode)
        for item in (self.back, self.reload, self.tab_bar, self.host):
            item.setVisible(is_open)
        self.host.setText(_host(url))
        self.placeholder.setText(
            "Браузер програми зараз закритий.\nВін відкриється тут сам, коли почнеться пара."
            if not self._login_open else "Відкриваю сторінку входу…")
        self.stack.setCurrentWidget(self.view if is_open else self.placeholder)

    def _close_login(self) -> None:
        self.done.setEnabled(False)
        self.bridge.command("close_login", ok=lambda _: self.done.setEnabled(True),
                            fail=lambda _: self.done.setEnabled(True))

    def _leave_wizard(self) -> None:
        if self._login_open:
            self._close_login()
        self.back_to_wizard.emit()


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")
