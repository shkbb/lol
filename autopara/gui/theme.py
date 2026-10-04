"""Вигляд вікна: світла й темна тема (за системною), спокійні кольори, заокруглені картки."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

LIGHT = {
    "bg": "#f4f5f8",
    "panel": "#ffffff",
    "panel2": "#f7f8fa",
    "border": "#e2e5eb",
    "text": "#1c2230",
    "muted": "#6b7280",
    "accent": "#4f66e8",
    "accent_text": "#ffffff",
    "accent_soft": "#e8ecfd",
    "ok": "#15803d",
    "ok_soft": "#e3f5e9",
    "warn": "#b45309",
    "warn_soft": "#fdf1de",
    "bad": "#c62828",
    "bad_soft": "#fde8e8",
}

DARK = {
    "bg": "#15171c",
    "panel": "#1e2128",
    "panel2": "#24272f",
    "border": "#30343e",
    "text": "#e7e9ee",
    "muted": "#9aa1ad",
    "accent": "#7d8cff",
    "accent_text": "#101320",
    "accent_soft": "#2a3050",
    "ok": "#5fd38a",
    "ok_soft": "#1d3326",
    "warn": "#f0b45a",
    "warn_soft": "#3a2f1c",
    "bad": "#ff7b7b",
    "bad_soft": "#3d2224",
}

QSS = """
QWidget { color: {text}; font-size: 14px; }
QMainWindow, #root, #page, QScrollArea, QScrollArea > QWidget > QWidget { background: {bg}; }
QToolTip { background: {panel}; color: {text}; border: 1px solid {border}; padding: 4px 6px; }

#sidebar { background: {panel}; border-right: 1px solid {border}; }
#brand { font-size: 19px; font-weight: 700; }
#brandStatus { color: {muted}; font-size: 13px; }
#nav { background: transparent; border: none; outline: none; }
#nav::item { padding: 9px 12px; margin: 1px 0; border-radius: 8px; }
#nav::item:hover { background: {panel2}; }
#nav::item:selected { background: {accent_soft}; color: {accent}; font-weight: 700; }
#navSmall { color: {muted}; border: none; background: transparent; text-align: left; padding: 6px 12px; }
#navSmall:hover { color: {accent}; }
#navSmall[active="true"] { color: {accent}; font-weight: 700; }

#h1 { font-size: 24px; font-weight: 700; }
#lead { color: {muted}; }
#h2 { font-size: 16px; font-weight: 700; }
#muted, QLabel[muted="true"] { color: {muted}; }
#hint { color: {muted}; font-size: 12.5px; }
#big { font-size: 17px; font-weight: 700; }
#code { font-size: 30px; font-weight: 700; letter-spacing: 6px; color: {accent}; }

#card { background: {panel}; border: 1px solid {border}; border-radius: 14px; }
#row { background: {panel2}; border-radius: 10px; }
#calm { color: {ok}; padding: 4px 2px; }

QLabel[badge="ok"] { color: {ok}; background: {ok_soft}; border-radius: 9px; padding: 2px 9px; font-size: 12.5px; }
QLabel[badge="bad"] { color: {bad}; background: {bad_soft}; border-radius: 9px; padding: 2px 9px; font-size: 12.5px; }
QLabel[badge="warn"] { color: {warn}; background: {warn_soft}; border-radius: 9px; padding: 2px 9px; font-size: 12.5px; }
QLabel[badge="accent"] { color: {accent}; background: {accent_soft}; border-radius: 9px; padding: 2px 9px; font-size: 12.5px; }
QLabel[badge="plain"] { color: {muted}; background: {panel2}; border-radius: 9px; padding: 2px 9px; font-size: 12.5px; }

QLabel[banner="warn"] { color: {warn}; background: {warn_soft}; border-radius: 10px; padding: 10px 12px; }
QLabel[banner="bad"] { color: {bad}; background: {bad_soft}; border-radius: 10px; padding: 10px 12px; }
QLabel[banner="ok"] { color: {ok}; background: {ok_soft}; border-radius: 10px; padding: 10px 12px; }
QLabel[banner="info"] { color: {accent}; background: {accent_soft}; border-radius: 10px; padding: 10px 12px; }

