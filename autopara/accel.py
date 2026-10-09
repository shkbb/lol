"""Відеокарта: програма сама дізнається, що в комп'ютері, і бере для розпізнавання мови те прискорення, яке там справді є.

- **NVIDIA** — Whisper на відеокарті (CUDA): у рази швидше, тож і наживо можна тримати найточнішу модель large-v3.
  Бібліотеки CUDA ставить інсталятор пакетами pip (див. ``pip_extras``), окремо нічого ставити не треба.
- **Intel, AMD, Apple** — Whisper на процесорі: faster-whisper інших карт не вміє, а whisper.cpp на вбудованій Intel
  (Vulkan) виявився повільнішим за процесор (замір: large-v3 — 170 с проти 140 с на 107 с звуку).
- **Мовна модель (Ollama)** на вбудованій карті через Vulkan «вішає» відеокарту (i915: GPU HANG, ErrorDeviceLost) і
  ще й тече пам'яттю — тож там її теж тримаємо на процесорі (``brain_on_gpu``). На окремих картах Ollama працює сама.

Вибір перевіряємо пробним розпізнаванням (ears.WhisperTranscriber): не вийшло — тихо лишаємось на процесорі.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x1022": "amd", "0x8086": "intel"}
NAMES = (("nvidia", "nvidia"), ("geforce", "nvidia"), ("quadro", "nvidia"), ("rtx", "nvidia"), ("radeon", "amd"),
         ("amd", "amd"), ("intel", "intel"), ("apple", "apple"))
DEVICES = ("auto", "cpu", "cuda")
# Скільки відеопам'яті треба Whisper (ГБ) — з запасом на звук і пошук варіантів (beam).
VRAM_FLOAT16 = 6.0  # large-v3 у float16 — найточніше
VRAM_INT8 = 3.0  # large-v3 у int8_float16 — майже так само точно, удвічі менше пам'яті
VRAM_TURBO = 1.8  # large-v3-turbo у int8_float16; менше — на процесорі


@dataclass(frozen=True)
class Gpu:
    vendor: str  # nvidia | amd | intel | apple | other
    name: str
    integrated: bool = False


@dataclass(frozen=True)
class Device:
    """Де й як рахувати Whisper (аргументи faster_whisper.WhisperModel) і як це назвати людині."""

    device: str  # cuda | cpu
    compute_type: str  # float16 | int8_float16 | int8
    label: str
    memory_gb: float = 0.0  # відеопам'ять, яку можна взяти

    @property
    def gpu(self) -> bool:
        return self.device != "cpu"


CPU = Device("cpu", "int8", "процесор")


def _vendor_of(name: str) -> str:
    lower = name.lower()
    return next((vendor for word, vendor in NAMES if word in lower), "other")


@lru_cache(maxsize=1)
def gpus() -> tuple[Gpu, ...]:
    """Відеокарти комп'ютера (порожньо — не вдалося дізнатися)."""
    try:
        if sys.platform == "darwin":
            return (Gpu("apple", "Apple Silicon", integrated=True),) if platform.machine() == "arm64" else ()
        if sys.platform == "win32":
            return tuple(_windows_gpus())
        return tuple(_linux_gpus())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log.debug("Не вдалося дізнатися відеокарту: %s", e)
        return ()


def _linux_gpus(root: Path = Path("/sys/class/drm")) -> list[Gpu]:
    found = []
    for card in sorted(root.glob("card[0-9]*")):
        device = card / "device"
        if "-" in card.name or not (device / "vendor").exists():
            continue  # card1-HDMI-A-1 — це роз'єм, а не карта
        vendor = VENDORS.get((device / "vendor").read_text().strip(), "other")
        uevent = (device / "uevent").read_text() if (device / "uevent").exists() else ""
        driver = next((line.split("=", 1)[1] for line in uevent.splitlines() if line.startswith("DRIVER=")), "?")
        # Вбудована: Intel без власної пам'яті (Arc має mem_info_vram_total) і AMD-«APU» (власна пам'ять < 2 ГБ).
        vram = device / "mem_info_vram_total"
        integrated = (vendor == "intel" and not vram.exists()) or (
            vendor == "amd" and vram.exists() and int(vram.read_text().strip() or 0) < 2 * 2**30)
        found.append(Gpu(vendor, f"{vendor} ({driver})", integrated))
    return found


