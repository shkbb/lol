"""Головне вікно: бічне меню, сторінки, індикатор автозбереження, значок у треї."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import app as app_module
from . import widgets as w
from .browser import BrowserPage
from .pages import LogPage, SchedulePage, TodayPage
from .settings import SettingsPage
from .wizard import Wizard

ICON = Path(__file__).with_name("icon.svg")
PAGES = [("today", "Сьогодні"), ("schedule", "Розклад"), ("browser", "Браузер"), ("settings", "Налаштування")]


def app_icon() -> QIcon:
    return QIcon(str(ICON))


class MainWindow(QMainWindow):
    show_requested = Signal()  # із потоку локального API: «покажи вікно» (запустили ще раз з ярлика)
    quit_requested = Signal()  # з потоку оновлення: «вийди» — нову версію запустить процес оновлення

    def __init__(self, model, bridge, on_quit):
        super().__init__()
        self.model, self.bridge, self.on_quit = model, bridge, on_quit
        self._quitting = False
        self.setWindowTitle("autopara")
        self.setWindowIcon(app_icon())
        self.resize(1080, 760)
        self.setMinimumSize(820, 560)

        self.root = QStackedWidget()
        self.root.setObjectName("root")
        self.setCentralWidget(self.root)

        self.wizard = Wizard(model, bridge)
        self.wizard.finished.connect(self.show_main)
        self.main = QWidget()
        self.main.setObjectName("root")
        self.root.addWidget(self.wizard)
        self.root.addWidget(self.main)
        # Браузер програми — одна сторінка: у меню «Браузер», а під час майстра — на весь екран замість майстра.
        self.browser = BrowserPage(model, bridge)
        self.browser.back_to_wizard.connect(self._back_to_wizard)
        self.browser_slot = self._slot()
        self.wizard_browser = self._slot()
        self.root.addWidget(self.wizard_browser)
        self._login_shown = False  # браузер показано заради входу в Google…
        self._login_seen = False  # …і програма вже підтвердила, що вхід відкрито
        self._return_to = ""
        bridge.browser_wanted.connect(self.show_browser)

        body = QHBoxLayout(self.main)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._sidebar())
        self.pages = QStackedWidget()
        self.today = TodayPage(model, bridge, self.go)
        self.schedule = SchedulePage(model, bridge, self.go)
        self.settings = SettingsPage(model, bridge, self)
        self.log = LogPage(model, bridge)
        self.browser_slot.layout().addWidget(self.browser)
        for page in (self.today, self.schedule, self.browser_slot, self.settings, self.log):
            self.pages.addWidget(page)
        body.addWidget(self.pages, 1)

        self.save_state = QLabel("")
        self.save_state.setObjectName("saveState")
        self.statusBar().addWidget(self.save_state)
        self.statusBar().setSizeGripEnabled(False)
        self._clear_status = QTimer(self)
        self._clear_status.setSingleShot(True)
        self._clear_status.timeout.connect(lambda: self.save_state.setText(""))
        model.status.connect(self.show_status)
        model.autostart_on.connect(lambda: self.show_status("saved", "✓ Програма тепер запускатиметься разом із комп'ютером"))

        self.tray = self._tray()
        self.show_requested.connect(self.bring_up)
        self.quit_requested.connect(self.quit_program)

        self.poll = QTimer(self)
        self.poll.setInterval(1500)
        self.poll.timeout.connect(self.refresh)
        self.poll.start()

        configured = bool(model.get("schedule.group") or model.get("schedule.group_uid"))
        if configured:
            self.show_main()
        else:
            self.show_wizard()
        self.refresh()

    # --- Будова ---------------------------------------------------------------------------

    def _sidebar(self) -> QWidget:
        side = QFrame()
        side.setObjectName("sidebar")
        side.setFixedWidth(210)
        layout = QVBoxLayout(side)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(4)
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(30, 30))
        brand.addWidget(logo)
        brand.addWidget(w.label("autopara", "brand", wrap=False), 1)
        layout.addLayout(brand)
        self.brand_status = w.label("", "brandStatus")
        self.brand_status.setContentsMargins(4, 0, 0, 10)
        layout.addWidget(self.brand_status)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setCursor(Qt.CursorShape.PointingHandCursor)
        for key, title in PAGES:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._nav_changed)
        layout.addWidget(self.nav, 1)
        self.log_button = QPushButton("Журнал")
        self.log_button.setObjectName("navSmall")
        self.log_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_button.clicked.connect(lambda: self.go("log"))
        layout.addWidget(self.log_button)
        return side

    @staticmethod
    def _slot() -> QWidget:
        slot = QWidget()
        slot.setObjectName("root")
        layout = QVBoxLayout(slot)
        layout.setContentsMargins(0, 0, 0, 0)
        return slot

    def _tray(self) -> QSystemTrayIcon | None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None  # напр., GNOME без розширення AppIndicator — тоді вікно відкривають ярликом
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip("autopara")
        menu = QMenu()
        open_action = QAction("Відкрити autopara", menu)
        open_action.triggered.connect(self.bring_up)
        quit_action = QAction("Вийти", menu)
        quit_action.triggered.connect(self.quit_program)
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: reason == QSystemTrayIcon.ActivationReason.Trigger and self.bring_up())
        tray.show()
        self._tray_menu = menu
        return tray

    # --- Навігація ------------------------------------------------------------------------

    def go(self, key: str, tab: str | None = None) -> None:
        keys = [k for k, _ in PAGES]
        if key in keys:
            self.nav.setCurrentRow(keys.index(key))
        else:
            self.nav.clearSelection()
            self.nav.setCurrentRow(-1)
            self.pages.setCurrentWidget(self.log)
        self.log_button.setProperty("active", key == "log")
        w.restyle(self.log_button)
        if key == "settings" and tab:
            self.settings.open_tab(tab)

    def _nav_changed(self, row: int) -> None:
        if row < 0:
            return
        self.pages.setCurrentIndex(row)
        self.log_button.setProperty("active", False)
        w.restyle(self.log_button)

    def current_key(self) -> str:
        row = self.nav.currentRow()
        return PAGES[row][0] if 0 <= row < len(PAGES) and self.pages.currentWidget() is not self.log else "log"

    def show_browser(self) -> None:
        """Відкрили вхід у Google — показати браузер програми: у майстрі — замість майстра, інакше — сторінку «Браузер»."""
        self._login_shown, self._login_seen = True, False
        if self.root.currentWidget() in (self.wizard, self.wizard_browser):
            self.wizard_browser.layout().addWidget(self.browser)
            self.browser.set_wizard_mode(True)
            self.root.setCurrentWidget(self.wizard_browser)
            return
        if self.current_key() != "browser":
            self._return_to = self.current_key()
        self.go("browser")

    def _back_to_wizard(self) -> None:
        self.browser_slot.layout().addWidget(self.browser)
        self.browser.set_wizard_mode(False)
        self.root.setCurrentWidget(self.wizard)

    def _login_finished(self) -> None:
        """Увійшли — повернутися туди, звідки відкривали вхід (майстер чи «Налаштування» → «Акаунти»)."""
        self._login_shown = False
        if self.root.currentWidget() is self.wizard_browser:
            self._back_to_wizard()
        elif self.current_key() == "browser" and self._return_to:
            self.go(self._return_to)
        self._return_to = ""

    def show_wizard(self) -> None:
        self.wizard.go(0)
        self.root.setCurrentWidget(self.wizard)
        self.statusBar().show()

    def show_main(self) -> None:
        if self.browser.wizard_mode:
            self._back_to_wizard()
        self.root.setCurrentWidget(self.main)
        self.go("today")

    # --- Стан -----------------------------------------------------------------------------

    def refresh(self) -> None:
        try:
            state = self.bridge.api(app_module.api_state)
        except Exception:  # noqa: BLE001 — програма саме перезапускається
            return
        self.brand_status.setText("● працює" if state.get("running") else "○ вимкнена")
        self.brand_status.setStyleSheet(f"color: {'#16a34a' if state.get('running') else ''};")
        if self.tray is not None:
            self.tray.setToolTip("autopara — працює" if state.get("running") else "autopara — вимкнена")
        self.today.update_state(state)
        self.settings.update_state(state)
        self.wizard.update_state(state)
        self.browser.update_state(state)
        if state.get("login_open"):
            self._login_seen = True
        elif self._login_shown and self._login_seen:
            self._login_finished()

    def show_status(self, state: str, text: str) -> None:
        self.save_state.setText(text)
        self.save_state.setProperty("state", state)
        w.restyle(self.save_state)
        if state == "saved":
            self._clear_status.start(4000)
        else:
            self._clear_status.stop()

    # --- Вікно й вихід ----------------------------------------------------------------------

    def bring_up(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.model.save_now(wait=True)
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()  # програма працює далі у фоні — заходитиме на пари сама
        settings = QSettings("autopara", "autopara")
        if not settings.value("background_hint_shown", False, bool):
            settings.setValue("background_hint_shown", True)
            where = "значок autopara біля годинника" if self.tray is not None else "ярлик autopara в меню програм"
            if self.tray is not None:
                self.tray.showMessage("autopara працює далі", f"Програма заходитиме на пари сама. Відкрити вікно — {where}.",
                                      app_icon(), 6000)
            else:
                QMessageBox.information(None, "autopara працює далі",
                                        f"Програма працює у фоні й заходитиме на пари сама.\nВідкрити вікно знову — {where}.")

    def quit_program(self) -> None:
        self.model.save_now(wait=True)
        self._quitting = True
        self.close()
        self.on_quit()
        QApplication.quit()
