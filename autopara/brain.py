"""«Головний мозок»: локальна мовна модель через Ollama.

Модель бачить опис ситуації (assistant.snapshot) і обирає одну дію з короткого
списку. Вона не пише в чат довільний текст і не керує мікрофоном — лише обирає,
а виконують і перевіряють дію рефлекси помічника (assistant.apply_decision).
Модель кличемо не постійно, а коли вуха чи очі помітили щось варте уваги:
так ноут не гріється дарма.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime
from time import monotonic
from typing import Callable

import requests

from .assistant import Decision

log = logging.getLogger(__name__)

ACTIONS = ("none", "notify", "chat_plus", "chat_name", "leave")
COOLDOWN_SECONDS = 45

SYSTEM_PROMPT = """Ти — помічник студента на онлайн-парі. Студента зараз немає біля комп'ютера; \
його мікрофон і камера вимкнені, і ти не можеш їх увімкнути.
Тобі дають опис пари: що на ній кажуть (автоматично розпізнана мова — з помилками; «[?]» — розпізнано \
невпевнено), повідомлення в чаті й кількість учасників. Мова буває українська, суржик, російська, \
з англійськими термінами: орієнтуйся на зміст розмови, а не на окремі слова.

Що де буває (вчися розрізняти):
- ПЕРЕКЛИЧКА: «поставте плюс, хто на парі», «відмітьтесь», викладач читає список групи, \
і студенти відповідають «є», «тут».
- НЕ перекличка, хоч і звучать прізвища чи «плюс»: перелік боржників чи тих, хто не здав роботу; \
розподіл тем чи доповідей; «хто хоче / хто готовий — плюс»; голосування; «плюс, якщо зрозуміло»; \
«плюс, хто вже зробив» (це заява від імені студента).
- Перевірка зв'язку: «плюс, якщо мене чути / бачите екран» — плюс тут нешкідливий.

