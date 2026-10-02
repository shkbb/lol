import json
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import pytest

from autopara import app as app_module
from autopara.app import AppServer, Controller
from autopara.config import Config, ScheduleSettings, read_config
from autopara.links import LinkRule
from autopara.rozklad import RozkladClient
from autopara.runner import Runner

from .conftest import make_lesson
from .test_config import PROFILE
from .test_rozklad import EVENT, FakeSite
from .test_runner import FakeClient, FakeGuard, FakeJoiner

TZ = ZoneInfo("Europe/Kyiv")


class IdleJoiner(FakeJoiner):
    def __init__(self):
        super().__init__()
        self.closed = False
        self.settings = None

    def idle(self, seconds):
        time.sleep(min(seconds, 0.05))

    def close(self):
        self.closed = True


@pytest.fixture
def running_app(tmp_path):
    clock = lambda: datetime.fromisoformat("2026-10-05T08:00").replace(tzinfo=TZ)  # noqa: E731
    config = Config(schedule=ScheduleSettings(group="ТЦР-32"), links=[LinkRule(subject="Бази", url="https://meet.google.com/aaa-bbbb-ccc")])
    joiners = []

    def parts(stack):
        joiner = IdleJoiner()
        joiners.append(joiner)
        runner = Runner(controller.config, FakeClient([make_lesson()]), joiner, clock=clock, sleep_guard=FakeGuard())
        return runner, joiner

    controller = Controller(config, tmp_path / "config.toml", clock=clock, runner_parts=parts)
    server = AppServer(controller)
    site = FakeSite()
    site.events = [dict(EVENT, date="05.10.2026", disciplineFullName="Бази даних", info="")]
    server.sources["duikt"] = RozkladClient(session=site)
    controller.start()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, controller, joiners
    server.shutdown()
    controller.shutdown()


def call(server, method, path, body=None, token=True, host=None):
    request = Request(f"http://127.0.0.1:{server.port}{path}", method=method)
    if token:
        request.add_header("X-Token", server.token)
    if host:
        request.add_header("Host", host)
    data = json.dumps(body).encode() if body is not None else None
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, data, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_api_needs_token_and_local_host(running_app):
    server, _, _ = running_app
    assert call(server, "GET", "/api/state", token=False)[0] == 403
    assert call(server, "GET", "/api/state", host="evil.example:80")[0] == 403
    assert call(server, "GET", "/api/state")[0] == 200


def test_start_stop_and_state(running_app):
    server, controller, joiners = running_app
    status, state = call(server, "GET", "/api/state")
    assert state["running"] is False and state["configured"] is True
    assert call(server, "POST", "/api/start")[1]["result"] == "started"
    for _ in range(50):
        state = call(server, "GET", "/api/state")[1]
        if state.get("upcoming"):
            break
        time.sleep(0.05)
    assert state["running"] and state["upcoming"][0]["title"] == "Бази даних"
    assert call(server, "POST", "/api/stop")[1]["result"] == "stopped"
    assert joiners[0].closed


def test_config_roundtrip_and_validation(running_app):
    server, controller, _ = running_app
    status, data = call(server, "GET", "/api/config")
    config = data["config"]
    assert config["schedule"]["group"] == "ТЦР-32" and data["universities"]["kpi"].startswith("КПІ")
    config["join"]["minutes_before"] = 5
    config["links"].append({"subject": "Мережі", "type": "Лк", "url": "meet.google.com/xxx-yyyy-zzz"})
    status, saved = call(server, "PUT", "/api/config", {"config": config})
    assert status == 200 and saved["result"] == "started"  # група є, а вхід ще не вмикали — вмикаємо
    on_disk = read_config(controller.config_path)
    assert on_disk.join.minutes_before == 5 and on_disk.links[-1].url == "https://meet.google.com/xxx-yyyy-zzz"
    config["join"]["zoom_mode"] = "web"
    status, error = call(server, "PUT", "/api/config", {"config": config})
    assert status == 400 and "zoom_mode" in error["error"]


