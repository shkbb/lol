"""«Головний мозок»: локальна мовна модель через Ollama.

Модель бачить опис ситуації (assistant.snapshot) і обирає одну дію з короткого
списку. Вона не пише в чат довільний текст і не керує мікрофоном — лише обирає,
а виконують і перевіряють дію рефлекси помічника (assistant.apply_decision).
Модель кличемо не постійно, а коли вуха чи очі помітили щось варте уваги:
так ноут не гріється дарма.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime
from time import monotonic
from typing import Callable

import requests

from . import accel
from .assistant import Decision

log = logging.getLogger(__name__)

ACTIONS = ("none", "notify", "chat_plus", "chat_name", "leave")
# Скільки Ollama тримає модель у пам'яті без діла. Наживо з вбудованою відеокартою Intel пам'ять процесу
# Ollama росла протягом дня (зранку ~3 ГБ, по обіді 6+ ГБ і ще ~4 ГБ спільної) і разом з усім іншим
# вичерпувала пам'ять ноутбука — тож тримаємо недовго й вивантажуємо, щойно пам'яті мало (BrainMemoryGuard).
KEEP_ALIVE = "10m"
LOW_MEMORY_GB = 2.0
# Ollama 0.35 з вбудованою відеокартою Intel (Vulkan) не віддає пам'ять: наживо кожен запит додавав ~270 МБ,
# і за пів години конспектів процес мозку виріс з 0,3 до 7,4 ГБ, поки ядро його не вбило. Тож після кожних
# кількох запитів вивантажуємо модель — з нею йде й накопичене; наступний запит завантажить її за секунди.
REQUESTS_PER_LOAD = 5
COOLDOWN_SECONDS = 45
CONTEXT = 6144  # токенів підказки й відповіді разом

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
- "none" — нічого важливого, звичайний хід пари. Обирай це найчастіше. Пояснення матеріалу, огляд тем, \
перелік інструментів чи прикладів — це "none", навіть якщо цікаво: усе це буде у звіті після пари.
- "notify" — надіслати студенту сповіщення на телефон, лише якщо від НЬОГО щось треба зараз чи він має \
це знати: викладач кличе студента, просить відповісти, увімкнути камеру, здати роботу, оголошує дедлайн \
чи контрольну, студента назвали серед боржників. У "message" одним-двома реченнями напиши, що сталося \
і що від нього хочуть, звертаючись до студента на «ти» і без його імені («тебе кличуть», «оголосили \
контрольну в понеділок»). Ніколи не переказуй, що зараз обговорюють, і не давай порад від себе.
- "chat_plus" — написати «+» у чат: лише на ПЕРЕКЛИЧЦІ чи перевірці зв'язку. Якщо плюсом записуються, \
голосують, погоджуються, кажуть «зрозуміло» чи «вже зробив» — це НЕ перекличка: обирай "notify".
- "chat_name" — написати в чат прізвище та ім'я студента: лише на перекличці за прізвищами.
- "leave" — запропонувати студенту вийти: лише якщо викладач явно завершив пару.
Ніколи не відповідай на запитання з матеріалу пари замість студента — для цього є "notify".
Відповідай лише JSON: {"action": "...", "message": "...", "reason": "..."}; у "reason" дослівно \
процитуй фразу з пари (з розшифровки чи чату), на яку спираєшся. "message" — лише українською, навіть якщо \
на парі говорять російською."""

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
    """``gpu`` — чи пускати модель на відеокарту (None — визначити самим, див. accel.brain_on_gpu). Без неї модель
    рахує процесор, і поки йде пара (``busy``), бере лише половину ядер — решта лишається вухам."""

    def __init__(self, url: str, model: str, session: requests.Session | None = None, timeout: float = 180,
                 gpu: bool | None = None, busy: Callable[[], bool] = lambda: False,
                 lock: threading.RLock | None = None):
        self.url = url.rstrip("/")
        self.model = model
        self.session = session or requests.Session()
        self.timeout = timeout
        self.gpu = accel.brain_on_gpu() if gpu is None else gpu
        self.busy = busy
        self.keep_alive = KEEP_ALIVE
        self.think: bool | None = None  # False — без «роздумів уголос» (Qwen думає за замовчуванням, це хвилини)
        # Спільний замок кількох моделей: поки відповідає велика модель для розмов, швидка чекає (дві разом не влізуть).
        self.lock = lock
        self.before: Callable[[], None] | None = None  # перед кожним запитом (ChatBrain: прибрати велику з пам'яті)
        self._requests = 0
        self._count_lock = threading.Lock()

    def _options(self, options: dict) -> dict:
        # Один розмір контексту для всіх запитів: Ollama перезавантажує модель, щойно він змінюється, а рішення
        # й конспекти наживо чергуються. Типові 4096 ще й мовчки обрізали відповіді з джерелами.
        options = {"num_ctx": CONTEXT, **options}
        if not self.gpu:
            options["num_gpu"] = 0
            if self.busy():
                options["num_thread"] = max(2, (os.cpu_count() or 4) // 4)  # половина фізичних ядер
        return options

    def _ask(self, payload: dict, timeout: float) -> dict:
        """Запит до Ollama; після кожних REQUESTS_PER_LOAD — вивантажити модель (див. REQUESTS_PER_LOAD)."""
        payload = {**payload, "options": self._options(payload.get("options", {}))}
        if self.think is not None:
            payload["think"] = self.think
        if self.lock is not None:
            with self.lock:
                if self.before is not None:
                    self.before()
                return self._post(payload, timeout)
        return self._post(payload, timeout)

    def _post(self, payload: dict, timeout: float) -> dict:
        try:
            response = self.session.post(f"{self.url}/api/chat", json=payload, timeout=timeout)
            response.raise_for_status()
            return response.json()
        finally:
            with self._count_lock:
                self._requests += 1
                due = self._requests % REQUESTS_PER_LOAD == 0
            if due:
                self.unload()

    def decide(self, situation: str) -> Decision:
        payload = {
            "model": self.model,
            "stream": False,
            "format": SCHEMA,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": situation},
            ],
        }
        try:
            content = self._ask(payload, self.timeout)["message"]["content"]
        except (requests.RequestException, KeyError, ValueError) as e:
            raise BrainError(f"Ollama недоступна ({self.url}): {e}") from e
        return parse_decision(content)

    def unload(self) -> bool:
        """Попросити Ollama вивантажити модель з пам'яті (якщо вона завантажена). True — було що вивантажувати."""
        try:
            loaded = self.session.get(f"{self.url}/api/ps", timeout=10).json().get("models") or []
            if not any(item.get("name", "").split(":")[0] == self.model.split(":")[0] for item in loaded):
                return False
            self.session.post(f"{self.url}/api/generate", json={"model": self.model, "keep_alive": 0}, timeout=30)
            return True
        except (requests.RequestException, ValueError) as e:
            log.debug("Не вдалося вивантажити модель мозку: %s", e)
            return False

    def summarize(self, instruction: str, text: str, num_ctx: int | None = None, num_predict: int = 400,
                  images: list[bytes] | None = None) -> str:
        """Вільний текст (конспект, звіт, відповідь помічника) — без JSON і без дій. ``num_ctx`` — інший розмір
        контексту, ніж CONTEXT (лише якщо справді треба: зміна розміру — це перезавантаження моделі).
        ``images`` — фото до питання (gemma3 бачить картинки)."""
        options: dict = {"temperature": 0.2, "num_predict": num_predict}
        if num_ctx:
            options["num_ctx"] = num_ctx
        user: dict = {"role": "user", "content": text}
        if images:
            user["images"] = [base64.b64encode(image).decode("ascii") for image in images]
        payload = {
            "model": self.model,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": options,
            "messages": [{"role": "system", "content": instruction}, user],
        }
        try:
            return self._ask(payload, self.timeout * 2)["message"]["content"].strip()
        except (requests.RequestException, KeyError, ValueError) as e:
            raise BrainError(f"Ollama недоступна ({self.url}): {e}") from e

    def choose(self, question: str, options: list[str]) -> int | None:
        """Вибір зі списку: номер варіанта (з нуля) або None, якщо жоден не підходить чи мозок недоступний.
        Тільки вибір — нічого вигадати модель тут не може."""
        listing = "\n".join(f"{index}. {option}" for index, option in enumerate(options, 1))
        payload = {
            "model": self.model,
            "stream": False,
            "format": {"type": "object", "properties": {"number": {"type": "integer"}}, "required": ["number"]},
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0, "num_predict": 20},
            "messages": [
                {"role": "system", "content": "Ти обираєш один варіант зі списку. Відповідай JSON {\"number\": N}, "
                                              "де N — номер варіанта, або 0, якщо жоден не підходить."},
                {"role": "user", "content": f"{question}\n\n{listing}"},
            ],
        }
        try:
            number = int(json.loads(self._ask(payload, self.timeout)["message"]["content"])["number"])
        except (requests.RequestException, KeyError, ValueError, TypeError) as e:
            log.warning("Мозок не зміг обрати: %s", e)
            return None
        return number - 1 if 1 <= number <= len(options) else None

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


