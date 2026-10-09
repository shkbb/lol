"""Фонова частина програми.

- ``Controller`` — серце: розклад, браузер і помічник живуть в одному потоці (так вимагає Playwright),
  а вікно програми спілкується з ним через чергу команд.
- ``api_*`` — що вікно може дізнатися чи змінити. Вікно (``gui``) викликає їх напряму.
- ``AppServer`` — той самий API для інших копій програми (лише 127.0.0.1, з випадковим токеном і
  перевіркою Host): ``autopara app`` показує вікно вже запущеної програми, ``import-config`` передає
  їй налаштування.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import tomllib
import urllib.request
from collections import deque
from concurrent.futures import Future
from contextlib import ExitStack
from dataclasses import astuple, replace
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from . import autostart, browser_setup
from .checks import run_checks
from .config import UNIVERSITIES, Config, ConfigError, config_to_dict, parse_config, save_config, user_config_dir
from .ears import whisper_available
from .links import platform_of, resolve
from .planner import Visit
from .rozklad import Lesson, RozkladError
from .screen import Screen
from .sources import make_source, resolve_target

log = logging.getLogger(__name__)

PROFILES = Path(__file__).resolve().parent.parent / "profiles"  # профілі з репозиторію (якщо є)
POLL_SECONDS = 20
GOOGLE_LOGIN = "https://accounts.google.com/ServiceLogin?hl=uk"
GOOGLE_ADD_ACCOUNT = "https://accounts.google.com/AddSession?hl=uk"
LOGIN_SETTLE_SECONDS = 2  # увійшли — ще мить, щоб Google доставив кукі, і вкладку входу закриваємо
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


class LogBuffer(logging.Handler):
    """Останні рядки лога — для вкладки «Журнал»."""

    def __init__(self, size: int = 500):
        super().__init__()
        self.lines: deque[tuple[int, str, str]] = deque(maxlen=size)
        self.counter = 0
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%d.%m %H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self.counter += 1
        self.lines.append((self.counter, record.levelname, self.format(record)))

    def since(self, after: int) -> list[dict]:
        return [{"n": n, "level": level, "text": text} for n, level, text in list(self.lines) if n > after]


class Controller:
    def __init__(self, config: Config, config_path: Path, clock=None, runner_parts=None):
        self.config = config
        self.config_path = config_path
        self.clock = clock or (lambda: datetime.now(ZoneInfo(self.config.timezone)))
        self.runner_parts = runner_parts or self._real_parts
        self._commands: queue.Queue = queue.Queue()
        self._wake = threading.Event()
        self._state_lock = threading.Lock()
        self._state: dict = {"running": False}
        self._thread = threading.Thread(target=self._loop, name="controller", daemon=True)
        self._done = threading.Event()
        # Живуть лише в потоці контролера:
        self.runner = None
        self.joiner = None
        self._stack: ExitStack | None = None
        self._restart_pending = False
        self._user_stopped = False  # вимкнули кнопкою — тоді й після збереження налаштувань не вмикаємо самі
        self._next_tick: datetime | None = None
        self._last_error = ""
        self.google_accounts: list[str] = read_accounts(config)  # у які Google-акаунти увійдено (кеш)
        self._login_open = False  # відкрито вхід у Google — стежимо, коли увійдуть
        self._login_pages: list = []
        self._accounts_at_login: set[str] = set()
        self._login_done_at = 0.0
        self._next_accounts_check = 0.0
        # Браузер програми показується у її вікні (див. screen.py); вікно й браузер спілкуються через це.
        self.screen = Screen()

    # --- Із будь-якого потоку ----------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def call(self, name: str, *args, timeout: float = 60):
        if self._done.is_set() or (self._thread.ident is not None and not self._thread.is_alive()):
            raise ConfigError("програма вже зупиняється")
        future: Future = Future()
        self._commands.put((name, args, future))
        self._wake.set()
        return future.result(timeout)

    def snapshot(self) -> dict:
        with self._state_lock:
            return dict(self._state)

    def shutdown(self) -> None:
        if self._thread.is_alive():
            try:
                self.call("quit", timeout=30)
            finally:
                self._done.wait(10)

    # --- Потік контролера --------------------------------------------------------

    def _loop(self) -> None:
        self._publish()
        self._first_accounts_check()
        while True:
            while True:
                try:
                    name, args, future = self._commands.get_nowait()
                except queue.Empty:
                    break
                try:
                    result = getattr(self, f"_do_{name}")(*args)
                    future.set_result(result)
                except Exception as e:  # noqa: BLE001 — помилку побачить вікно, а цикл житиме далі
                    log.exception("Команда %s не вдалася", name)
                    future.set_exception(e)
                self._publish()
                if name == "quit":
                    self._done.set()
                    self._refuse_waiting()
                    return
            self._tick_if_due()
            self._act_now()
            try:
                self._watch_login()
            except Exception:  # noqa: BLE001 — вхід не повинен зупиняти програму
                log.exception("Помилка під час входу в Google")
                self._login_open = False
            if self.joiner is not None:
                self.joiner.idle(0.5)  # чекаємо, даючи браузеру доставляти події
            else:
                self._wake.wait(0.5)
                self._wake.clear()

    def _refuse_waiting(self) -> None:
        """Програма зупиняється: усім, хто ще чекає на команду, — одразу відмова, а не тайм-аут."""
        while True:
            try:
                _, _, future = self._commands.get_nowait()
            except queue.Empty:
                return
            future.set_exception(ConfigError("програма вже зупиняється"))

    def _real_parts(self, stack: ExitStack):
        from .joiner import Joiner
        from .runner import Runner
        from .wiring import assistant_stack

        info = stack.enter_context(assistant_stack(self.config, self.clock))
        joiner = Joiner(
            self.config.join, self.config.profile_dir, audio_sink=info.audio_sink, frames=info.frames,
            audio_tap=info.audio_tap, zoom_app_heard=info.zoom_app_heard, chooser=info.choose, screen=self.screen,
        )
        runner = Runner(
            self.config, make_source(self.config), joiner, clock=self.clock, assistant=info.assistant,
            app_audio=info.app_audio, visited_file=self.config.data_dir / "visited.json",
            schedule_cache=self.config.data_dir / "schedule-cache.json",
            ears_silence=info.voice_meter.silent_for if info.voice_meter is not None else None,
        )
        info.refine_allowed = lambda: runner.quiet(self.clock())  # уточнення — лише коли пари не скоро
        info.assistant.schedule_note = runner.today_note  # мозок знає, яка пара була, яка йде, яка далі
        return runner, joiner

    def _tick_if_due(self) -> None:
        if self.runner is None:
            return
        now = self.clock()
        if self._next_tick is None or now >= self._next_tick:
            self._next_tick = now + timedelta(seconds=POLL_SECONDS)
            try:
                self.runner.tick(now)
            except Exception:  # noqa: BLE001
                log.exception("Помилка в циклі розкладу")
            if self._restart_pending and not self.runner.active:
                log.info("Пара скінчилась — застосовую відкладені налаштування")
                self._restart_pending = False
                self._do_stop()
                self._do_start()
            self._publish()

    def _act_now(self) -> None:
        if self.runner is None or not hasattr(self.runner, "act_now"):
            return
        try:
            self.runner.act_now(self.clock())
        except Exception:  # noqa: BLE001
            log.exception("Помилка під час дії в зустрічі")

    def _do_start(self) -> str:
        self._user_stopped = False
        if self.runner is not None:
            return "already"
        if self.joiner is not None:  # браузер, відкритий для входу в Google, — перезапустимо з потрібними налаштуваннями
            self.joiner.close()
            self.joiner = None
        stack = ExitStack()
        try:
            self.runner, self.joiner = self.runner_parts(stack)
        except Exception:
            stack.close()
            raise
        self._stack = stack
        self._next_tick = None
        log.info("Автоматичний вхід на пари увімкнено")
        return "started"

    def _do_stop(self) -> str:
        if self.runner is None:
            return "already"
        sleep_guard = getattr(self.runner, "sleep_guard", None)
        if sleep_guard is not None:
            sleep_guard.release()
        if hasattr(self.runner, "finish_all"):
            try:
                self.runner.finish_all()  # запис пари, що ще йде, — зберегти
            except Exception:  # noqa: BLE001
                log.exception("Не вдалося зберегти запис пари")
        if self.joiner is not None:
            self.joiner.close()
        if self._stack is not None:
            self._stack.close()
        self.runner, self.joiner, self._stack = None, None, None
        log.info("Автоматичний вхід на пари вимкнено")
        return "stopped"

    def _do_user_stop(self) -> str:
        self._user_stopped = True
        return self._do_stop()

    def _do_quit(self) -> str:
        self._do_stop()
        if self.joiner is not None:
            self.joiner.close()
            self.joiner = None
        return "quit"

    def _do_reload(self, config: Config) -> str:
        old, self.config = self.config, config
        runner = self.runner
        if runner is None:
            if self._user_stopped or not configured(config):
                return "saved"
            # Групу щойно вибрали (або імпортували налаштування) — одразу вмикаємо вхід на пари.
            try:
                self._do_start()
            except Exception as e:  # noqa: BLE001 — налаштування збережені; причину видно у вікні й лозі
                log.error("Налаштування збережено, але автоматичний вхід не запустився: %s", e)
                return "saved"
            return "started"
        light_telegram = {"mentions": config.telegram.mentions, "joins": config.telegram.joins}  # діють на льоту
        heavy = (
            any(getattr(old, section) != getattr(config, section) for section in ("ears", "brain", "timezone", "data_dir"))
            or replace(old.telegram, **light_telegram) != config.telegram
            or old.join.mute_sound != config.join.mute_sound
        )
        if heavy:
            if runner.active:
                self._restart_pending = True
                return "after_lesson"  # не рвемо пару: застосуємо, щойно вона скінчиться
            self._do_stop()
            self._do_start()
            return "restarted"
        # Легкі зміни — на льоту.
        runner.config = config
        if runner.assistant is not None:
            runner.assistant.set_me(config.me)
            runner.assistant.alert_mentions = config.telegram.mentions
        self.joiner.settings = config.join
        if old.schedule != config.schedule:
            runner.client = make_source(config)
            runner.target = None
        runner.next_refresh = None
        self._next_tick = None
        return "applied"

    def _do_restart(self) -> str:
        """Перезапустити помічника (наприклад, щойно встановили розпізнавання мови), не рвучи пару."""
        if self.runner is None:
            return "saved"
        if self.runner.active:
            self._restart_pending = True
            return "after_lesson"
        self._do_stop()
        self._do_start()
        return "restarted"

    def _do_test_join(self, url: str, minutes: int) -> str:
        if self.runner is None:
            self._do_start()
        now = self.clock()
        for visit in self.runner.plan.visits:
            # «Зайти зараз» на пару з розкладу (почалась раніше) — заходимо на неї саму, з її назвою й звітом,
            # а не «тестовим входом», що розірвав би пару на два записи.
            if (visit.url == url and visit.key not in self.runner.finished and visit.key not in self.runner.active
                    and visit.join_at - timedelta(hours=1) <= now < visit.leave_at):
                self.runner.retry_at[visit.key] = now
                self._next_tick = None
                return "joining"
        leave_at = now + timedelta(minutes=minutes)
        lesson = Lesson(now.date(), now.time(), leave_at.time(), "Тестовий вхід")
        self.runner.extra_visits.append(Visit(url, platform_of(url), [lesson], now, leave_at))
        self._next_tick = None
        return "joining"

    def _do_leave(self, key: str) -> str:
        if self.runner is not None and key in self.runner.active:
            self.runner._leave(key, "ти натиснув «Вийти» у вікні програми")
            return "left"
        return "not_found"

    def _do_login(self) -> str:
        """Вхід у Google — у браузері програми, який видно в її вікні. Уже є акаунт — одразу «додати ще один»."""
        if not browser_setup.ensure():
            raise ConfigError(browser_setup.status()["error"])
        if self.joiner is None:
            from .joiner import Joiner

            self.joiner = Joiner(self.config.join, self.config.profile_dir, screen=self.screen)
        browser = self.joiner.browser
        pages = []
        if self.config.join.zoom_mode == "browser":
            pages.append(browser.new_page())
            pages[-1].goto("https://zoom.us/signin", wait_until="commit")
        pages.append(browser.new_page())  # Google — останньою, щоб саме її було видно
        pages[-1].goto(GOOGLE_ADD_ACCOUNT if self.google_accounts else GOOGLE_LOGIN, wait_until="commit")
        self._login_pages = [page for page in self._login_pages if not page.is_closed()] + pages
        self._accounts_at_login = set(self.google_accounts)
        self._login_open = True
        self._login_done_at = 0.0
        self._next_accounts_check = 0.0
        return "opened"

    def _do_close_login(self) -> str:
        """Увійшли (або передумали): закрити вкладки входу, а браузер — якщо в ньому не йде пара."""
        self._check_accounts()
        self._login_open = False
        self._login_done_at = 0.0
        pages, self._login_pages = self._login_pages, []
        if self.joiner is not None:
            for page in pages:
                try:
                    if not page.is_closed():
                        page.close()
                except Exception:  # noqa: BLE001 — браузер уже закрили
                    pass
            self.joiner.browser.close_if_idle()
        return "closed"

    def _watch_login(self) -> None:
        """Поки відкрито вхід — раз на кілька секунд дивимось, у які акаунти вже увійшли. З'явився новий —
        вхід готовий: вкладку входу закриваємо самі, натискати нічого не треба."""
        if not self._login_open:
            return
        if self.joiner is None or not self.joiner.browser.is_open():
            self._login_open = False  # браузер закрився
            self._publish()
            return
        if self._login_done_at and time.monotonic() >= self._login_done_at:
            log.info("Вхід у Google готовий — закриваю вкладку входу")
            self._do_close_login()
            self._publish()
            return
        if time.monotonic() >= self._next_accounts_check:
            self._next_accounts_check = time.monotonic() + 3
            self._check_accounts()
            fresh = set(self.google_accounts) - self._accounts_at_login
            if fresh and not self._login_done_at and self.config.join.zoom_mode != "browser":
                self._login_done_at = time.monotonic() + LOGIN_SETTLE_SECONDS

    def _first_accounts_check(self) -> None:
        """Список Google-акаунтів ще невідомий (перший запуск нової версії) — один раз тихо дивимось, у які
        акаунти вже увійдено, у невидимому браузері. Лише якщо браузер програми вже є й зараз не зайнятий."""
        if _accounts_file(self.config).exists() or not self.config.profile_dir.exists():
            return
        if (self.joiner is not None and self.joiner.browser.is_open()) or browser_setup.status()["status"] == "installing":
            return
        from .joiner import Browser

        browser = Browser(self.config.profile_dir, headless=True)
        try:
            accounts = browser.google_accounts() if browser.context() else None
        except Exception as e:  # noqa: BLE001 — це лише підказка для вікна
            log.debug("Не вдалося подивитись акаунти Google: %s", e)
            accounts = None
        finally:
            browser.close()
        if accounts is not None:
            self.google_accounts = accounts
            write_accounts(self.config, accounts)
            log.info("Google: увійдено в %s", ", ".join(accounts) if accounts else "жоден акаунт")
            self._publish()

    def _check_accounts(self) -> None:
        if self.joiner is None:
            return
        accounts = self.joiner.browser.google_accounts()
        if accounts is not None and accounts != self.google_accounts:
            self.google_accounts = accounts
            write_accounts(self.config, accounts)
            log.info("Google: увійдено в %s", ", ".join(accounts) if accounts else "жоден акаунт")
            self._publish()

    def _publish(self) -> None:
        runner = self.runner
        state: dict = {
            "running": runner is not None,
            "restart_pending": self._restart_pending,
            "now": self.clock().isoformat(),
            "google_accounts": list(self.google_accounts),
            "login_open": self._login_open,
            "browser": browser_setup.status(),
        }
        if runner is not None:
            target = runner.target
            state["group"] = (target.student_name or target.group_name) if target else ""
            state["error"] = getattr(runner, "last_error", "")
            state["active"] = [
                {
                    "key": key,
                    "title": session.title,
                    "platform": session.visit.platform,
                    "url": session.visit.url,
                    "participants": getattr(runner.last_seen.get(key), "participants", None),
                    "in_browser": getattr(session, "page", None) is not None,  # а не в Zoom-клієнті
                }
                for key, session in runner.active.items()
            ]
            state["upcoming"] = [
                {
                    "title": visit.title,
                    "platform": visit.platform,
                    "url": visit.url,
                    "join_at": visit.join_at.isoformat(),
                    "leave_at": visit.leave_at.isoformat(),
                }
                for visit in runner.plan.visits
                if visit.key not in runner.finished and visit.key not in runner.active
            ][:8]
            state["unlinked"] = [
                {"title": lesson.title, "when": f"{WEEKDAYS[lesson.day.weekday()]} {lesson.day:%d.%m} {lesson.start:%H:%M}"}
                for lesson in runner.plan.unlinked
            ][:10]
            state["conflicts"] = list(runner.plan.conflicts)
        with self._state_lock:
            self._state = state


# --- Дані для вікна ------------------------------------------------------------


def _accounts_file(config: Config) -> Path:
    return config.data_dir / "google-accounts.json"


def read_accounts(config: Config) -> list[str]:
    """Останній відомий список Google-акаунтів — щоб показувати його й без відкритого браузера."""
    try:
        accounts = json.loads(_accounts_file(config).read_text(encoding="utf-8"))
        return [a for a in accounts if isinstance(a, str)]
    except (OSError, ValueError, TypeError):
        return []


def write_accounts(config: Config, accounts: list[str]) -> None:
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        _accounts_file(config).write_text(json.dumps(accounts, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log.debug("Не вдалося запам'ятати акаунти: %s", e)


def configured(config: Config) -> bool:
    s = config.schedule
    return bool(s.group or s.group_uid or s.group_id)


def lessons_view(config: Config, days: int, source=None, start: date | None = None, target=None) -> dict:
    source = source or make_source(config)
    target = target or resolve_target(source, config)
    tz = ZoneInfo(config.timezone)
    now = datetime.now(tz)
    first = start or now.date()
    lessons = source.schedule(target, first, first + timedelta(days=days))
    rows = []
    for lesson in lessons:
        resolution = resolve(lesson, config.links, config.schedule.use_schedule_links)
        rows.append(
            {
                "date": lesson.day.isoformat(),
                "weekday": WEEKDAYS[lesson.day.weekday()],
                "start": f"{lesson.start:%H:%M}",
                "end": f"{lesson.end:%H:%M}",
                "subject": lesson.subject,
                "short": lesson.short_subject,
                "type": lesson.type,
                "teacher": lesson.teacher,
                "room": lesson.room,
                "url": resolution.url,
                "source": resolution.source,
                "platform": platform_of(resolution.url) if resolution.url else None,
            }
        )
    return {
        "group": target.student_name or target.group_name or target.uid,
        "lessons": rows,
        "today": now.date().isoformat(),
        "now": f"{now:%H:%M}",
        "first": first.isoformat(),
        "last": (first + timedelta(days=days)).isoformat(),
    }


def list_profiles() -> list[str]:
    return sorted(path.stem for path in PROFILES.glob("*.toml")) if PROFILES.is_dir() else []


# --- Сервер вікна --------------------------------------------------------------


class AppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, controller: Controller, port: int = 0, token: str | None = None, logs: LogBuffer | None = None):
        super().__init__(("127.0.0.1", port), Handler)
        self.controller = controller
        self.token = token or secrets.token_urlsafe(24)
        self.logs = logs or LogBuffer()
        self.sources: dict[str, object] = {}  # кеш клієнтів розкладу для пошуку груп
        self.targets: dict[tuple, object] = {}  # уже знайдені групи — щоб гортати тижні без зайвих запитів
        self.autostart = autostart
        self.show_window = None  # вікно програми ставить сюди «показати себе»

    def autostart_once(self) -> bool:
        """Щойно налаштували вперше — вмикаємо запуск разом із комп'ютером (вимкнути можна у «Поведінці»)."""
        try:
            state = self.autostart.status()
            if state["supported"] and not state["enabled"]:
                return bool(self.autostart.set_enabled(True)["enabled"])
        except OSError as e:
            log.warning("Не вдалося ввімкнути автозапуск: %s", e)
        return False

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#t={self.token}"

    def source_for(self, university: str):
        if university not in self.sources:
            config = Config()
            config.schedule.university = university
            self.sources[university] = make_source(config)
        return self.sources[university]