def _windows_gpus() -> list[Gpu]:
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name"],
        capture_output=True, text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [Gpu(_vendor_of(name), name, integrated=any(word in name.lower() for word in ("uhd", "iris", "(tm) graphics")))
            for name in names if not any(word in name.lower() for word in ("basic display", "remote", "virtual"))]


def has_nvidia() -> bool:
    return any(card.vendor == "nvidia" for card in gpus()) or shutil.which("nvidia-smi") is not None


def nvidia_memory() -> tuple[str, float, float] | None:
    """(назва, усього ГБ, вільно ГБ) першої карти NVIDIA — з nvidia-smi (ставиться разом із драйвером)."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return None
    try:
        result = subprocess.run([smi, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        name, total, free = (part.strip() for part in result.stdout.splitlines()[0].split(","))
        return name, float(total) / 1024, float(free) / 1024
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def brain_on_gpu() -> bool:
    """Чи пускати мовну модель (Ollama) на відеокарту. Лише на окрему: на вбудованій (Intel, AMD-APU) Vulkan вішає
    карту — разом із нею смикається й екран — і тече пам'яттю. Mac (Metal) і невідоме — як вирішить сама Ollama."""
    cards = gpus()
    if sys.platform == "darwin" or not cards:
        return True
    return any(not card.integrated for card in cards)


def pip_extras() -> str:
    """Додаткові пакети для інсталятора: на комп'ютері з NVIDIA — ще й бібліотеки CUDA для Whisper (~1,5 ГБ)."""
    return "ears,cuda" if has_nvidia() and sys.platform != "darwin" else "ears"


def _nvidia_library_dirs() -> list[Path]:
    """Теки з бібліотеками CUDA, поставленими pip (пакети nvidia-cublas-cu12, nvidia-cudnn-cu12)."""
    try:
        import nvidia  # type: ignore[import-not-found]
    except ImportError:
        return []
    dirs = []
    for base in getattr(nvidia, "__path__", []):
        for package in ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc"):
            for name in ("lib", "bin"):
                if (folder := Path(base) / package / name).is_dir():
                    dirs.append(folder)
    return dirs


_loaded = False


def load_cuda_libraries() -> None:
    """Підвантажити бібліотеки CUDA з пакетів pip, щоб CTranslate2 їх знайшов (без LD_LIBRARY_PATH і змін у системі)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for folder in _nvidia_library_dirs():
        if sys.platform == "win32":
            os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"
            os.add_dll_directory(str(folder))
            continue
        # cuDNN 9 підвантажує свої частини сам — тож вантажимо всі, щоб їх уже було видно за назвою.
        for library in sorted(folder.glob("lib*.so*")):
            try:
                ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
            except OSError as e:
                log.debug("CUDA: не завантажилась %s: %s", library.name, e)


def whisper_device(setting: str = "auto", free: bool = False) -> Device:
    """Де рахувати Whisper: "auto" — відеокарта NVIDIA, якщо є і їй вистачає пам'яті, інакше процесор.

    ``free`` — рахувати лише вільну відеопам'ять (уточнення після пари: модель вух наживо вже сидить на карті).
    """
    if setting == "cpu" or (setting == "auto" and not has_nvidia()):
        return CPU
    load_cuda_libraries()
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            log.info("Відеокарта NVIDIA є, але CUDA недоступна (драйвер?) — розпізнаю на процесорі")
            return CPU
    except (ImportError, RuntimeError) as e:
        log.info("CUDA недоступна (%s) — розпізнаю на процесорі", e)
        return CPU
    memory = nvidia_memory()
    name, total, spare = memory if memory else ("NVIDIA", VRAM_INT8, VRAM_INT8)
    usable = spare if free else total
    if usable < VRAM_TURBO and setting == "auto":
        log.info("На відеокарті %s лише %.1f ГБ пам'яті — розпізнаю на процесорі", name, usable)
        return CPU
    compute = "float16" if usable >= VRAM_FLOAT16 else "int8_float16"
    return Device("cuda", compute, f"{name} (CUDA)", usable)


def live_model(device: Device, cpus: int, ram_gb: float) -> str:
    """Модель для вух наживо. На відеокарті — large-v3 (там вона встигає), якщо влазить; на процесорі — turbo
    (large-v3 там відставала від пари на 10+ хв) або small на слабкому комп'ютері."""
    if device.gpu:
        return "large-v3" if device.memory_gb >= VRAM_INT8 else "large-v3-turbo"
    return "large-v3-turbo" if cpus >= 4 and ram_gb >= 8 else "small"
