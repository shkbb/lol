"""Помічник з навчання в Telegram — не лише про поточну пару.

Старий бот брав «щось із записів» і просив маленьку модель відповісти — вона губилась і вигадувала.
Тепер так:
- **розклад і «ти на парі?»** — відповідь із даних одразу, без мовної моделі (вигадувати там нічого);
- **про пари** — спершу знаходимо потрібне: яку пару мають на увазі («остання», «вчора», «з баз даних»,
  «05.10»), а в ній і в усіх записах — шматки, де про це говорили (пошук за словами з урахуванням відмінків,
  BM25). Модель відповідає лише з цих джерел і каже, звідки взяла: «(Безпека, 05.10 11:21)»; немає в
  джерелах — так і каже;
- **будь-що інше** — звичайна розмова, але з позначкою, що це загальні знання, а не з пари.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from . import notes

log = logging.getLogger(__name__)

HEADER_RE = re.compile(r"^(?P<title>.+?) — (?P<day>\d\d\.\d\d\.\d{4}) (?P<start>\d\d:\d\d)–(?P<end>\d\d:\d\d)(?:, (?P<teacher>.*))?$")
LINE_RE = re.compile(r"^\[(\d\d:\d\d)(?::\d\d)?\]\s*(.*)$")
WORD_RE = re.compile(r"[a-zа-яіїєґ0-9']+", re.I)
STOP = set("""і й та а але або чи що як це той та ті цей ця ці там тут так ну от вже ще же би б не ні на в у з із зі до від по за
про для при над під між через щоб коли де хто який яка яке які мене тебе йому їй нам вам їх я ти він вона воно ми ви вони
мій твій свій наш ваш був була було були є буде будуть бути може можна треба також тобто просто дуже ось оце от""".split())
WEEKDAYS = ["понеділ", "вівтор", "серед", "четвер", "п'ятниц", "субот", "неділ"]
CHUNK_CHARS = 600

SCHEDULE_RE = re.compile(r"розклад|наступн\w* пар|котр\w* (пар|почина|буде)|коли (буде|почина|наступн)|які (в мене |у мене )?пари|"
                         r"скільки (в мене |у мене )?пар|що (в мене |у мене )?(завтра|сьогодні|післязавтра|в понеділ|у вівтор|в серед|"
                         r"в четвер|в п'ятниц)", re.I)
NOW_RE = re.compile(r"\b(зараз|наразі|щойно)\b|ти на парі|що відбувається|скільки (людей|учасник)", re.I)
TASKS_RE = re.compile(r"задал|завдан|дедлайн|здат|домашк|\bлаб|лаборатор|контрольн|модульн|курсов|до якого|термін", re.I)
LESSON_RE = re.compile(r"(останн|минул|попередн)\w*\s+пар|вчора|позавчора|сьогодні|на парі з|що було|про що (була|були|йшлося|говорили)|"
                       r"\d\d?\.\d\d|" + "|".join(WEEKDAYS), re.I)

SYSTEM = (
    "Ти — autopara, помічник студента з навчання в Telegram. Програма сама ходить на онлайн-пари, слухає їх "
    "і зберігає записи; ти допомагаєш студентові розібратися, що було на парах, що задали, і просто відповідаєш "
    "на питання. Пиши українською, по-людськи й коротко (до 120 слів), без вступів.\n"
    "Правила:\n"
    "1. Про пари, завдання, дедлайни й слова викладачів — ЛИШЕ з джерел нижче. Після факту з джерела в дужках "
    "вкажи, звідки він: предмет, дата й час, наприклад «(Безпека, 05.10 11:21)». Розшифровка автоматична, з "
    "помилками розпізнавання: перекручені слова виправляй за змістом, але нічого не домислюй.\n"
    "2. Якщо в джерелах відповіді немає — так і скажи («у записах пар цього немає»). Не вигадуй пар, дат, завдань.\n"
    "3. Якщо питання не про пари (пояснити поняття, порадити, поговорити) — відповідай як розумний співрозмовник і "
    "почни з «📚» (загальні знання, не з пари).\n"
    "4. Ти не можеш нічого робити на парі сам; якщо просять дію — підкажи команду: «екран», «чат», «статус», "
    "«напиши <текст>», «вийди»."
)


def _lemmatizer():
    try:
        import pymorphy3

        analyzer = pymorphy3.MorphAnalyzer(lang="uk")
        cache: dict[str, str] = {}

        def lemma(word: str) -> str:
            if word not in cache:
                cache[word] = analyzer.parse(word)[0].normal_form
            return cache[word]

        return lemma
    except Exception:  # noqa: BLE001 — немає словника: основа слова (перші 6 літер) — гірше, але працює
        return lambda word: word[:6]


_lemma = None


def tokens(text: str) -> list[str]:
    global _lemma
    if _lemma is None:
        _lemma = _lemmatizer()
    words = [word.lower() for word in WORD_RE.findall(text.replace("’", "'").replace("ʼ", "'"))]
    return [_lemma(word) for word in words if word not in STOP and len(word) > 1]


@dataclass
class LessonDoc:
    """Один запис пари, розібраний на частини."""

    path: Path
    title: str
    subject: str
    kind: str
    day: date
    start: str
    end: str
    teacher: str
    summary: str
    lines: list[tuple[str, str]]  # (час «HH:MM», текст)
    chat: list[tuple[str, str]]

    @property
    def short(self) -> str:
        """Коротка назва для посилань: «Безпека», «Бази даних»."""
        words = self.subject.split()
        return " ".join(words[:2]) if len(words) > 2 else self.subject

    def label(self, time: str = "") -> str:
        return f"{self.short}, {self.day:%d.%m}" + (f" {time}" if time else "")


def parse_record(path: Path) -> LessonDoc | None:
    try:
        text = path.read_text(encoding="utf-8").split(notes.LIVE_MARK, 1)[0]
    except OSError:
        return None
    header, _, body = text.partition("\n")
    match = HEADER_RE.match(header.strip())
    if not match:
        return None
    subject, kind = notes.split_title(match["title"])
    summary = ""
    if notes.SUMMARY_MARK in body:
        summary = body.split(notes.SUMMARY_MARK, 1)[1].split(notes.TRANSCRIPT_MARK, 1)[0].strip()
    transcript, _, chat = body.split(notes.TRANSCRIPT_MARK, 1)[-1].partition(notes.CHAT_MARK)

    def lines(block: str) -> list[tuple[str, str]]:
        out = []
        for raw in block.splitlines():
            if m := LINE_RE.match(raw.strip()):
                out.append((m.group(1), m.group(2).strip()))
        return out

    return LessonDoc(path, match["title"], subject, kind, datetime.strptime(match["day"], "%d.%m.%Y").date(),
                     match["start"], match["end"], (match["teacher"] or "").strip(), summary, lines(transcript),
                     lines(chat))


@dataclass
class Chunk:
    doc: LessonDoc
    time: str
    text: str
    terms: Counter = field(default_factory=Counter)


class Library:
    """Усі записи пар з теки «Пари» і пошук у них (BM25 за лемами). Перечитує, коли з'являються нові записи."""

    def __init__(self, root: Path):
        self.root = root
        self.docs: list[LessonDoc] = []
        self.chunks: list[Chunk] = []
        self._seen: tuple = ()
        self._df: Counter = Counter()
        self._avg = 1.0

    def refresh(self) -> None:
        paths = notes.all_records(self.root) if self.root.is_dir() else []
        key = tuple((str(path), path.stat().st_mtime) for path in paths)
        if key == self._seen:
            return
        self._seen = key
        self.docs = sorted((doc for doc in map(parse_record, paths) if doc), key=lambda d: (d.day, d.start))
        self.chunks = []
        for doc in self.docs:
            if doc.summary:
                self.chunks.append(Chunk(doc, doc.start, "Підсумок пари: " + doc.summary))
            current, started = [], ""
            for time, text in doc.lines:
                if not current:
                    started = time
                current.append(text)
                if sum(len(part) for part in current) >= CHUNK_CHARS:
                    self.chunks.append(Chunk(doc, started, " ".join(current)))
                    current = []
            if current:
                self.chunks.append(Chunk(doc, started, " ".join(current)))
            if doc.chat:
                self.chunks.append(Chunk(doc, doc.chat[0][0], "Чат пари: " + " | ".join(text for _, text in doc.chat)))
        self._df = Counter()
        for chunk in self.chunks:
            chunk.terms = Counter(tokens(chunk.text))
            self._df.update(chunk.terms.keys())
        self._avg = sum(sum(c.terms.values()) for c in self.chunks) / max(1, len(self.chunks))

    def search(self, query: str, limit: int = 6, docs: list[LessonDoc] | None = None) -> list[tuple[float, Chunk]]:
        terms = [term for term in tokens(query) if term in self._df]
        if not terms:
            return []
        n = len(self.chunks)
        allowed = {id(doc) for doc in docs} if docs is not None else None
        scored = []
        for chunk in self.chunks:
            if allowed is not None and id(chunk.doc) not in allowed:
                continue
            length = sum(chunk.terms.values()) or 1
            score = 0.0
            for term in terms:
                tf = chunk.terms.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / self._avg))
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: -item[0])
        return scored[:limit]

    def subjects_named(self, question: str) -> list[str]:
        """Предмети, які згадано в питанні: «з тестування» → «Якість і тестування ПЗ», «баз даних» → «…баз даних…».
        Слова, що є в назвах багатьох предметів («програмного забезпечення»), важать мало."""
        asked = set(tokens(question))
        subjects = list(dict.fromkeys(doc.subject for doc in self.docs))
        words = {subject: {t for t in tokens(subject) if len(t) > 3} for subject in subjects}
        df = Counter(term for terms in words.values() for term in terms)
        scores = {subject: sum(1 / df[t] for t in terms & asked) for subject, terms in words.items()}
        best = max(scores.values(), default=0)
        return [subject for subject, score in scores.items() if best >= 0.99 and score == best]