class Handler(BaseHTTPRequestHandler):
    server: AppServer

    def log_message(self, format: str, *args) -> None:  # noqa: A002 — так зветься в BaseHTTPRequestHandler
        log.debug("UI: " + format, *args)

    # Відповіді

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        return data if isinstance(data, dict) else {}

    # Маршрути

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def _route(self, method: str) -> None:
        if not self._host_ok():
            return self._json(403, {"error": "чужий Host"})
        parts = urlsplit(self.path)
        if not secrets.compare_digest(self.headers.get("X-Token", ""), self.server.token):
            return self._json(403, {"error": "немає токена — відкрий вікно через `autopara app`"})
        handler = ROUTES.get((method, parts.path))
        if handler is None:
            return self._json(404, {"error": "немає такого"})
        query = {key: values[-1] for key, values in parse_qs(parts.query).items()}
        try:
            body = self._body() if method != "GET" else {}
            status, data = handler(self.server, query, body)
        except (ConfigError, RozkladError) as e:
            status, data = 400, {"error": str(e)}
        except json.JSONDecodeError:
            status, data = 400, {"error": "некоректний JSON"}
        except Exception as e:  # noqa: BLE001
            log.exception("UI: помилка в %s %s", method, parts.path)
            status, data = 500, {"error": str(e)}
        self._json(status, data)


