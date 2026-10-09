"""Помічник з навчання в Telegram — не лише про поточну пару.

Маленька мовна модель (4B) — найслабша ланка: вона губиться, плутає пари й вигадує. Тож вона робить якомога менше:
- **розклад** — відповідь із даних, без моделі;
- **що було на парі, що задали, дедлайни** — прямо з підсумків пар (розділи «Про що», «Завдання й дедлайни»):
  яку пару мають на увазі («остання», «вчора», «з баз даних», «05.10»), знаходимо самі, а модель не потрібна —
  отже, й вигадати нічого не може;
- **коли/що казали про щось** — шукаємо в усіх записах шматки, де про це говорили (пошук за словами з урахуванням
  відмінків, BM25), модель коротко відповідає лише з них, а звідки це — дописуємо самі: «Безпека, 09.10 12:55»;
- **будь-що інше** — звичайна розмова; пояснення понять — з позначкою «📚» (загальні знання, не з пари).
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
# Слова самого питання («що казали про OWASP на парі») — шукати їх у записах нема сенсу.
QUESTION_LEMMAS = {"казати", "сказати", "говорити", "розповідати", "пояснювати", "пара", "викладач", "саме", "взагалі",
                   "бути", "коли", "що", "який", "останній", "минулий"}
TRANSLIT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh",
                          "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n",
                          "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
                          "ч": "ch", "ш": "sh", "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "'": "", "ё": "e", "ы": "y",
                          "э": "e", "ъ": ""})
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
    "Ти — autopara, помічник студента з навчання в Telegram. Програма сама ходить на онлайн-пари, слухає їх і "
    "зберігає записи. Пиши українською, по-людськи й коротко: 2–4 речення, своїми словами, без вступів, без "
    "заголовків і без розмітки (жодних ** чи #).\n"
    "Якщо є уривки з записів пар — відповідай на питання з них, але не переписуй їх. Розшифровка автоматична, з "
    "помилками розпізнавання: перекручені слова й терміни виправляй за змістом (ОВАСП — OWASP), відомий термін можеш "
    "пояснити одним реченням. Не вигадуй пар, людей, дат, завдань і дедлайнів; чого в уривках немає — так і скажи.\n"
    "Якщо уривків немає і питання не про пари — відповідай як розумний співрозмовник.\n"
    "Ти не можеш нічого робити на парі сам; якщо просять дію — підкажи команду: «екран», «чат», «статус», "
    "«напиши <текст>», «вийди»."
)
SECTION_RE = re.compile(r"^\**\s*(Про що|Що робили|Завдання й дедлайни|Важливе|Чат)\s*:?\**\s*:?\s*(.*)$")
# «Завдання: не було», «Важливе: нічого» — показувати нема чого.
NOTHING_RE = re.compile(r"^(не було|немає|нема|нічого|не задано|не оголошено|не вказано|—|-)\b", re.I)
TEACHER_RE = re.compile(r"хто (викладає|веде|читає|викладач)|як\w* (звати|звуть) викладач|викладач\w* (з|по|у|в)\b", re.I)
SMALL_TALK_RE = re.compile(r"^\W*(привіт|вітаю|добр(ий|ого|е|ий день|ого ранку)|хай|hello|hi|дякую|дяки|спасибі|як справи|"
                           r"як ти|ти тут|хто ти|що ти вмієш|ок|окей|добре|клас|супер)\b", re.I)
EXPLAIN_RE = re.compile(r"що так\w*|поясни|як працю|навіщо|чим відрізня|розкажи про|що означа|приклад", re.I)
PLACEHOLDER_RE = re.compile(r"\[[^\]]{2,40}\]")
ABSENT_RE = re.compile(r"^(дедлайн|термін)\w*[^.]*\bне (було )?(вказан|встановлен|оголошен|названо|надано)", re.I)
ABOUT_LESSONS_RE = re.compile(r"казал|казав|сказал|говорил|розповідал|пояснювал|на пар|викладач|в записах|у записах", re.I)
SEARCH_STRONG = 3.0  # BM25: нижче — у записах про це майже нічого, тож це загальне питання
WEEKDAY_NAMES = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]


def sections(summary: str) -> dict[str, str]:
    """Розділи підсумку пари: {"Про що": …, "Завдання й дедлайни": …}."""
    found: dict[str, str] = {}
    current = ""
    for raw in summary.splitlines():
        line = raw.strip()
        if m := SECTION_RE.match(line):
            current = m.group(1)
            found[current] = m.group(2).strip().strip("*").strip()
        elif current and line:
            found[current] = (found[current] + " " + line).strip()
    # Мовна модель іноді лишає заповнювачі («подати до [дата]») — таке речення нічого не каже, а лише плутає.
    clean = {name: " ".join(part for part in re.split(r"(?<=[.!?])\s+", text)
                            if not PLACEHOLDER_RE.search(part) and not ABSENT_RE.match(part))
             for name, text in found.items()}
    return {name: text for name, text in clean.items() if text}


def _shorten(text: str, limit: int) -> str:
    """Перші речення до ``limit`` знаків."""
    if len(text) <= limit:
        return text
    out = ""
    for part in re.split(r"(?<=[.!?])\s+", text):
        if out and len(out) + len(part) > limit:
            break
        out = f"{out} {part}".strip()
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


def plain(text: str) -> str:
    """Telegram показує текст як є: прибрати розмітку маркдауну, яку любить мовна модель."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"^\s*[*-]\s+", "• ", text, flags=re.M)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    return text.strip()


