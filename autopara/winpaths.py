"""Windows і кирилиця в імені користувача (C:\\Users\\Денис): рушій Whisper (CTranslate2) на Windows не відкриває
файли моделей за такими шляхами. Тож моделі розпізнавання кладемо в теку з латинським шляхом — спільну
C:\\Users\\Public\\autopara (її може писати будь-який користувач). Викликати до першого імпорту faster_whisper:
huggingface_hub читає HF_HOME, щойно його імпортують.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def ascii_model_cache(home: Path | None = None, public: Path | None = None) -> Path | None:
    """На Windows із нелатинською домашньою текою — HF_HOME у латинську теку. Повертає її (None — не треба)."""
    if sys.platform != "win32" or os.environ.get("HF_HOME"):
        return None
    home = home or Path.home()
    if str(home).isascii():
        return None
    base = public or Path(os.environ.get("PUBLIC") or r"C:\Users\Public")
    folder = base / "autopara" / "huggingface"
    if not str(folder).isascii():
        return None
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    os.environ["HF_HOME"] = str(folder)
    return folder
