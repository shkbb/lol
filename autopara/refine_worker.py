"""Окремий процес для уточнення розшифровки після пари (див. refine.py).

Велика модель (large-v3) займає кілька гігабайт, а Python після неї пам'ять системі не повертає — у самій
програмі так двічі закінчилась пам'ять і зависав увесь ноутбук. Тож модель живе лише тут: процес
розшифровує один запис, пише результат і завершується, віддаючи все. Під тиском пам'яті ядро вб'є першим
саме його (не програму й не робочий стіл), а процесор він бере з нижчим пріоритетом.

    python -m autopara.refine_worker <завдання.json> --model large-v3 --language uk --beam 5 --hint "…" --device auto

Результат — <завдання>.result.json: [[секунда від початку запису, текст], …].
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path


def be_gentle() -> None:
    """Нижчий пріоритет процесора; на Linux — ще й першим під ніж, якщо закінчиться пам'ять."""
    try:
        Path("/proc/self/oom_score_adj").write_text("1000")
    except OSError:
        pass
    if hasattr(os, "nice"):
        try:
            os.nice(10)
        except OSError:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes

            below_normal = 0x00004000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal)
        except (OSError, AttributeError):
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Уточнити розшифровку однієї пари (для autopara)")
    parser.add_argument("job")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--language", default="uk")
    parser.add_argument("--beam", type=int, default=5)
    parser.add_argument("--hint", default="")
    parser.add_argument("--device", default="auto")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s уточнення: %(message)s", datefmt="%H:%M:%S")
    be_gentle()

    from .ears import WhisperTranscriber

    job = Path(args.job)
    pcm = job.with_suffix(".raw").read_bytes()
    # free_memory: вуха наживо вже тримають свою модель на відеокарті — беремо карту, лише якщо на ній ще є місце.
    transcriber = WhisperTranscriber(args.model, args.language, device=args.device, free_memory=True)
    lines = transcriber.transcribe_long(pcm, args.hint, beam_size=args.beam)
    result = job.with_suffix(".result.json")
    partial = result.with_suffix(".tmp")
    partial.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    partial.replace(result)  # результат з'являється цілим — або не з'являється зовсім
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