def api_state(server: AppServer, query: dict, body: dict):
    config = server.controller.config
    state = server.controller.snapshot()
    state["configured"] = configured(config)
    state["university"] = UNIVERSITIES.get(config.schedule.university, "")
    return 200, state


def api_get_config(server: AppServer, query: dict, body: dict):
    controller = server.controller
    return 200, {
        "config": config_to_dict(controller.config),
        "universities": UNIVERSITIES,
        "path": str(controller.config_path),
        "profiles": list_profiles(),
        "platform": sys.platform,
        "autostart": server.autostart.status(),
        "ears_installed": whisper_available() is None,
        "google_accounts": list(controller.google_accounts),
    }


def api_put_config(server: AppServer, query: dict, body: dict):
    config = parse_config(body.get("config") or {})
    first_time = not configured(server.controller.config) and configured(config)
    save_config(config, server.controller.config_path)
    result = server.controller.call("reload", config)
    return 200, {"ok": True, "result": result, "config": config_to_dict(config), "autostart": first_time and server.autostart_once()}


def api_import(server: AppServer, query: dict, body: dict):
    if name := body.get("profile"):
        if name not in list_profiles():
            return 404, {"error": f"профілю {name} немає"}
        data = tomllib.loads((PROFILES / f"{name}.toml").read_text(encoding="utf-8"))
    else:
        try:
            data = tomllib.loads(body.get("toml") or "")
        except tomllib.TOMLDecodeError as e:
            return 400, {"error": f"файл не схожий на налаштування autopara: {e}"}
    config = parse_config(data)
    controller = server.controller
    if controller.config.telegram.token and not config.telegram.token:
        config.telegram = controller.config.telegram  # не губимо вже налаштований бот
    first_time = not configured(controller.config) and configured(config)
    save_config(config, controller.config_path)
    result = controller.call("reload", config)
    return 200, {"ok": True, "result": result, "config": config_to_dict(config), "autostart": first_time and server.autostart_once()}


