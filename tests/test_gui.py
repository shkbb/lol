"""Вікно програми (Qt) без екрана: майстер, автозбереження, Telegram, розклад, акаунти Google."""

import os
import threading
import time
from datetime import date, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from autopara.app import AppServer, Controller  # noqa: E402
from autopara.config import Config, read_config  # noqa: E402
from autopara.gui import panels  # noqa: E402
from autopara.gui.bridge import Bridge, ConfigModel  # noqa: E402
from autopara.gui.window import MainWindow  # noqa: E402
from autopara.rozklad import RozkladClient  # noqa: E402
from autopara.runner import Runner  # noqa: E402

from .conftest import make_lesson  # noqa: E402
from .test_app import FakeAutostart, FakeTelegram, IdleJoiner, running_app  # noqa: E402,F401
from .test_rozklad import EVENT, FakeSite  # noqa: E402
from .test_runner import FakeClient, FakeGuard  # noqa: E402


@pytest.fixture(scope="module")
def qt():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def wait_until(condition, timeout=10.0):
    """Крутити цикл Qt, доки не станеться те, на що чекаємо (відповідь із фонового потоку тощо)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QtWidgets.QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    raise AssertionError("не дочекався")


WINDOWS = []


@pytest.fixture(autouse=True)
def close_windows():
    yield
    while WINDOWS:
        window = WINDOWS.pop()
        window.poll.stop()
        window.model._timer.stop()
        window._quitting = True
        window.close()
        window.deleteLater()
    QtWidgets.QApplication.processEvents()


def window_for(server, qt):
    bridge = Bridge(server)
    model = ConfigModel(bridge, delay_ms=50)
    window = MainWindow(model, bridge, on_quit=lambda: None)
    WINDOWS.append(window)
    return window, model


def saved_config(path):
    return read_config(path) if path.exists() else None


def test_main_window_sections(running_app, qt):
    server, _, _ = running_app
    window, _ = window_for(server, qt)
    assert window.root.currentWidget() is window.main  # група вже є — одразу програма, без майстра
    labels = [window.nav.item(i).text() for i in range(window.nav.count())]
    assert labels == ["Сьогодні", "Розклад", "Налаштування"] and window.log_button.text() == "Журнал"
    tabs = [window.settings.tabs.tabText(i) for i in range(window.settings.tabs.count())]
    assert tabs == ["Пари", "Акаунти", "Помічник", "Telegram", "Загальне"]
    window.go("settings", "telegram")
    assert window.pages.currentWidget() is window.settings and window.settings.tabs.currentIndex() == 3
    window.go("log")
    assert window.pages.currentWidget() is window.log


def test_settings_save_themselves(running_app, qt, tmp_path):
    """Кнопки «Зберегти» немає: змінив — і за мить це у файлі й у програмі."""
    server, controller, _ = running_app
    window, model = window_for(server, qt)
    model.set("join.minutes_before", 7)
    model.set("me.name", "Ляшко Денис")
    wait_until(lambda: window.save_state.text().startswith("✓"))
    saved = read_config(tmp_path / "config.toml")
    assert saved.join.minutes_before == 7 and saved.me.name == "Ляшко Денис"
    assert controller.config.join.minutes_before == 7

    model.set("links", [{"subject": "Бази", "url": "ftp://"}, {"subject": ""}])  # некоректно — не зберігається
    wait_until(lambda: window.save_state.text().startswith("⚠"))
    assert read_config(tmp_path / "config.toml").links[0].url == "https://meet.google.com/aaa-bbbb-ccc"


def test_first_run_wizard(qt, tmp_path):
    """Друг відкрив програму вперше: група → посилання → Google → Telegram → готово, усе зберігається по ходу."""
    site = FakeSite()
    site.events = [dict(EVENT, date=f"{date.today() + timedelta(days=1):%d.%m.%Y}", disciplineFullName="Бази даних", info="")]
    def parts(stack):
        joiner = IdleJoiner()
        return Runner(controller.config, FakeClient([make_lesson()]), joiner, sleep_guard=FakeGuard()), joiner

    controller = Controller(Config(), tmp_path / "config.toml", runner_parts=parts)
    server = AppServer(controller)
    server.autostart = FakeAutostart()
    server.sources["duikt"] = RozkladClient(session=site)
    controller.start()
    try:
        window, model = window_for(server, qt)
        wizard = window.wizard
        assert window.root.currentWidget() is wizard and not wizard.next.isEnabled()

        group = wizard.stack.widget(0).findChild(panels.GroupPanel)
        group.search.setText("тцр")
        group._find()
        wait_until(lambda: group.results.count() == 2 and group.results.item(0).data(256))
        group._picked(group.results.item(1))
        assert model.get("schedule.group") == "ТЦР-32" and wizard.next.isEnabled()

        wizard._next()  # посилання: предмети з розкладу
        links = wizard.links
        wait_until(lambda: links.list.count() == 1)
        row = links.list.itemAt(0).widget()
        assert row.subject == "Бази даних"
        row.url.setText("meet.google.com/abc-defg-hij")
        row._add()
        wait_until(lambda: saved_config(tmp_path / "config.toml") and saved_config(tmp_path / "config.toml").links)
        assert read_config(tmp_path / "config.toml").links[0].url == "https://meet.google.com/abc-defg-hij"

        wizard._next()  # Google
        wizard._next()  # Telegram — пропускаємо
        assert wizard.skip.isVisible() or wizard.stack.currentIndex() == 3
        wizard._next()  # готово: автозапуск увімкнено
        assert server.autostart.enabled and "заходитиме на пари сама" in wizard.done_text.text()
        wizard._next()
        assert window.root.currentWidget() is window.main
    finally:
        controller.shutdown()


def test_google_accounts_seen_and_university_one_is_main(running_app, qt):
    server, _, _ = running_app
    window, model = window_for(server, qt)
    accounts = window.settings.accounts
    accounts.update_state({"google_accounts": ["denys.personal@gmail.com", "st1234567@stud.duikt.edu.ua"]})
    assert model.get("join.meet_account") == "st1234567@stud.duikt.edu.ua"  # університетський — основний
    assert model.get("join.accounts") == ["denys.personal@gmail.com"]
    assert "Увійдено" in accounts.status.text() and accounts.choice_box.isVisibleTo(accounts)


def test_controller_watches_login_and_remembers_accounts(running_app, tmp_path):
    server, controller, _ = running_app
    controller.config.data_dir = tmp_path / "data"

    class LoginBrowser:
        def is_open(self):
            return True

        def google_accounts(self):
            return ["st1234567@stud.duikt.edu.ua"]

        def close(self):
            pass

    class LoginJoiner:
        browser = LoginBrowser()

        def idle(self, seconds):
            time.sleep(0.05)

        def close(self):
            pass

    controller.joiner = LoginJoiner()
    controller._login_open = True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and controller.snapshot().get("google_accounts") != ["st1234567@stud.duikt.edu.ua"]:
        controller._wake.set()
        time.sleep(0.05)
    assert controller.snapshot()["google_accounts"] == ["st1234567@stud.duikt.edu.ua"]
    from autopara.app import read_accounts

    assert read_accounts(controller.config) == ["st1234567@stud.duikt.edu.ua"]  # видно й без відкритого браузера


def test_telegram_pairs_only_whoever_has_the_code(running_app, qt, tmp_path, monkeypatch):
    from autopara import telegram

    server, _, _ = running_app
    fake = FakeTelegram()
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    monkeypatch.setattr(telegram, "API", f"http://127.0.0.1:{fake.server_address[1]}/bot{{token}}/{{method}}")
    opened = []
    monkeypatch.setattr(panels, "open_url", opened.append)  # не відкривати справжній Telegram
    token = "123456789:AAHfakefakefakefakefakefakefake12345"
    try:
        window, model = window_for(server, qt)
        window.go("settings", "telegram")
        panel = window.settings.tabs.currentWidget().findChild(panels.TelegramPanel)
        panel.token.setText(token)
        panel._token_edited(token)
        panel._check()
        wait_until(lambda: panel.username == "ivan_autopara_bot")
        panel._pair()
        code = panel.code.text()
        assert opened == [f"https://t.me/ivan_autopara_bot?start={code}"]
        with fake.lock:
            fake.updates += [
                {"update_id": 7, "message": {"chat": {"id": 666}, "text": "/start 000000"}},  # чужий, без коду
                {"update_id": 8, "message": {"chat": {"id": 42}, "text": f"/start {code}"}},
            ]
        wait_until(lambda: "Підключено" in panel.pair_state.text(), timeout=15)
        wait_until(lambda: (saved_config(tmp_path / "config.toml") or Config()).telegram.chat_id == 42)
    finally:
        fake.shutdown()
    saved = read_config(tmp_path / "config.toml")
    assert (saved.telegram.token, saved.telegram.chat_id) == (token, 42)


def test_schedule_page_shows_the_week(running_app, qt):
    server, _, _ = running_app
    today = date.today()
    monday = today + timedelta(days=1 if today.weekday() == 6 else -today.weekday())
    server.sources["duikt"].session.events = [dict(EVENT, date=f"{monday:%d.%m.%Y}", disciplineFullName="Бази даних", info="")]
    window, _ = window_for(server, qt)
    page = window.schedule
    window.go("schedule")
    page._load()

    def texts():
        return " ".join(label.text() for label in page.findChildren(QtWidgets.QLabel))

    wait_until(lambda: "Бази даних" in texts())
    assert f"Понеділок, {monday:%d.%m}" in texts() and "Meet" in texts()  # посилання з правила
    page._move(7)
    wait_until(lambda: "Пар немає" in texts() and "Бази даних" not in texts())


def test_show_window_of_the_running_copy(running_app, qt, tmp_path, monkeypatch):
    """Запустили ще раз з ярлика — вже запущена програма просто показує своє вікно."""
    from autopara import app as app_module

    server, _, _ = running_app
    monkeypatch.setattr(app_module, "lock_path", lambda: tmp_path / "app.json")
    lock = app_module.write_lock(server.port, server.token)
    try:
        instance = app_module.running_instance()
        assert app_module.show_running(instance) == "no_window"  # autopara run — без вікна
        shown = []
        server.show_window = lambda: shown.append(1)
        assert app_module.show_running(instance) == "shown" and shown == [1]
    finally:
        app_module.release_lock(lock)