class BrainMemoryGuard:
    """Раз на хвилину: якщо вільної пам'яті менше LOW_MEMORY_GB — просимо Ollama вивантажити модель (з нею
    й накопичене). Наступне питання до мозку завантажить її знову за кілька секунд."""

    def __init__(self, brain: OllamaBrain, free_gb: Callable[[], float | None], period: float = 60):
        self.brain = brain
        self.free_gb = free_gb
        self.period = period
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="brain-memory", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.period):
            free = self.free_gb()
            if free is not None and free < LOW_MEMORY_GB and self.brain.unload():
                log.warning("Мало вільної пам'яті (%.1f ГБ) — вивантажив модель мозку, наступного разу завантажу знову", free)


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


class ChatBrain:
    """Хто пише неспішні тексти: відповіді на вільні питання в Telegram і підсумки пар після них. Поза парами,
    коли вистачає пам'яті, — більша й розумніша модель (``big``, «розумний режим»; повільніше), а під час пар чи
    коли пам'яті мало — швидка (``small``).

    Дві моделі разом у пам'ять не влазять, тож: перед великою вивантажуємо швидку й чекаємо, поки звільниться
    ``need_gb``; велику тримаємо хвилину між запитами (звіт — це десяток шматків поспіль), а щойно знадобиться
    швидка (рішення мозку, конспект) — спершу прибираємо велику. Увесь час, поки думає велика, швидка чекає на
    спільному замку (``lock``)."""

    SMALL_GB = 3.5  # стільки звільниться, коли вивантажимо швидку
    WARM_SECONDS = 75  # велика тримається між запитами стільки (keep_alive 60 с + запас)

    def __init__(self, small: OllamaBrain, big: OllamaBrain | None, quiet: Callable[[], bool],
                 free_gb: Callable[[], float | None], need_gb: float, lock: threading.RLock | None = None,
                 wait: Callable[[float], None] = time.sleep, clock: Callable[[], float] = monotonic):
        self.small = small
        self.big = big
        self.quiet = quiet
        self.free_gb = free_gb
        self.need_gb = need_gb
        self.lock = lock or threading.RLock()
        self.wait = wait
        self.clock = clock
        self.big_ready = False  # велику модель уже завантажено (див. BrainConnector)
        self._big_used: float | None = None
        if big is not None and hasattr(small, "before"):
            small.before = self._evict_big  # будь-хто кличе швидку — спершу геть велику

    def will_think_long(self) -> bool:
        if self.big is None or not self.big_ready or not self.quiet():
            return False
        free = self.free_gb()
        warm = self._big_used is not None
        return free is None or warm or free + self.SMALL_GB >= self.need_gb

    def _evict_big(self) -> None:
        if self._big_used is None or self.big is None:
            return
        self._big_used = None
        try:
            self.big.unload()
        except Exception as e:  # noqa: BLE001
            log.debug("Велика модель не вивантажилась: %s", e)

    def _room(self) -> bool:
        """Вивантажити швидку й дочекатися, поки звільниться пам'ять для великої (до ~10 с)."""
        if self._big_used is not None and self.clock() - self._big_used < self.WARM_SECONDS:
            return True  # велика ще в пам'яті з попереднього шматка
        try:
            self.small.unload()
        except Exception as e:  # noqa: BLE001 — не вивантажилась: тоді просто не буде місця
            log.debug("Швидка модель не вивантажилась: %s", e)
        free = self.free_gb()
        for _ in range(10):
            free = self.free_gb()
            if free is None or free >= self.need_gb:
                return True
            self.wait(1)
        log.info("Розумний режим: вільно лише %.1f ГБ (треба %.1f) — пише швидка модель", free, self.need_gb)
        return False

    def summarize(self, instruction: str, text: str, **options) -> str:
        if self.will_think_long() and not options.get("images"):
            with self.lock:
                if self.quiet() and self._room():
                    try:
                        answer = self.big.summarize(instruction, text, **options)
                        self._big_used = self.clock()
                        return answer
                    except BrainError as e:
                        log.warning("Велика модель не відповіла (%s) — пише швидка", e)
                        self._evict_big()
                        self._big_used = None
        return self.small.summarize(instruction, text, **options)