def api_groups(server: AppServer, query: dict, body: dict):
    university = query.get("university") or server.controller.config.schedule.university
    text = (query.get("q") or "").strip()
    if len(text) < 2:
        return 200, {"groups": []}
    found = server.source_for(university).search_groups(text)
    return 200, {
        "groups": [
            {
                "name": group.group_name,
                "faculty": group.faculty_name,
                "course": group.course or None,
                "faculty_id": group.faculty_id or None,
                "group_id": group.group_id or None,
                "uid": group.uid,
            }
            for group in found
        ]
    }


def api_students(server: AppServer, query: dict, body: dict):
    source = server.source_for("duikt")
    if query.get("group_id"):
        faculty, course, group = int(query["faculty_id"]), int(query["course"]), int(query["group_id"])
    else:
        target = source.find_group(query.get("group", ""))
        faculty, course, group = target.faculty_id, target.course, target.group_id
    students = source.students(faculty, course, group)
    return 200, {"students": [student.name for student in students]}


def api_lessons(server: AppServer, query: dict, body: dict):
    config = server.controller.config
    source = make_source(config) if config.schedule.base_url else server.source_for(config.schedule.university)
    start = None
    if query.get("start"):
        try:
            start = date.fromisoformat(query["start"])
        except ValueError:
            return 400, {"error": "дата має бути у форматі РРРР-ММ-ДД"}
    days = min(max(int(query.get("days", 14)), 0), 62)
    key = astuple(config.schedule)
    if key not in server.targets:
        server.targets[key] = resolve_target(source, config)
    return 200, lessons_view(config, days, source, start=start, target=server.targets[key])


