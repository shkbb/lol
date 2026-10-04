"""Зв'язок вікна з програмою й автозбереження налаштувань.

Вікно викликає ті самі функції, що й локальний API (``app.api_*``), але напряму, без HTTP.
Повільне (сайт розкладу, Telegram, команди контролера) — у фоновому потоці, а результат
повертається в потік вікна: Qt-віджети можна чіпати лише звідти.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .. import app as app_module
from ..config import ConfigError
from ..rozklad import RozkladError

log = logging.getLogger(__name__)


class ApiError(Exception):
    pass


class _Relay(QObject):
    """Живе в потоці вікна: функції, передані з фонового потоку, виконуються вже тут."""

    deliver = Signal(object)

    def __init__(self):
        super().__init__()
        self.deliver.connect(self._run)

    @staticmethod
    def _run(callback: Callable[[], None]) -> None:
        try:
            callback()
        except RuntimeError as e:  # віджет, що чекав відповіді, уже закрили
            log.debug("Відповідь для закритого віджета: %s", e)


class _Task:
    """Фонова дія вікна. Звичайний потік-демон: на виході з програми його не чекаємо
    (пул потоків Qt чекав би, поки допрацює, наприклад, 20-секундне завантаження списку груп)."""

    def __init__(self, work, ok, fail, relay):
        self.work, self.ok, self.fail, self.relay = work, ok, fail, relay

    def run(self) -> None:
        try:
            result = self.work()
        except Exception as e:  # noqa: BLE001 — будь-яку помилку показуємо людині, а не валимо вікно
            message = str(e) or e.__class__.__name__
            if not isinstance(e, (ApiError, ConfigError, RozkladError)):
                log.exception("Вікно: помилка у фоновій дії")
            if self.fail is not None:
                self._deliver(lambda: self.fail(message))
            return
        if self.ok is not None:
            self._deliver(lambda: self.ok(result))

    def _deliver(self, callback) -> None:
        try:
            self.relay.deliver.emit(callback)
        except RuntimeError:
            pass  # вікно вже закрили — відповідь нікому не потрібна


class Bridge(QObject):
    """Доступ вікна до програми: ``api()`` — одразу, ``run()`` — у фоні."""

    def __init__(self, server):
        super().__init__()
        self.server = server
        self.relay = _Relay()

    @property
    def controller(self):
        return self.server.controller

    def api(self, handler: Callable, query: dict | None = None, body: dict | None = None) -> dict:
        try:
            status, data = handler(self.server, query or {}, body or {})
        except (ConfigError, RozkladError) as e:
            raise ApiError(str(e)) from e
        if status >= 400:
            raise ApiError(data.get("error") or f"помилка {status}")
        return data

    def run(self, work: Callable[[], Any], ok: Callable[[Any], None] | None = None,
            fail: Callable[[str], None] | None = None) -> None:
        threading.Thread(target=_Task(work, ok, fail, self.relay).run, name="window-task", daemon=True).start()

    def call_api(self, handler, query=None, body=None, ok=None, fail=None) -> None:
        self.run(lambda: self.api(handler, query, body), ok, fail)

    def command(self, name: str, *args, ok=None, fail=None, timeout: float = 120) -> None:
        """Команда контролеру (вхід у Google, увімкнути, вийти з пари…) — у фоні."""
        self.run(lambda: self.controller.call(name, *args, timeout=timeout), ok, fail)


SAVE_MESSAGES = {
    "after_lesson": "✓ Збережено — застосую, щойно скінчиться пара",
    "started": "✓ Збережено — програма увімкнулась",
    "restarted": "✓ Збережено",
    "applied": "✓ Збережено",
    "saved": "✓ Збережено",
}


class ConfigModel(QObject):
    """Налаштування у вікні: кожна зміна зберігається сама через секунду тиші — кнопки «Зберегти» немає."""

    status = Signal(str, str)  # state: saving | saved | error, текст для індикатора
    replaced = Signal()  # налаштування замінили цілком (імпорт) — сторінки мають перемалюватись
    autostart_on = Signal()  # уперше налаштували — автозапуск увімкнено
    saved_ok = Signal()  # програма вже працює з новими налаштуваннями (розклад, посилання)

    def __init__(self, bridge: Bridge, delay_ms: int = 1000):
        super().__init__()
        self.bridge = bridge
        meta = bridge.api(app_module.api_get_config)
        self.data: dict = meta.pop("config")
        self.meta = meta
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self.save_now)
        self._saving = False
        self._dirty = False

    # --- Читання й зміна -----------------------------------------------------------

    def get(self, path: str, default: Any = None) -> Any:
        value: Any = self.data
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    def set(self, path: str, value: Any) -> None:
        keys = path.split(".")
        target = self.data
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        if value is None:
            if target.pop(keys[-1], None) is None:
                return
        elif target.get(keys[-1]) == value:
            return
        else:
            target[keys[-1]] = value
        self.touch()

    def touch(self) -> None:
        """Щось змінилось (у тому числі всередині списку) — зберегти за секунду."""
        self._dirty = True
        self.status.emit("saving", "Зберігаю…")
        self._timer.start()

    @property
    def links(self) -> list:
        return self.data.setdefault("links", [])

    # --- Збереження -----------------------------------------------------------------

    def save_now(self, wait: bool = False) -> None:
        """Зберегти зараз. ``wait`` — дочекатися (перед закриттям вікна)."""
        self._timer.stop()
        if wait:
            self._wait_for_save()
        if not self._dirty:
            return
        if wait:
            try:
                self._saved(self.bridge.api(app_module.api_put_config, body={"config": copy.deepcopy(self.data)}))
            except Exception as e:  # noqa: BLE001
                log.error("Не вдалося зберегти налаштування: %s", e)
            return
        if self._saving:
            return  # збережемо, щойно скінчиться поточне збереження
        self._saving = True
        self._dirty = False
        snapshot = copy.deepcopy(self.data)
        self.bridge.call_api(app_module.api_put_config, body={"config": snapshot}, ok=self._saved, fail=self._failed)

    def _wait_for_save(self, seconds: float = 30) -> None:
        """Дочекатися збереження, що вже йде у фоні (крок майстра спирається на збережене)."""
        from PySide6.QtCore import QCoreApplication

        deadline = time.monotonic() + seconds
        while self._saving and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.02)

    def _saved(self, result: dict) -> None:
        self._saving = False
        if self._dirty:
            self._timer.start()
            return
        self.status.emit("saved", SAVE_MESSAGES.get(result.get("result"), "✓ Збережено"))
        self.saved_ok.emit()
        if result.get("autostart"):
            self.autostart_on.emit()

    def _failed(self, message: str) -> None:
        self._saving = False
        self._dirty = True  # спробуємо ще раз, щойно щось виправлять
        self.status.emit("error", f"⚠ Не збережено: {message}")

    def replace(self, data: dict) -> None:
        """Налаштування замінили цілком (імпорт профілю чи файла) — вони вже збережені програмою."""
        self._timer.stop()
        self._dirty = False
        self.data = data
        self.meta = {**self.meta, **{k: v for k, v in self.bridge.api(app_module.api_get_config).items() if k != "config"}}
        self.replaced.emit()

    def reload_meta(self) -> None:
        self.meta.update({k: v for k, v in self.bridge.api(app_module.api_get_config).items() if k != "config"})