def whole_sentences(text: str) -> str:
    """Модель обірвалась на ліміті слів — лишити лише повні речення."""
    text = text.strip()
    if not text or text[-1] in ".!?…)»\"" or "\n" not in text and len(text) < 200:
        return text
    cut = max(text.rfind(mark) for mark in (". ", "! ", "? ", ".\n", "!\n", "?\n"))
    return text[:cut + 1].strip() if cut > len(text) // 3 else text + "…"


def _lemmatizer():
    """(лема слова, чи слово є в словнику)."""
    try:
        import pymorphy3

        analyzer = pymorphy3.MorphAnalyzer(lang="uk")
        cache: dict[str, tuple[str, bool]] = {}

        def lemma(word: str) -> tuple[str, bool]:
            if word not in cache:
                cache[word] = (analyzer.parse(word)[0].normal_form, analyzer.word_is_known(word))
            return cache[word]

        return lemma
    except Exception:  # noqa: BLE001 — немає словника: основа слова (перші 6 літер) — гірше, але працює
        return lambda word: (word[:6], True)


_lemma = None


def sound(word: str) -> str:
    """Як слово звучить: англійські терміни Whisper пише то латиницею, то кирилицею («OWASP», «ОВАСП», «Ovasp»).
    Транслітерація й приголосні — однаковий ключ для всіх трьох."""
    w = word.lower().translate(TRANSLIT)
    for a, b in (("ph", "f"), ("gh", ""), ("ck", "k"), ("kh", "h"), ("w", "v"), ("c", "k"), ("q", "k"), ("x", "ks"), ("g", "h")):
        w = w.replace(a, b)
    if not w:
        return ""
    return "~" + re.sub(r"(.)\1+", r"\1", w[0] + re.sub(r"[aeiouy]", "", w[1:]))