def api_command(name: str):
    def handler(server: AppServer, query: dict, body: dict):
        return 200, {"result": server.controller.call(name)}

    return handler


def api_join(server: AppServer, query: dict, body: dict):
    url = (body.get("url") or "").strip()
    if not url:
        return 400, {"error": "встав посилання на зустріч"}
    return 200, {"result": server.controller.call("test_join", url, int(body.get("minutes") or 60))}


def api_leave(server: AppServer, query: dict, body: dict):
    return 200, {"result": server.controller.call("leave", body.get("key", ""))}


def api_check(server: AppServer, query: dict, body: dict):
    checks = run_checks(server.controller.config)
    return 200, {"checks": [{"ok": c.ok, "area": c.area, "text": c.text} for c in checks]}


def _telegram_token(body: dict) -> str:
    token = (body.get("token") or "").strip()
    if not re.fullmatch(r"\d+:[\w-]{20,}", token):
        raise ConfigError("це не схоже на токен від @BotFather — він виглядає як 123456789:AAH…")
    return token


def api_telegram_check(server: AppServer, query: dict, body: dict):
    from .telegram import TelegramBot, TelegramError

    try:
        me = TelegramBot(_telegram_token(body)).me()
    except TelegramError as e:
        if "Unauthorized" in str(e) or "Not Found" in str(e):
            return 400, {"error": "Telegram не знає такого токена — скопіюй його від @BotFather ще раз"}
        return 400, {"error": str(e)}
    return 200, {"username": me.get("username", ""), "name": me.get("first_name", "")}