def test_saving_respects_the_off_switch(running_app):
    server, controller, _ = running_app
    config = call(server, "GET", "/api/config")[1]["config"]
    config["schedule"] = {}
    assert call(server, "PUT", "/api/config", {"config": config})[1]["result"] == "saved"  # групи ще немає
    config["schedule"] = {"group": "ТЦР-32"}
    assert call(server, "PUT", "/api/config", {"config": config})[1]["result"] == "started"
    assert call(server, "POST", "/api/stop")[1]["result"] == "stopped"
    config["join"]["minutes_before"] = 4
    assert call(server, "PUT", "/api/config", {"config": config})[1]["result"] == "saved"  # вимкнули кнопкою — не вмикаємо
    assert controller.snapshot()["running"] is False


def test_import_profile_profile(running_app):
    server, controller, _ = running_app
    status, data = call(server, "GET", "/api/config")
    assert "profile" in data["profiles"]
    status, imported = call(server, "POST", "/api/import", {"profile": "profile"})
    assert status == 200 and controller.config.join.meet_account == "st1234567@stud.duikt.edu.ua"


def test_import_rejects_garbage(running_app):
    server, _, _ = running_app
    status, error = call(server, "POST", "/api/import", {"toml": "це не toml ["})
    assert status == 400


def test_group_search_and_lessons(running_app):
    server, _, _ = running_app
    status, data = call(server, "GET", "/api/groups?q=%D1%82%D1%86%D1%80&university=duikt")  # «тцр»
    assert [g["name"] for g in data["groups"]] == ["ТЦР-31", "ТЦР-32"]
    status, data = call(server, "GET", "/api/lessons?days=3")
    assert status == 200 and data["lessons"][0]["subject"] == "Бази даних"
    assert data["lessons"][0]["url"] == "https://meet.google.com/aaa-bbbb-ccc"


def test_live_reload_without_restart(running_app):
    server, controller, joiners = running_app
    call(server, "POST", "/api/start")
    config = call(server, "GET", "/api/config")[1]["config"]
    config["me"] = {"name": "Ляшко Денис"}
    status, saved = call(server, "PUT", "/api/config", {"config": config})
    assert saved["result"] == "applied" and len(joiners) == 1  # браузер не перезапускали


def test_window_edits_and_saves_settings(running_app, tmp_path):
    """Справжнє вікно в браузері: відкрити з токеном, змінити налаштування, зберегти — і воно у файлі."""
    sync_api = pytest.importorskip("playwright.sync_api")
    server, controller, _ = running_app
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(server.url)
            page.get_by_role("heading", name="Головна").wait_for()

            page.click("#nav button[data-page=group]")
            assert page.get_by_label("Група").first.input_value() == "ТЦР-32"

            page.click("#nav button[data-page=behaviour]")
            assert page.locator("#savebar").is_hidden()
            page.get_by_label("За скільки хвилин до пари заходити").fill("7")
            page.locator("#savebar").wait_for(state="visible")
            page.click("#save")
            page.get_by_text("Збережено").first.wait_for()
            assert page.locator("#savebar").is_hidden()
            assert errors == []
        finally:
            browser.close()
    assert read_config(tmp_path / "config.toml").join.minutes_before == 7
    assert controller.config.join.minutes_before == 7