QPushButton { background: {panel}; border: 1px solid {border}; border-radius: 9px; padding: 7px 15px; }
QPushButton:hover { border-color: {accent}; color: {accent}; }
QPushButton:pressed { background: {panel2}; }
QPushButton:disabled { color: {muted}; border-color: {border}; background: {panel2}; }
QPushButton[kind="primary"] { background: {accent}; color: {accent_text}; border: 1px solid {accent}; font-weight: 700; }
QPushButton[kind="primary"]:hover { color: {accent_text}; }
QPushButton[kind="primary"]:disabled { background: {panel2}; color: {muted}; border-color: {border}; }
QPushButton[kind="big"] { background: {accent}; color: {accent_text}; border: 1px solid {accent}; font-weight: 700;
                          font-size: 15px; padding: 11px 22px; border-radius: 11px; }
QPushButton[kind="big"]:hover { color: {accent_text}; }
QPushButton[kind="big"]:disabled { background: {panel2}; color: {muted}; border-color: {border}; }
QPushButton[kind="link"] { background: transparent; border: none; color: {accent}; padding: 2px 4px; }
QPushButton[kind="link"]:hover { text-decoration: underline; }
QPushButton[kind="seg"] { border-radius: 9px; padding: 7px 16px; }
QPushButton[kind="seg"]:checked { background: {accent_soft}; color: {accent}; border-color: {accent}; font-weight: 700; }
QToolButton#more { border: none; color: {accent}; padding: 4px 0; font-weight: 700; background: transparent; }

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {
    background: {panel}; border: 1px solid {border}; border-radius: 9px; padding: 7px 10px;
    selection-background-color: {accent}; selection-color: {accent_text};
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus { border-color: {accent}; }
QComboBox QAbstractItemView { background: {panel}; border: 1px solid {border}; selection-background-color: {accent_soft};
                              selection-color: {text}; outline: none; }
QListWidget#results { background: {panel}; border: 1px solid {border}; border-radius: 10px; outline: none; padding: 4px; }
QListWidget#results::item { padding: 7px 9px; border-radius: 7px; }
QListWidget#results::item:hover, QListWidget#results::item:selected { background: {accent_soft}; color: {text}; }

QRadioButton, QCheckBox { spacing: 9px; }
QRadioButton::indicator, QCheckBox::indicator { width: 17px; height: 17px; }

QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; border: 1px solid transparent; border-radius: 9px; padding: 7px 14px; margin-right: 4px;
               color: {muted}; }
QTabBar::tab:hover { color: {text}; }
QTabBar::tab:selected { background: {panel}; border-color: {border}; color: {text}; font-weight: 700; }

QPlainTextEdit#log { font-family: monospace; font-size: 12.5px; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: {border}; border-radius: 4px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QStatusBar { background: {panel}; border-top: 1px solid {border}; color: {muted}; }
#saveState[state="error"] { color: {bad}; }
#saveState[state="saved"] { color: {ok}; }
"""


def colors() -> dict:
    hints = QGuiApplication.styleHints()
    dark = hasattr(hints, "colorScheme") and hints.colorScheme() == Qt.ColorScheme.Dark
    return DARK if dark else LIGHT


def apply(app: QApplication) -> dict:
    """Застосувати тему (світлу чи темну — як у системі)."""
    c = colors()
    app.setStyle("Fusion")
    palette = QPalette()
    for role, key in (
        (QPalette.ColorRole.Window, "bg"),
        (QPalette.ColorRole.Base, "panel"),
        (QPalette.ColorRole.AlternateBase, "panel2"),
        (QPalette.ColorRole.Button, "panel"),
        (QPalette.ColorRole.Text, "text"),
        (QPalette.ColorRole.WindowText, "text"),
        (QPalette.ColorRole.ButtonText, "text"),
        (QPalette.ColorRole.PlaceholderText, "muted"),
        (QPalette.ColorRole.Highlight, "accent"),
        (QPalette.ColorRole.HighlightedText, "accent_text"),
        (QPalette.ColorRole.ToolTipBase, "panel"),
        (QPalette.ColorRole.ToolTipText, "text"),
    ):
        palette.setColor(role, QColor(c[key]))
    app.setPalette(palette)
    qss = QSS
    for key, value in c.items():
        qss = qss.replace("{" + key + "}", value)
    app.setStyleSheet(qss)
    return c
