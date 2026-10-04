"""Записи пар: кожна пара — файл у теці свого предмета (~/.autopara/Пари/<Предмет>/<дата час> <тип>.txt).

Маленька мовна модель не втримає в голові всі пари, тож на питання вона сама дивиться в записи: визначає,
про який предмет мова, бере підсумки кількох останніх пар цього предмета й уривки, де трапляються слова
з питання. Усе лежить лише на цьому комп'ютері.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Callable

FOLDER = "Пари"
SUMMARY_MARK = "--- Підсумок ---"
TRANSCRIPT_MARK = "--- Розшифровка ---"
CHAT_MARK = "--- Чат ---"
TITLE_RE = re.compile(r"^(.*?)\s*\(([^()]{1,12})\)\s*$")  # «Бази даних (Лк)» → «Бази даних», «Лк»
NOW_RE = re.compile(r"зараз|сьогодні|ця пара|цій парі|цієї пари|на парі|викладач|сейчас|сегодня", re.I)


def split_title(title: str) -> tuple[str, str]:
    match = TITLE_RE.match(title.strip())
    return (match.group(1).strip(), match.group(2).strip()) if match else (title.strip(), "")


def safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", name).strip().strip(".") or "Без назви"


def lesson_path(root: Path, title: str, start: datetime) -> Path:
    subject, kind = split_title(title)
    return root / safe(subject) / f"{start:%Y-%m-%d %H-%M}{' ' + safe(kind) if kind else ''}.txt"


def write(path: Path, header: str, summary: str, transcript: list[str], chat: list[str]) -> None:
    lines = [header, ""]
    if summary:
        lines += [SUMMARY_MARK, summary.strip(), ""]
    lines += [TRANSCRIPT_MARK, *transcript]
    if chat:
        lines += ["", CHAT_MARK, *chat]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_summary(path: Path, summary: str) -> None:
    """Підсумок від мозку приходить після пари — дописуємо його вгору вже збереженого запису."""
    text = path.read_text(encoding="utf-8")
    header, _, body = text.partition("\n")
    if SUMMARY_MARK in body:
        body = TRANSCRIPT_MARK + body.split(TRANSCRIPT_MARK, 1)[1] if TRANSCRIPT_MARK in body else ""
    path.write_text(f"{header}\n\n{SUMMARY_MARK}\n{summary.strip()}\n\n{body.lstrip()}", encoding="utf-8")


def subjects(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def records(root: Path, subject: str) -> list[Path]:
    """Записи предмета від найстарішого до найновішого (у назві файла — дата й час)."""
    folder = root / subject
    return sorted(folder.glob("*.txt")) if folder.is_dir() else []


def _fold(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split())


def _stems(text: str) -> set[str]:
    """Основи значущих слів: перші 5 літер слів від 4 літер («баз даних» ≈ «Бази даних», «тестування» ≈ «тестуванню»)."""
    return {word[:5] for word in _fold(text).split() if len(word) >= 4}


def find_subject(question: str, known: list[str], current: str = "",
                 choose: Callable[[str, list[str]], int | None] | None = None) -> str:
    """Про який предмет питання: названий у питанні → поточна пара → спитати мозок, обравши зі списку."""
    asked = _stems(question)
    scores = {subject: len(_stems(subject) & asked) for subject in known}
    best = max(scores, key=scores.get, default="")
    if best and scores[best] > 0:
        return best
    if current and (current in known or NOW_RE.search(question)):
        return current
    if choose is not None and known:
        # «джава» ≈ «…JAVA» — такого збігу слів не видно, а мозок зрозуміє. Останній варіант — «загальне питання».
        options = known + ["жоден конкретний — питання про пари загалом"]
        index = choose(f"Студент питає: «{question}». Про який предмет це питання?", options)
        if index is not None and index < len(known):
            return known[index]
    return current


def summary_of(path: Path, chars: int = 700) -> str:
    """Підсумок пари з запису, а якщо його немає (мозок був вимкнений) — кінець розшифровки."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    header, _, body = text.partition("\n")
    if SUMMARY_MARK in body:
        summary = body.split(SUMMARY_MARK, 1)[1].split(TRANSCRIPT_MARK, 1)[0].strip()
        return f"{header}\nПідсумок: {summary[:chars]}"
    transcript = body.split(TRANSCRIPT_MARK, 1)[-1].split(CHAT_MARK, 1)[0].strip()
    return f"{header}\nКінець розшифровки: …{transcript[-chars:]}" if transcript else header


def snippets(root: Path, question: str, limit: int = 4, chars: int = 300) -> list[str]:
    """Місця в усіх записах, де трапляються слова з питання (без назв предметів — їх і так видно)."""
    words = _stems(question) - set().union(*(_stems(s) for s in subjects(root)))
    if not words or not root.is_dir():
        return []
    found = []
    for path in sorted(root.glob("*/*.txt")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines[2:], 2):
            hits = len(words & _stems(line))
            if hits:
                around = " ".join(lines[max(2, index - 1): index + 2])
                found.append((hits, path.name, f"{lines[0]} — {path.parent.name}:\n{around[:chars]}"))
    found.sort(key=lambda item: item[1], reverse=True)  # новіші записи — першими…
    found.sort(key=lambda item: -item[0])  # …серед тих, де збігів найбільше
    return [text for _, _, text in found[:limit]]


def gather(root: Path, question: str, current: str = "",
           choose: Callable[[str, list[str]], int | None] | None = None, budget: int = 3500) -> str:
    """Що з записів показати мозку для цього питання — не більше ``budget`` символів."""
    known = subjects(root)
    if not known:
        return ""
    subject = find_subject(question, known, split_title(current)[0] if current else "", choose)
    if subject in known:
        latest = records(root, subject)[-5:]
        title = f"Останні пари з предмета «{subject}» — за датою, остання внизу:"
    else:  # питання про пари загалом — останні з усіх предметів
        latest = sorted(root.glob("*/*.txt"), key=lambda path: path.name)[-4:]
        title = "Останні пари — за датою, остання внизу:"
    # Маленька модель плутається, коли новіше йде першим, — тож хронологічно, а найновішу позначаємо.
    # Якщо не влазить у бюджет — відкидаємо найстаріші.
    parts: list[str] = []
    used = 0
    for index, path in enumerate(reversed(latest)):
        piece = summary_of(path) + ("\n← це найновіша пара" if index == 0 else "")
        if used + len(piece) > budget * 2 // 3:
            break
        parts.insert(0, piece)
        used += len(piece)
    if parts:
        parts.insert(0, title)
    found = snippets(root, question)
    if found:
        extra = []
        for piece in found:
            if used + len(piece) > budget:
                break
            extra.append(piece)
            used += len(piece)
        if extra:
            parts.append("Згадки в записах пар:\n" + "\n\n".join(extra))
    return "\n\n".join(parts)
