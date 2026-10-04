"""Вікно програми — звичайна програма для Linux, Windows і macOS (Qt), без браузера.

Один процес: фонова частина (``app.Program``) і вікно. Закрили вікно — програма працює далі у фоні
й заходить на пари; ще раз відкрили з ярлика — показується те саме вікно, друга копія не запускається.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from pathlib import Path

from ..config import Config

log = logging.getLogger(__name__)


def run(config: Config, config_path: Path, hidden: bool = False) -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    from ..app import Program, configured
    from . import theme
    from .bridge import Bridge, ConfigModel
    from .window import MainWindow, app_icon

    app = QApplication(sys.argv[:1])
    app.setApplicationName("autopara")
    app.setDesktopFileName("autopara")  # Wayland: значок і назва беруться з ярлика autopara.desktop
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)  # закрили вікно — програма працює далі
    theme.apply(app)
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda *_: theme.apply(app))

    program = Program(config, config_path)
    program.start()
    threading.Thread(target=program.server.serve_forever, name="local-api", daemon=True).start()
    stopped = threading.Event()

    def stop() -> None:
        if stopped.is_set():
            return
        stopped.set()
        program.server.shutdown()
        program.stop()

    bridge = Bridge(program.server)
    model = ConfigModel(bridge)
    window = MainWindow(model, bridge, on_quit=stop)
    program.server.show_window = window.show_requested.emit
    if not hidden or not configured(config):
        window.show()

    # kill / вимкнення комп'ютера шлють SIGTERM: виходимо як слід — закриваємо браузер, знімаємо заборону сну.
    signal.signal(signal.SIGTERM, lambda *_: QTimer.singleShot(0, window.quit_program))
    signal.signal(signal.SIGINT, lambda *_: QTimer.singleShot(0, window.quit_program))
    wake = QTimer()
    wake.start(400)  # щоб Python встигав обробляти сигнали, поки працює цикл Qt
    wake.timeout.connect(lambda: None)
    try:
        app.exec()
    finally:
        stop()