def api_telegram_pair(server: AppServer, query: dict, body: dict):
    from .telegram import TelegramError, wait_for_code

    code = str(body.get("code") or "")
    if not re.fullmatch(r"\d{6}", code):
        return 400, {"error": "некоректний код"}
    try:
        chat_id = wait_for_code(_telegram_token(body), code, wait_seconds=int(body.get("wait") or 180))
    except TelegramError as e:
        if "Conflict" in str(e):
            return 409, {"error": "цього бота вже слухає інша програма — вимкни її на хвилину й спробуй знову"}
        return 400, {"error": str(e)}
    return 200, {"chat_id": chat_id}


def api_telegram_test(server: AppServer, query: dict, body: dict):
    from .telegram import TelegramBot, TelegramError

    try:
        TelegramBot(body.get("token", ""), int(body.get("chat_id") or 0)).send("✓ autopara на зв'язку")
    except TelegramError as e:
        return 400, {"error": str(e)}
    return 200, {"ok": True}


def api_audio_devices(server: AppServer, query: dict, body: dict):
    from .ears import list_input_devices

    return 200, {"devices": list_input_devices()}


def api_install_ears(server: AppServer, query: dict, body: dict):
    """Доставити розпізнавання мови в те саме середовище Python, де працює програма, і перезапустити вуха."""
    packages = ["faster-whisper>=1.0"] + ([] if sys.platform.startswith("linux") else ["sounddevice>=0.4"])
    log.info("Встановлюю розпізнавання мови (%s) — 1–3 хвилини...", ", ".join(packages))
    result = subprocess.run([sys.executable, "-m", "pip", "install", *packages], capture_output=True, text=True)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        log.error("Не вдалося встановити розпізнавання мови: %s", " / ".join(tail))
        return 500, {"error": "не вдалося встановити: " + " ".join(tail)[-300:]}
    importlib.invalidate_caches()
    log.info("Розпізнавання мови встановлено")
    return 200, {"ok": True, "result": server.controller.call("restart", timeout=300)}


