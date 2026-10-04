"""Цеглинки вікна: сторінка з прокруткою, картки, перемикачі й поля, що самі зберігаються."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QSize, Qt, Property, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme


def label(text: str = "", name: str = "", wrap: bool = True, rich: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    if rich:
        widget.setTextFormat(Qt.TextFormat.RichText)
        widget.setOpenExternalLinks(True)
    else:
        widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def badge(text: str, kind: str = "plain") -> QLabel:
    widget = QLabel(text)
    widget.setProperty("badge", kind)
    widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


def banner(text: str, kind: str = "info") -> QLabel:
    widget = QLabel(text)
    widget.setProperty("banner", kind)
    widget.setWordWrap(True)
    return widget


def restyle(widget: QWidget) -> None:
    """Після зміни властивості, від якої залежить стиль (badge, kind…)."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def button(text: str, kind: str = "", on_click: Callable | None = None) -> QPushButton:
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if kind:
        widget.setProperty("kind", kind)
    if on_click is not None:
        widget.clicked.connect(lambda *_: on_click())
    return widget


def hbox(*items, spacing: int = 10, stretch_at: int | None = None) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for item in items:
        if item is None:
            continue
        if item == "stretch":
            layout.addStretch(1)
        elif isinstance(item, QLayout):
            layout.addLayout(item)
        else:
            layout.addWidget(item)
    return layout


def clear(layout: QLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)  # одразу з вікна, а не «колись потім» — без миготіння старого
            widget.deleteLater()
        elif item.layout() is not None:
            clear(item.layout())


class Page(QScrollArea):
    """Сторінка: прокрутка, вміст по центру, не ширше ~780 px — читати зручно й на широкому екрані."""

    def __init__(self, title: str = "", lead: str = "", width: int = 780, margins: tuple = (28, 24, 28, 28)):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        holder = QWidget()
        holder.setObjectName("page")
        outer = QHBoxLayout(holder)
        outer.setContentsMargins(*margins)
        column = QWidget()
        column.setMaximumWidth(width)
        self.body = QVBoxLayout(column)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(14)
        outer.addStretch(0)
        outer.addWidget(column, 1)
        outer.addStretch(0)
        self.setWidget(holder)
        if title:
            self.body.addWidget(label(title, "h1"))
        if lead:
            self.body.addWidget(label(lead, "lead"))
        self._end = None

    def add(self, item) -> None:
        if self._end is not None:
            self.body.removeItem(self._end)
        if isinstance(item, QLayout):
            self.body.addLayout(item)
        else:
            self.body.addWidget(item)
        self.body.addStretch(1)
        self._end = self.body.itemAt(self.body.count() - 1)


class Card(QFrame):
    def __init__(self, title: str = "", hint: str = ""):
        super().__init__()
        self.setObjectName("card")
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(20, 18, 20, 18)
        self.layout_.setSpacing(10)
        if title:
            self.layout_.addWidget(label(title, "h2"))
        if hint:
            self.layout_.addWidget(label(hint, "muted"))

    def add(self, item, stretch: int = 0):
        if isinstance(item, QLayout):
            self.layout_.addLayout(item, stretch)
        else:
            self.layout_.addWidget(item, stretch)
        return item


class More(QWidget):
    """«Більше налаштувань» — рідкісне сховано, на екрані лише головне."""

    def __init__(self, title: str = "Більше налаштувань"):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.toggle = QToolButton()
        self.toggle.setObjectName("more")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.toggled.connect(self._toggled)
        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 4, 0, 0)
        self.body.setSpacing(12)
        self.content.hide()
        layout.addWidget(self.toggle)
        layout.addWidget(self.content)

    def _toggled(self, on: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.content.setVisible(on)

    def add(self, item):
        if isinstance(item, QLayout):
            self.body.addLayout(item)
        else:
            self.body.addWidget(item)
        return item


class Switch(QAbstractButton):
    """Перемикач «увімкнено / вимкнено» — зрозуміліший за квадратну галочку."""

    def __init__(self, checked: bool = False):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(QSize(44, 26))
        self._offset = 1.0 if checked else 0.0
        self._animation = QPropertyAnimation(self, b"offset", self)
        self._animation.setDuration(140)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self.toggled.connect(self._animate)

    def _animate(self, on: bool) -> None:
        self._animation.stop()
        self._animation.setEndValue(1.0 if on else 0.0)
        self._animation.start()

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def setChecked(self, on: bool) -> None:  # noqa: N802 — так у Qt
        super().setChecked(on)
        self._offset = 1.0 if on else 0.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        c = theme.colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QColor(c["accent"]) if self.isChecked() else QColor(c["border"])
        if not self.isEnabled():
            track.setAlpha(110)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRect(0, 0, 44, 26), 13, 13)
        painter.setBrush(QColor("#ffffff"))
        x = 3 + int(18 * self._offset)
        painter.drawEllipse(QRect(x, 3, 20, 20))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(44, 26)


