"""Моделі заздалегідь: інсталятор кличе `autopara models`, щоб перша пара не чекала, поки докачаються гігабайти.

Які саме — вирішує те саме, що й програма на цьому комп'ютері: розпізнавання наживо й після пари
(ears.resolve_model / resolve_refine_model), мовна модель мозку, модель пошуку за змістом і, якщо ввімкнено,
розумний режим. Що вже є — пропускаємо; чого не вийшло (немає інтернету, Ollama, місця) — програма докачає сама,
коли знадобиться. Перервати можна будь-коли (Ctrl+C) — нічого не зламається.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from .config import Config

SPARE_GB = 2.0  # стільки місця лишити на диску після завантаження
# Ті самі файли, що бере faster_whisper.utils.download_model, — тоді програма знайде модель у кеші.
WHISPER_FILES = ["config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*"]
OLLAMA_WAIT = 60  # с: Ollama щойно встановили — дати їй запуститися

Say = Callable[[str], None]


def whisper_models(config: Config) -> list[tuple[str, str]]:
    """[(модель, навіщо)] для розпізнавання мови — без повторів."""
    from . import accel
    from .ears import resolve_model, resolve_refine_model

    device = accel.whisper_device(config.ears.device)
    live = resolve_model(config.ears.model, device)
    refine = resolve_refine_model(config.ears.refine_model, config.ears.model, device)
    models = [(live, "розпізнавання мови на парі")]
    if refine not in ("off", live):
        models.append((refine, "точніше розпізнавання після пари"))
    return models


def ollama_models(config: Config) -> list[tuple[str, str]]:
    brain = config.brain
    wanted = [(brain.model, "мозок: сповіщення, звіти після пар, відповіді бота"),
              (brain.embed_model, "пошук у записах пар за змістом"),
              (brain.chat_model, "розумний режим поза парами")]
    models: list[tuple[str, str]] = []
    for name, why in wanted:
        if name and name not in [known for known, _ in models]:
            models.append((name, why))
    return models


def _free_gb(folder: Path) -> float:
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return shutil.disk_usage(folder).free / 1e9


def _tree_bytes(folder: Path) -> int:
    total = 0
    for root, _, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            try:
                if not os.path.islink(path):  # знімки — посилання на ті самі файли, не рахуємо двічі
                    total += os.path.getsize(path)
            except OSError:
                pass
    return total


def _watch(folder: Path, total: int, progress: Callable[[str], None]) -> Callable[[], None]:
    """Поступ завантаження за тим, як росте тека моделі. Повертає «зупинити»."""
    stop = threading.Event()
    start = _tree_bytes(folder)

    def loop() -> None:
        shown = ""
        while not stop.wait(1):
            percent = min(int((_tree_bytes(folder) - start) * 100 / total), 99)
            text = f"  {percent}% з {total / 1e9:.1f} ГБ"
            if text != shown:
                shown = text
                progress(text)

    threading.Thread(target=loop, name="prefetch-progress", daemon=True).start()
    return stop.set


def fetch_whisper(name: str, why: str, say: Say, progress: Callable[[str], None] | None = None) -> bool:
    # Звичайне завантаження (без Xet) пише файл поступово — видно поступ. Діє лише в цьому процесі (`autopara models`):
    # huggingface_hub читає ці змінні, коли його імпортують.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")  # свій поступ — у тому ж вигляді, що й для Ollama
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)  # без «unauthenticated requests» у вікні інсталятора
    try:
        import huggingface_hub
        from faster_whisper.utils import _MODELS
        from huggingface_hub import constants
    except ImportError:
        say(f"• {name} ({why}): розпізнавання мови не встановлене — пропускаю (доставити можна кнопкою у вікні програми)")
        return False
    repo = name if "/" in name else _MODELS.get(name)
    if repo is None:
        say(f"• {name}: невідома модель розпізнавання — пропускаю")
        return False
    try:
        huggingface_hub.snapshot_download(repo, allow_patterns=WHISPER_FILES, local_files_only=True)
        say(f"• {name} ({why}): вже є")
        return True
    except Exception:  # noqa: BLE001 — у кеші немає (або неповна) — завантажуємо
        pass
    size = 0
    try:
        info = huggingface_hub.HfApi().model_info(repo, files_metadata=True)
        size = sum((file.size or 0) for file in info.siblings or []
                   if any(fnmatch.fnmatch(file.rfilename, pattern) for pattern in WHISPER_FILES))
    except Exception:  # noqa: BLE001 — розмір лише для поступу й перевірки місця
        pass
    cache = Path(constants.HF_HUB_CACHE)
    free = _free_gb(cache)
    if size and free < size / 1e9 + SPARE_GB:
        say(f"• {name} ({why}): на диску лише {free:.1f} ГБ, а треба ~{size / 1e9:.1f} ГБ — пропускаю")
        return False
    say(f"• {name} ({why}): завантажую" + (f" ~{size / 1e9:.1f} ГБ" if size else "") + "...")
    stop = _watch(cache / ("models--" + repo.replace("/", "--")), size, progress) if size and progress else (lambda: None)
    try:
        huggingface_hub.snapshot_download(repo, allow_patterns=WHISPER_FILES)
    except Exception as e:  # noqa: BLE001
        say(f"  не вийшло ({e}) — програма докачає сама")
        return False
    finally:
        stop()
    if size and progress:
        progress(f"  100% з {size / 1e9:.1f} ГБ")
    say("  готово")
    return True


def ollama_ready(url: str, session: requests.Session, wait: float = OLLAMA_WAIT,
                 installed: Callable[[], bool] = lambda: shutil.which("ollama") is not None,
                 sleep: Callable[[float], None] = time.sleep) -> bool:
    """Ollama відповідає. Щойно встановлена ще може запускатися — тоді трохи чекаємо; не встановлена — ні."""
    deadline = time.monotonic() + (wait if installed() else 0)
    while True:
        try:
            if session.get(f"{url}/api/tags", timeout=5).ok:
                return True
        except requests.RequestException:
            pass
        if time.monotonic() >= deadline:
            return False
        sleep(2)


def fetch_ollama(url: str, name: str, why: str, say: Say, session: requests.Session,
                 progress: Callable[[str], None] | None = None) -> bool:
    try:
        names = {model.get("name") for model in session.get(f"{url}/api/tags", timeout=10).json().get("models", [])}
    except (requests.RequestException, ValueError):
        names = set()
    if name in names or f"{name}:latest" in names:
        say(f"• {name} ({why}): вже є")
        return True
    say(f"• {name} ({why}): завантажую...")
    shown = ""
    try:
        with session.post(f"{url}/api/pull", json={"model": name, "stream": True}, stream=True, timeout=(10, 600)) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                status = json.loads(line)
                if status.get("error"):
                    raise RuntimeError(status["error"])
                total, done = status.get("total") or 0, status.get("completed") or 0
                if total >= 50e6 and progress is not None:  # лише великі частини, не дрібні файли налаштувань
                    text = f"  {int(done * 100 / total)}% з {total / 1e9:.1f} ГБ"
                    if text != shown:
                        shown = text
                        progress(text)
                if status.get("status") == "success":
                    say("  готово")
                    return True
    except (requests.RequestException, ValueError, RuntimeError) as e:
        say(f"  не вийшло ({e}) — програма докачає сама, коли Ollama працюватиме")
        return False
    say("  не вийшло (Ollama не підтвердила) — програма докачає сама")
    return False


def _progress_line() -> Callable[[str], None] | None:
    """Поступ в одному рядку, що оновлюється (лише в справжньому терміналі)."""
    if not sys.stdout.isatty():
        return None

    def show(text: str) -> None:
        print("\r" + text.ljust(30), end="", flush=True)
        if text.startswith("  100%"):
            print()
    return show


def run(config: Config, say: Say = print, session: requests.Session | None = None) -> bool:
    """Завантажити все, чого бракує. True — усе на місці."""
    session = session or requests.Session()
    ok = True
    try:
        if config.ears.enabled:
            for name, why in whisper_models(config):
                ok = fetch_whisper(name, why, say, _progress_line()) and ok
        if config.brain.enabled:
            models = ollama_models(config)
            if ollama_ready(config.brain.url, session):
                for name, why in models:
                    ok = fetch_ollama(config.brain.url, name, why, say, session, _progress_line()) and ok
            else:
                ok = False
                say("• Мовні моделі (" + ", ".join(name for name, _ in models) + "): Ollama не запущена — "
                    "програма завантажить їх сама, щойно Ollama запрацює.")
    except KeyboardInterrupt:
        say("\nПерервано — решту програма докачає сама, коли знадобиться.")
        return False
    say("Усі моделі на місці." if ok else "Не все вийшло — решту програма докачає сама, коли знадобиться.")
    return ok