def api_autostart(server: AppServer, query: dict, body: dict):
    try:
        return 200, server.autostart.set_enabled(bool(body.get("enabled")))
    except OSError as e:
        return 400, {"error": f"не вдалося змінити автозапуск: {e}"}


def api_log(server: AppServer, query: dict, body: dict):
    return 200, {"lines": server.logs.since(int(query.get("after", 0)))}


def api_show(server: AppServer, query: dict, body: dict):
    """Іншу копію запустили з ярлика — показуємо вікно цієї (якщо вона з вікном)."""
    if server.show_window is None:
        return 200, {"result": "no_window"}
    server.show_window()
    return 200, {"result": "shown"}


ROUTES = {
    ("GET", "/api/state"): api_state,
    ("GET", "/api/config"): api_get_config,
    ("PUT", "/api/config"): api_put_config,
    ("POST", "/api/import"): api_import,
    ("GET", "/api/groups"): api_groups,
    ("GET", "/api/students"): api_students,
    ("GET", "/api/lessons"): api_lessons,
    ("POST", "/api/start"): api_command("start"),
    ("POST", "/api/stop"): api_command("user_stop"),
    ("POST", "/api/login"): api_command("login"),
    ("POST", "/api/join"): api_join,
    ("POST", "/api/leave"): api_leave,
    ("POST", "/api/check"): api_check,
    ("POST", "/api/telegram/check"): api_telegram_check,
    ("POST", "/api/telegram/pair"): api_telegram_pair,
    ("POST", "/api/telegram/test"): api_telegram_test,
    ("GET", "/api/log"): api_log,
    ("POST", "/api/autostart"): api_autostart,
    ("GET", "/api/audio-devices"): api_audio_devices,
    ("POST", "/api/install-ears"): api_install_ears,
    ("POST", "/api/show"): api_show,
}