def _day_from(question: str, today: date) -> date | None:
    low = question.lower()
    if "позавчора" in low:
        return today - timedelta(days=2)
    if "вчора" in low:
        return today - timedelta(days=1)
    if "сьогодні" in low:
        return today
    if m := re.search(r"\b(\d\d?)\.(\d\d)\b", low):
        try:
            return date(today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    for index, stem in enumerate(WEEKDAYS):
        if stem in low:
            back = (today.weekday() - index) % 7 or 7  # «у понеділок» про минуле — останній понеділок
            return today - timedelta(days=back)
    return None


class Helper:
    """Відповідь на повідомлення студента. ``ask(system, text)`` — мовна модель; ``schedule(now)`` — розклад текстом."""

    def __init__(self, library: Library, ask: Callable[[str, str], str] | None,
                 schedule: Callable[[datetime], str] | None = None):
        self.library = library
        self.ask = ask
        self.schedule = schedule

    def route(self, question: str) -> str:
        if SCHEDULE_RE.search(question) and not TASKS_RE.search(question):
            return "schedule"
        if TASKS_RE.search(question):
            return "tasks"
        if LESSON_RE.search(question):
            return "lesson"
        if NOW_RE.search(question):
            return "now"
        return "search"

    def answer(self, question: str, now: datetime, live: str = "", dialog: str = "", events: str = "") -> str:
        kind = self.route(question)
        if kind == "schedule" and self.schedule is not None:
            return self.schedule(now)
        self.library.refresh()
        sources = self.sources(question, kind, now, live)
        if self.ask is None:
            return "Мовна модель зараз недоступна. " + (("Ось що знайшов у записах:\n" + sources[:1500]) if sources else "")
        parts = [f"Зараз {now:%d.%m.%Y %H:%M}, {['понеділок', 'вівторок', 'середа', 'четвер', 'пʼятниця', 'субота', 'неділя'][now.weekday()]}."]
        if self.schedule is not None:
            parts.append(self.schedule(now))
        if events:
            parts.append(events)
        if dialog:
            parts.append("Розмова перед цим:\n" + dialog)
        parts.append("Джерела:\n" + (sources or "(у записах пар про це нічого не знайшлося)"))
        parts.append(f"Студент пише: {question}")
        return self.ask(SYSTEM, "\n\n".join(parts))

    def sources(self, question: str, kind: str, now: datetime, live: str = "", budget: int = 4000) -> str:
        library = self.library
        out: list[str] = []
        if kind == "now" or (live and kind in ("lesson", "search") and re.search(r"зараз|на парі|викладач", question, re.I)):
            if live:
                out.append("Поточна пара (наживо):\n" + live)
        picked = self._pick_docs(question, now) if kind in ("lesson", "tasks") else []
        day = _day_from(question, now.date()) if kind in ("lesson", "tasks") else None
        if day is not None and not picked:
            out.append(f"За {day:%d.%m.%Y} записів пар немає (програма тоді не була на парах або нічого не чула).")
        for doc in picked[-3:]:
            head = f"[{doc.title}, {doc.day:%d.%m.%Y} {doc.start}–{doc.end}, {doc.teacher}]"
            body = doc.summary or "Підсумку немає. Початок: " + " ".join(text for _, text in doc.lines[:12])[:600]
            out.append(f"{head}\n{body}")
        scope = picked or None
        if kind == "tasks":
            query = question + " завдання лабораторна здати дедлайн до"
        else:
            query = question
        for _, chunk in library.search(query, limit=6, docs=scope):
            out.append(f"[{chunk.doc.label(chunk.time)}] {chunk.text}")
        if not out and kind in ("lesson", "tasks") and library.docs and day is None:
            latest = library.docs[-3:] if kind == "tasks" else library.docs[-1:]
            for doc in latest:
                out.append(f"[{doc.title}, {doc.day:%d.%m.%Y} {doc.start}–{doc.end}]\n{doc.summary or '(без підсумку)'}")
        text = ""
        for piece in out:
            piece = piece if len(piece) <= 1600 else piece[:1600] + "…"  # довгий підсумок не витісняє решту джерел
            if len(text) + len(piece) > budget:
                break
            text += piece + "\n\n"
        return text.strip()

    def _pick_docs(self, question: str, now: datetime) -> list[LessonDoc]:
        """Які записи пар мають на увазі: предмет, дата («вчора», «05.10», «у вівторок»), «остання/минула пара»."""
        docs = self.library.docs
        if not docs:
            return []
        named = set(self.library.subjects_named(question))
        if named:
            docs = [doc for doc in docs if doc.subject in named]
        day = _day_from(question, now.date())
        if day is not None:
            docs = [doc for doc in docs if doc.day == day]  # того дня записів немає — так і скажемо
        if re.search(r"(останн|минул|попередн)\w*\s+пар", question, re.I) and day is None:
            return docs[-1:]
        if named or day is not None:
            return docs[-5:]
        return []