def tokens(text: str) -> list[str]:
    global _lemma
    if _lemma is None:
        _lemma = _lemmatizer()
    out = []
    for word in WORD_RE.findall(text.replace("’", "'").replace("ʼ", "'")):
        word = word.lower()
        if word in STOP or len(word) < 2:
            continue
        lemma, known = _lemma(word)
        if lemma in QUESTION_LEMMAS:
            continue
        out.append(lemma)
        latin = word.isascii() and not word.isdigit()
        if (latin or not known) and len(word) >= 3:
            out.append(sound(word))  # термін, назва, абревіатура — знайдеться і в іншому написанні
    return out


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
    def parts(self) -> dict[str, str]:
        return sections(self.summary)

    @property
    def short(self) -> str:
        """Коротка назва для посилань: «Безпека», «Бази даних»."""
        out: list[str] = []
        for word in self.subject.split():  # «Якість і тестування», а не «Якість і»
            out.append(word)
            if sum(len(part) > 2 for part in out) >= 2:
                break
        return " ".join(out)

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
    """Відповідь на повідомлення студента. ``ask(system, text)`` — мовна модель (None — ще не готова);
    ``schedule(now)`` — розклад текстом."""

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

    def quick(self, question: str, now: datetime) -> str | None:
        """Відповідь без мовної моделі — з розкладу й підсумків пар. None — тут потрібна модель."""
        kind = self.route(question)
        if kind == "schedule" and self.schedule is not None:
            return self.schedule(now)
        if kind == "search" and TEACHER_RE.search(question):
            self.library.refresh()
            return self._teachers(question)
        if kind not in ("lesson", "tasks"):
            return None
        self.library.refresh()
        if kind == "tasks":
            return self._tasks(question, now)
        if not EXPLAIN_RE.search(question) and not re.search(r"\bколи\b|що (казав|казала|сказав|говорив)", question, re.I):
            return self._lessons(question, now)
        return None

    def answer(self, question: str, now: datetime, live: str = "", dialog: str = "", events: str = "") -> str:
        if (ready := self.quick(question, now)) is not None:
            return ready
        kind = self.route(question)
        self.library.refresh()
        talk = bool(SMALL_TALK_RE.search(question)) and len(question) < 60  # «Привіт! Як справи?» — шукати нема чого
        found = self.library.search(question, limit=6) if kind != "now" and not talk else []
        strong = [item for item in found if item[0] >= SEARCH_STRONG]
        sources = self.sources(question, kind, now, live) if strong or kind == "now" else ""
        if not sources and ABOUT_LESSONS_RE.search(question) and not talk:
            topic = m.group(1) if (m := re.search(r"\bпро\s+(.+?)[?.!]*$", question, re.I)) else "це"
            return (f"У записах пар про {topic[:60]} нічого не знайшов. Можливо, Whisper записав це інакше або цього "
                    "не було на парах, де я був.")
        if self.ask is None:
            if sources:
                return "Мовна модель ще не готова, ось що знайшов у записах пар:\n" + sources[:1500]
            return "Мовна модель ще не готова (Ollama) — на таке поки не відповім. Розклад і завдання — питай."
        explain = bool(EXPLAIN_RE.search(question))
        parts = [f"Зараз {now:%d.%m.%Y %H:%M}, {WEEKDAY_NAMES[now.weekday()]}."]
        if events:
            parts.append(events)
        if dialog:
            parts.append("Розмова перед цим:\n" + dialog)
        parts.append(f"Питання студента: {question}")
        if sources:
            # Питання — перед уривками, а вимога — після: інакше маленька модель просто переписує уривки.
            parts.append("Уривки з записів пар (автоматична розшифровка, з помилками):\n" + sources)
            parts.append(f"Відповідай на питання «{question}»" + (
                " простими словами; уривки — лише що про це казали на парі." if explain else
                " лише з цих уривків, своїми словами, 2–4 речення."))
        reply = whole_sentences(plain(self.ask(SYSTEM, "\n\n".join(parts))))
        reply = re.sub(r"^(Уривки з записів пар|Знайдено в записах пар)[^:]*:\s*", "", reply)
        if explain:  # пояснення — загальні знання, а записи лише кажуть, де про це говорили
            return "📚 " + reply + (("\n\n" + self._cite(strong, "На парах про це")) if strong else "")
        return reply + (("\n\n" + self._cite(strong)) if strong else "")

    @staticmethod
    def _cite(found: list[tuple[float, Chunk]], title: str = "Джерела") -> str:
        """Звідки відповідь: «Безпека, 09.10 12:55, 12:57» — пишемо самі, а не просимо модель."""
        places: dict[str, list[str]] = {}
        for _, chunk in found[:4]:
            places.setdefault(chunk.doc.label(), []).append(chunk.time)
        return f"{title}: " + "; ".join(f"{label} {', '.join(sorted(set(times)))}" for label, times in places.items())

    def _teachers(self, question: str) -> str | None:
        """«Хто викладає бази даних?» — з записів пар (у заголовку кожного — викладач з розкладу)."""
        named = self.library.subjects_named(question)
        if not named:
            return None
        by_kind: dict[str, str] = {}
        for doc in self.library.docs:
            if doc.subject in named and doc.teacher:
                by_kind[doc.kind or "пара"] = doc.teacher
        if not by_kind:
            return None
        return f"👤 {named[0]}:\n" + "\n".join(f"• {kind}: {teacher}" for kind, teacher in by_kind.items())

    def _lessons(self, question: str, now: datetime) -> str:
        """«Що було на останній парі / вчора / на парі з баз даних» — з підсумків пар."""
        picked = self._pick_docs(question, now)
        day = _day_from(question, now.date())
        if day is None and self.library.subjects_named(question):
            picked = picked[-1:]  # «що було на парі з баз даних» — остання така пара
        if not picked:
            if day is not None:
                return f"За {day:%d.%m} ({WEEKDAY_NAMES[day.weekday()]}) записів пар немає — програма тоді не була на парах або нічого не чула."
            if self.library.subjects_named(question):
                return "Записів цієї пари ще немає."
            picked = self.library.docs[-1:]
            if not picked:
                return "Записів пар ще немає — вони з'являться після першої пари."
        many = len(picked) > 1
        out = []
        for doc in picked[-4:]:
            parts = doc.parts
            head = f"📖 {doc.title}, {doc.day:%d.%m} {doc.start}–{doc.end}" + (f", {doc.teacher}" if doc.teacher else "")
            if not parts:
                start = " ".join(text for _, text in doc.lines[:8])[:400]
                out.append(f"{head}\nПідсумку немає. Початок розшифровки: {start}" if start else f"{head}\nПідсумку немає.")
                continue
            names = ("Про що", "Завдання й дедлайни") if many else ("Про що", "Що робили", "Завдання й дедлайни", "Важливе")
            lines = [f"{name}: {parts[name]}" for name in names if parts.get(name) and not NOTHING_RE.match(parts[name])]
            out.append("\n".join([head, *lines]))
        return "\n\n".join(out) + "\n\n(підсумки складає мовна модель з автоматичної розшифровки — можливі неточності)"

    def _tasks(self, question: str, now: datetime) -> str:
        """«Що задали з …», «які дедлайни», «які лаби захистити» — розділ «Завдання й дедлайни» підсумків пар."""
        picked = self._pick_docs(question, now)
        named = self.library.subjects_named(question)
        if not picked and not named and _day_from(question, now.date()) is None:
            since = now.date() - timedelta(days=14)
            picked = [doc for doc in self.library.docs if doc.day >= since]
        items = []
        for doc in picked:
            task = doc.parts.get("Завдання й дедлайни", "")
            if task and not NOTHING_RE.match(task):
                items.append(f"• {doc.short} ({doc.kind or doc.title}), {doc.day:%d.%m}: {_shorten(task, 300)}")
        if not items:
            where = f" з «{named[0]}»" if named else ""
            return f"📝 У записах пар{where} завдань і дедлайнів не знайшов." + (
                "" if picked else " (записів за цей час немає)")
        return "📝 Завдання з записів пар:\n" + "\n".join(items[-8:]) + (
            "\n\n(з автоматичних підсумків — дати й номери краще звір у Classroom)")

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
        query = question + " завдання лабораторна здати дедлайн до" if kind == "tasks" else question
        for _, chunk in library.search(query, limit=6, docs=scope):
            out.append(f"[{chunk.doc.label(chunk.time)}] {chunk.text}")
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
        if named and day is None:
            return docs[-3:]
        if named or day is not None:
            return docs[-5:]
        return []