class SwitchRow(QWidget):
    """Назва, пояснення під нею і перемикач праворуч."""

    toggled = Signal(bool)

    def __init__(self, title: str, hint: str = "", checked: bool = False):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(14)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = label(title)
        self.title.setStyleSheet("font-weight: 700;")
        text.addWidget(self.title)
        if hint:
            text.addWidget(label(hint, "hint"))
        layout.addLayout(text, 1)
        self.switch = Switch(checked)
        self.switch.toggled.connect(self.toggled)
        layout.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignTop)
        self.title.mousePressEvent = lambda event: self.switch.toggle()  # клік по назві теж перемикає


def field(title: str, control: QWidget, hint: str = "") -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    caption = label(title)
    caption.setStyleSheet("font-weight: 700;")
    layout.addWidget(caption)
    layout.addWidget(control)
    if hint:
        layout.addWidget(label(hint, "hint"))
    return box


# --- Поля, прив'язані до налаштувань (зберігаються самі) -----------------------------


def text_field(model, path: str, title: str, hint: str = "", placeholder: str = "", password: bool = False) -> QWidget:
    edit = QLineEdit(str(model.get(path) or ""))
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(not password)
    if password:
        edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.textEdited.connect(lambda text: model.set(path, text.strip() or None))
    widget = field(title, edit, hint)
    widget.edit = edit
    return widget


def number_field(model, path: str, title: str, hint: str = "", minimum: int = 0, maximum: int = 999, suffix: str = "") -> QWidget:
    spin = QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(int(model.get(path) or 0))
    if suffix:
        spin.setSuffix(suffix)
    spin.setMaximumWidth(140)
    spin.valueChanged.connect(lambda value: model.set(path, int(value)))
    return field(title, spin, hint)


def switch_row(model, path: str, title: str, hint: str = "", default: bool = True,
               on_change: Callable[[bool], None] | None = None) -> SwitchRow:
    row = SwitchRow(title, hint, bool(model.get(path, default)))

    def changed(on: bool) -> None:
        model.set(path, on)
        if on_change is not None:
            on_change(on)

    row.toggled.connect(changed)
    return row


def choice_field(model, path: str, options: list[tuple[str, str, str]], default: str = "") -> QWidget:
    """Варіанти радіокнопками: (значення, назва, пояснення)."""
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    group = QButtonGroup(box)
    current = model.get(path, default)
    for value, title, hint in options:
        radio = QRadioButton(title)
        radio.setChecked(value == current)
        radio.setStyleSheet("font-weight: 700;")
        radio.toggled.connect(lambda on, value=value: on and model.set(path, value))
        group.addButton(radio)
        layout.addWidget(radio)
        if hint:
            note = label(hint, "hint")
            note.setContentsMargins(28, 0, 0, 2)
            layout.addWidget(note)
    box.group = group
    return box


def select_field(model, path: str, title: str, options: list[tuple[str, str]], hint: str = "", default: str = "") -> QWidget:
    combo = QComboBox()
    current = model.get(path, default) or ""
    for value, text in options:
        combo.addItem(text, value)
    index = combo.findData(current)
    combo.setCurrentIndex(index if index >= 0 else 0)
    combo.currentIndexChanged.connect(lambda _: model.set(path, combo.currentData() or None))
    return field(title, combo, hint)


class FlowLayout(QLayout):
    """Елементи в рядок з перенесенням — для «чипів» зі списками."""

    def __init__(self, spacing: int = 6):
        super().__init__()
        self._items = []
        self.setSpacing(spacing)
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._place(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._place(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect: QRect, apply: bool) -> int:
        x, y, line = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() and line > 0:
                x, y, line = rect.x(), y + line + self.spacing(), 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self.spacing()
            line = max(line, hint.height())
        return y + line - rect.y()


class ChipsField(QWidget):
    """Список коротких значень (пошти, готові відповіді): «чипи» з ✕ і поле «додати й Enter»."""

    def __init__(self, model, path: str, placeholder: str):
        super().__init__()
        self.model, self.path = model, path
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.chips = QWidget()
        self.flow = FlowLayout()
        self.chips.setLayout(self.flow)
        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        self.input.returnPressed.connect(self._add)
        layout.addWidget(self.chips)
        layout.addWidget(self.input)
        self._draw()

    def values(self) -> list:
        return list(self.model.get(self.path) or [])

    def _add(self) -> None:
        text = self.input.text().strip()
        if text and text not in self.values():
            self.model.set(self.path, self.values() + [text])
            self._draw()
        self.input.clear()

    def _remove(self, value: str) -> None:
        self.model.set(self.path, [item for item in self.values() if item != value])
        self._draw()

    def _draw(self) -> None:
        clear(self.flow)
        for value in self.values():
            chip = QPushButton(f"{value}  ✕")
            chip.setToolTip("Прибрати")
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.clicked.connect(lambda *_, value=value: self._remove(value))
            self.flow.addWidget(chip)
        self.chips.setVisible(bool(self.values()))
        self.chips.updateGeometry()