Обери ОДНУ дію:
- "none" — нічого важливого, звичайний хід пари. Обирай це найчастіше.
- "notify" — надіслати студенту сповіщення на телефон. У "message" одним-двома реченнями напиши, \
що сталося і що від нього хочуть: викладач кличе студента, просить відповісти, увімкнути камеру, \
здати роботу, оголошує дедлайн чи контрольну, студента назвали серед боржників.
- "chat_plus" — написати «+» у чат: лише на ПЕРЕКЛИЧЦІ чи перевірці зв'язку. Якщо плюсом записуються, \
голосують, погоджуються, кажуть «зрозуміло» чи «вже зробив» — це НЕ перекличка: обирай "notify".
- "chat_name" — написати в чат прізвище та ім'я студента: лише на перекличці за прізвищами.
- "leave" — запропонувати студенту вийти: лише якщо викладач явно завершив пару.
Ніколи не відповідай на запитання з матеріалу пари замість студента — для цього є "notify".
Відповідай лише JSON: {"action": "...", "message": "...", "reason": "..."}; у "reason" коротко \
процитуй фразу, на яку спираєшся."""

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "message": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["action", "message", "reason"],
}


class BrainError(Exception):
    pass


def parse_decision(content: str) -> Decision:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise BrainError(f"модель відповіла не JSON: {content[:200]!r}") from e
    if not isinstance(data, dict):
        raise BrainError(f"модель відповіла не об'єктом: {content[:200]!r}")
    action = str(data.get("action", "none"))
    if action not in ACTIONS:
        log.warning("Модель вигадала дію %r — ігнорую", action)
        action = "none"
    return Decision(action, str(data.get("message", ""))[:500], str(data.get("reason", ""))[:300])


class OllamaBrain:
    def __init__(self, url: str, model: str, session: requests.Session | None = None, timeout: float = 180):
        self.url = url.rstrip("/")
        self.model = model
        self.session = session or requests.Session()
        self.timeout = timeout

    def decide(self, situation: str) -> Decision:
        payload = {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "keep_alive": "30m",
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": situation},
            ],
        }
        try:
            response = self.session.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
            response.raise_for_status()
            content = response.json()["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as e:
            raise BrainError(f"Ollama недоступна ({self.url}): {e}") from e
        return parse_decision(content)

    def summarize(self, instruction: str, text: str) -> str:
        """Вільний текст (конспект, звіт) — без JSON і без дій."""
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.2, "num_predict": 400},
            "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": text}],
        }
        try:
            response = self.session.post(f"{self.url}/api/chat", json=payload, timeout=self.timeout * 2)
            response.raise_for_status()
            return response.json()["message"]["content"].strip()
        except (requests.RequestException, KeyError, ValueError) as e:
            raise BrainError(f"Ollama недоступна ({self.url}): {e}") from e

    def check(self) -> str | None:
        """None, якщо все гаразд, інакше — що не так."""
        try:
            tags = self.session.get(f"{self.url}/api/tags", timeout=5).json()
        except (requests.RequestException, ValueError):
            return "Ollama не встановлена чи не запущена — ollama.com/download (на Arch: sudo pacman -S ollama)"
        names = {model.get("name") for model in tags.get("models", [])}
        if self.model not in names and f"{self.model}:latest" not in names:
            return f"модель {self.model} ще не завантажена — програма завантажить її сама під час запуску"
        return None

    def reachable(self) -> bool:
        try:
            return self.session.get(f"{self.url}/api/tags", timeout=5).ok
        except requests.RequestException:
            return False

    def pull(self) -> bool:
        """Завантажити модель через саму Ollama (кілька гігабайтів), показуючи поступ у лозі."""
        log.info("Мозок: завантажую модель %s (перший раз — кілька ГБ, це може тривати)...", self.model)
        shown = -1
        try:
            with self.session.post(
                f"{self.url}/api/pull", json={"model": self.model, "stream": True}, stream=True, timeout=(10, 600)
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    status = json.loads(line)
                    if status.get("error"):
                        raise BrainError(status["error"])
                    total, done = status.get("total") or 0, status.get("completed") or 0
                    if total and (percent := int(done * 100 / total) // 10 * 10) != shown:
                        shown = percent
                        log.info("Мозок: модель %s — %d%%", self.model, percent)
                    if status.get("status") == "success":
                        return True
        except (requests.RequestException, ValueError, BrainError) as e:
            log.warning("Мозок: не вдалося завантажити модель %s (%s) — працюю без нього", self.model, e)
            return False
        return self.check() is None


class BrainConnector:
    """Підключає мозок, щойно він готовий: Ollama запущена й модель є (немає — завантажує сама).

    Перевіряє у фоні й повторює раз на кілька хвилин: Ollama могла ще не встигнути запуститися після
    входу в систему, або її встановили вже тоді, коли програма працювала. До того працюють рефлекси.
    """

    def __init__(self, brain: OllamaBrain, on_ready: Callable[[], None], retry_seconds: float = 300):
        self.brain = brain
        self.on_ready = on_ready
        self.retry_seconds = retry_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="brain-connect", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        told = False
        while not self._stop.is_set():
            if self.brain.check() is None or (self.brain.reachable() and self.brain.pull()):
                if not self._stop.is_set():
                    self.on_ready()
                return
            if not told and not self.brain.reachable():
                told = True
                log.info("Мозок: Ollama не знайдена — працюю без нього, а щойно вона з'явиться, підключу сам")
            self._stop.wait(self.retry_seconds)


class BrainWorker:
    """Кличе модель у фоновому потоці.

    Рішення (що робити зараз) — у пріоритеті: сигнали, що прийшли, поки модель думала, зливаються
    в один виклик. Фонові задачі (конспект пари) — коли рішень немає.
    """

    def __init__(self, brain: OllamaBrain, clock: Callable[[], datetime]):
        self.brain = brain
        self.clock = clock
        self.assistant = None  # під'єднується після створення помічника
        self._wanted: set[str] = set()
        self._jobs: queue.Queue[Callable[[], None]] = queue.Queue()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_call = -COOLDOWN_SECONDS
        self._thread = threading.Thread(target=self._run, name="brain", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self, key: str) -> None:
        with self._lock:
            self._wanted.add(key)
        self._wake.set()

    def submit(self, job: Callable[[], None]) -> None:
        """Фонова задача з низьким пріоритетом."""
        self._jobs.put(job)
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            pause = None
            with self._lock:
                if self._wanted:
                    # Не частіше ніж раз на COOLDOWN_SECONDS: сигнали за цей час зіллються.
                    pause = self._last_call + COOLDOWN_SECONDS - monotonic()
                    keys = set()
                    if pause <= 0:
                        keys, self._wanted = self._wanted, set()
            if pause is not None and pause <= 0:
                for key in keys:
                    self.think(key)
                continue
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                job = None
            if job is not None:
                try:
                    job()
                except Exception:  # noqa: BLE001 — одна невдала задача не має зупиняти мозок
                    log.exception("Фонова задача мозку впала")
                continue
            self._wake.wait(timeout=pause)
            self._wake.clear()

    def think(self, key: str) -> None:
        situation = self.assistant.snapshot(key, self.clock()) if self.assistant else None
        if situation is None:
            return
        self._last_call = monotonic()
        started = monotonic()
        try:
            decision = self.brain.decide(situation)
        except BrainError as e:
            log.warning("Мозок: %s", e)
            return
        log.debug("Мозок думав %.0f с: %s", monotonic() - started, decision)
        self.assistant.apply_decision(key, decision, self.clock())