def test_second_copy_finds_the_running_one(running_app, tmp_path, monkeypatch):
    """Два «бігуни» заходили б на пару двічі — тому `autopara run` бачить уже запущену копію."""
    from autopara import cli

    server, controller, _ = running_app
    monkeypatch.setattr(app_module, "lock_path", lambda: tmp_path / "app.json")
    assert app_module.running_instance() is None
    lock = app_module.write_lock(server.port, server.token)
    instance = app_module.running_instance()
    assert instance["url"] == server.url
    with pytest.raises(cli.ConfigError, match="вже працює"):
        cli.cmd_run(cli.build_parser().parse_args(["run"]))

    # import-config при запущеній програмі передає їй налаштування одразу.
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "imported.toml"
    cli.cmd_import_config(cli.build_parser().parse_args(["-c", str(target), "import-config", str(PROFILE)]))
    assert controller.config.join.meet_account == "st1234567@stud.duikt.edu.ua"

    app_module.release_lock(lock)
    assert not lock.exists()
    lock.write_text(json.dumps({"port": server.port, "token": "stale-token", "pid": 1}), encoding="utf-8")
    assert app_module.running_instance() is None  # застарілий файл не плутаємо з живою програмою


class FakeTelegram(ThreadingHTTPServer):
    """Імітація api.telegram.org: getMe, getUpdates з черги, sendMessage."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeTelegramHandler)
        self.updates = []
        self.sent = []
        self.lock = threading.Lock()


class FakeTelegramHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        method = self.path.rsplit("/", 1)[1]
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        params = json.loads(body or b"{}")
        if method == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Мій autopara", "username": "ivan_autopara_bot"}
        elif method == "getUpdates":
            with self.server.lock:
                result = [u for u in self.server.updates if u["update_id"] >= params.get("offset", 0)]
            if not result and params.get("timeout"):
                time.sleep(0.2)
        else:
            self.server.sent.append((method, params))
            result = {}
        data = json.dumps({"ok": True, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_telegram_wizard_pairs_only_whoever_has_the_code(running_app, tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    from autopara import telegram

    server, controller, _ = running_app
    fake = FakeTelegram()
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    monkeypatch.setattr(telegram, "API", f"http://127.0.0.1:{fake.server_address[1]}/bot{{token}}/{{method}}")
    token = "123456789:AAHfakefakefakefakefakefakefake12345"
    try:
        with sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                page.route("https://t.me/**", lambda route: route.fulfill(body="telegram"))
                page.goto(server.url)
                page.click("#nav button[data-page=telegram]")
                page.get_by_placeholder("123456789:AAH…").fill(token)
                page.get_by_role("button", name="Перевірити").click()
                page.get_by_text("@ivan_autopara_bot").first.wait_for()
                page.get_by_role("button", name="Підключити").click()
                code = page.locator(".code").inner_text()
                with fake.lock:
                    fake.updates += [
                        {"update_id": 7, "message": {"chat": {"id": 666}, "text": "/start 000000"}},  # чужий, без коду
                        {"update_id": 8, "message": {"chat": {"id": 42}, "text": f"/start {code}"}},
                    ]
                page.get_by_text("Підключено!").wait_for(timeout=15000)
            finally:
                browser.close()
    finally:
        fake.shutdown()
    saved = read_config(tmp_path / "config.toml")
    assert (saved.telegram.token, saved.telegram.chat_id) == (token, 42)
    assert any(params.get("chat_id") == 42 and params["text"].startswith("👋 Готово") for _, params in fake.sent)


def test_timetable_tab_shows_the_week(running_app):
    sync_api = pytest.importorskip("playwright.sync_api")
    server, controller, _ = running_app
    today = date.today()
    monday = today + timedelta(days=1 if today.weekday() == 6 else -today.weekday())
    site = server.sources["duikt"].session
    site.events = [dict(EVENT, date=f"{monday:%d.%m.%Y}", disciplineFullName="Бази даних", info="")]
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(server.url)
            page.click("#nav button[data-page=timetable]")
            card = page.locator(".days .card").first
            card.get_by_text("Бази даних").wait_for()
            assert f"Понеділок, {monday:%d.%m}" in card.inner_text()
            assert "Meet" in card.inner_text()  # посилання з правила
            page.get_by_role("button", name="→").click()
            page.get_by_text("Пар немає").first.wait_for()
            assert page.get_by_text("Бази даних").count() == 0
        finally:
            browser.close()