# --- Запуск --------------------------------------------------------------------


def lock_path() -> Path:
    return user_config_dir() / "app.json"


def running_instance() -> dict | None:
    """Якщо програма вже працює — {port, token, pid, url}.

    Питаємо саму програму, а не перевіряємо pid: так не сплутаємо її з чужим процесом із тим самим номером.
    """
    try:
        info = json.loads(lock_path().read_text(encoding="utf-8"))
        port, token = int(info["port"]), str(info["token"])
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"X-Token": token})
        with _local_opener().open(request, timeout=3) as response:
            if response.status != 200:
                return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return {**info, "url": f"http://127.0.0.1:{port}/#t={token}"}


def _local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # до 127.0.0.1 — завжди напряму


def push_config(instance: dict, config: Config) -> str:
    """Передати налаштування вже запущеній програмі: вона їх збереже й застосує, не перериваючи пару."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{instance['port']}/api/config",
        data=json.dumps({"config": config_to_dict(config)}, ensure_ascii=False).encode("utf-8"),
        method="PUT",
        headers={"X-Token": instance["token"], "Content-Type": "application/json"},
    )
    with _local_opener().open(request, timeout=60) as response:
        return json.loads(response.read())["result"]


def show_running(instance: dict) -> str:
    """Попросити вже запущену програму показати своє вікно: "shown" або "no_window" (autopara run)."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{instance['port']}/api/show",
        data=b"{}",
        method="POST",
        headers={"X-Token": instance["token"], "Content-Type": "application/json"},
    )
    with _local_opener().open(request, timeout=10) as response:
        return json.loads(response.read())["result"]


def write_lock(port: int, token: str) -> Path:
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.unlink(missing_ok=True)
    # У файлі токен вікна — читати його може лише власник.
    with os.fdopen(os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as f:
        json.dump({"port": port, "token": token, "pid": os.getpid()}, f)
    return lock


def release_lock(lock: Path) -> None:
    try:
        if json.loads(lock.read_text(encoding="utf-8")).get("pid") == os.getpid():
            lock.unlink()
    except (OSError, ValueError):
        pass


class Program:
    """Фонова частина: контролер і локальний API. Без вікна (``autopara run``) чи з ним (``gui``)."""

    def __init__(self, config: Config, config_path: Path, port: int = 0):
        self.config = config
        self.logs = LogBuffer()
        logging.getLogger().addHandler(self.logs)
        self.controller = Controller(config, config_path)
        self.server = AppServer(self.controller, port=port, logs=self.logs)
        self.lock: Path | None = None

    def start(self, autostart: bool = True) -> None:
        self.controller.start()
        self.lock = write_lock(self.server.port, self.server.token)
        browser_setup.prepare_in_background()  # якщо браузер програми ще не завантажено — докачати, поки налаштовують
        if autostart and configured(self.config):
            threading.Thread(target=_autostart, args=(self.controller,), daemon=True).start()
        elif not configured(self.config):
            log.warning("Група ще не вибрана — відкрий вікно програми (autopara app) і заповни налаштування")

    def stop(self) -> None:
        self.server.server_close()
        self.controller.shutdown()
        if self.lock is not None:
            release_lock(self.lock)


def serve(config: Config, config_path: Path, autostart: bool = True, port: int = 0) -> None:
    """Без вікна (``autopara run``): блокує до Ctrl+C / SIGTERM."""
    program = Program(config, config_path, port=port)
    program.start(autostart)
    log.info("autopara працює у фоні без вікна")
    try:
        program.server.serve_forever()
    except KeyboardInterrupt:
        log.info("Зупинено")
    finally:
        program.stop()


def _autostart(controller: Controller) -> None:
    try:
        controller.call("start", timeout=300)
    except Exception as e:  # noqa: BLE001 — програма житиме далі, а помилку видно у вікні й лозі
        log.error("Не вдалося запустити автоматичний вхід: %s", e)
