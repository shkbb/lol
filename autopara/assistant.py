"""Помічник на парі — «мозок» autopara.

Влаштований як нервова система:
- очі — чат і кількість учасників зустрічі (joiner.py);
- вуха — розпізнана мова пари (ears.py);
- рефлекси — швидкі правила без ШІ (тут): група масово пише «+» → пишемо «+»;
  тебе назвали — одразу сповіщення;
- головний мозок — локальна мовна модель (brain.py): розбирається в ситуаціях,
  які ми не передбачили, і обирає одну з дозволених дій;
- руки — написати в чат, надіслати тобі сповіщення, вийти з пари.

Руки навмисно короткі. Помічник ніколи не вмикає мікрофон чи камеру, а сам пише
в чат лише «+», твоє ім'я або коротку фразу присутності/подяки, яку вже написала
група. Решта — сповіщення тобі в Telegram, і вирішуєш ти: можна відповісти боту,
і він напише в чат твій текст.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from .config import MeSettings

log = logging.getLogger(__name__)

BURST_WINDOW = timedelta(minutes=3)
BURST_MIN_AUTHORS = 3
CHAT_COOLDOWN = timedelta(minutes=2)
MAX_CHAT_PER_MEETING = 5
ASK_TIMEOUT = timedelta(minutes=5)
MENTION_COOLDOWN = timedelta(minutes=1)
ROLL_CALL_COOLDOWN = timedelta(minutes=3)

# Що помічник може сам продублювати за групою: присутність, привітання, подяка — і що він
# тоді пише (None — твоя фраза присутності з [me] present). Відповіді на питання
# (варіанти «а», «1» тощо) — ніколи: це вирішуєш ти.
ECHO_GROUPS = [
    (re.compile(r"^\+$"), "+"),
    (re.compile(r"^(присутн\w*|тут|я тут|є|here|present)$"), None),
    (re.compile(r"^(дякую|дякуємо|спасибі)( за пару| вам)?$"), "Дякую!"),
    (re.compile(r"^до побачення$"), "До побачення!"),
    (re.compile(r"^(добр\w+ (день|ранок|вечір|дня)|вітаю|здрастуйте)$"), "Добрий день!"),
]
# Що в мові викладача схоже на перекличку.
ROLL_CALL_RE = re.compile(r"плюс|перекличк|відміч|відміт|присутн|хто (є|тут|зараз)", re.I)
# Масовий «+» буває не лише на перекличці: ним записуються на проєкти, голосують, кажуть «зрозуміло».
# Тож перш ніж ставити «+», дивимось, що саме просили.
ATTENDANCE_RE = re.compile(r"перекличк|відміт|відміч|присутн|хто (є|тут|зараз)|хто на пар|чи всі (є|тут)|attendance", re.I)
NOT_ATTENDANCE_RE = re.compile(
    r"хто (хоче|бажає|готов|може|зможе|згод|за\b|проти|візьм|піде|поїде|братиме)|бажаюч|охоч|добровол|запиш|"
    r"запис|візьм|додатков|проєкт|проект|доповід|реферат|презентац|конференц|олімпіад|конкурс|хакатон|участь|"
    r"голосу|згодні|хочете|зрозуміл|якщо (так|згодні|ок)",
    re.I,
)
PLUS_REQUEST_RE = re.compile(r"плюс|\+", re.I)
NAME_REQUEST_RE = re.compile(r"прізвищ|ім'я|імена|піб", re.I)
BRAIN_ANSWER_TIMEOUT = timedelta(seconds=90)
CHUNK_CHARS = 2500  # скільки розшифровки віддавати мозку на конспект за раз
# Скільки чекаємо на твоє «ні», перш ніж діяти самостійно, коли сигнали за, але не всі.
VETO_WINDOW = timedelta(seconds=45)
# Як довго мозкове «схоже, перекличка» чекає на підтвердження від групи в чаті.
EXPECT_WINDOW = timedelta(minutes=5)
# Перекличка голосом: стільки різних однокурсників мають прозвучати за ці хвилини до тебе.
VOICE_ROLL_CALL_NAMES = 3
VOICE_ROLL_CALL_WINDOW = timedelta(minutes=4)
# Що варто показати «мозку»: прохання до всіх щось зробити.
ATTENTION_RE = re.compile(
    r"плюс|перекличк|відміч|відміт|присутн|хто (є|тут|зараз)|напишіть|поставте|відпишіть|"
    r"увімкніть|ввімкніть|включіть|камер|здайте|надішліть|дедлайн|контрольн",
    re.I,
)
ME_AUTHOR_RE = re.compile(r"^(you|ви|вы)$", re.I)

HELP = (
    "Я пишу, коли щось відбувається на парі. Що можна відповісти:\n"
    "статус — що зараз на парі\n"
    "чат — показати чат зустрічі\n"
    "екран — скріншот зустрічі\n"
    "так / ні — на моє питання «написати в чат?»\n"
    "+ — написати «+» у чат\n"
    "напиши <текст> — написати твій текст у чат\n"
    "↩️ відповідь на будь-яке моє повідомлення — теж піде в чат пари\n"
    "вийди — вийти з пари"
)
COMMANDS = {
    "/start", "/help", "help", "допомога", "?", "/status", "статус", "так", "yes", "ок", "ok", "давай", "+",
    "ні", "no", "не треба", "не пиши", "вийди", "/leave", "чат", "/chat", "екран", "скрін", "скріншот", "/screen",
}


@dataclass(frozen=True)
class ChatMessage:
    id: str
    author: str
    text: str


@dataclass(frozen=True)
class Action:
    kind: str  # "chat" | "leave"
    key: str
    text: str = ""
    reason: str = ""


@dataclass(frozen=True)
class Decision:
    """Рішення «мозку». ``action``: none | notify | chat_plus | chat_name | leave."""

    action: str
    message: str = ""
    reason: str = ""


@dataclass
class LessonRecord:
    """Усе про одну пару — для звіту після неї."""

    report_id: str
    title: str
    teacher: str
    start: datetime
    end: datetime
    joined: datetime
    left: datetime
    transcript: list[tuple[datetime, str]]
    chat: list[tuple[datetime, ChatMessage]]
    mentions: list[tuple[datetime, str]]
    sent: list[tuple[datetime, str]]
    peak: int
    rest: str  # хвіст розшифровки, який мозок ще не конспектував


@dataclass
class Meeting:
    key: str
    title: str
    teacher: str
    start: datetime
    end: datetime
    joined: datetime | None = None
    report_id: str = ""
    lesson_number: int = 0
    chunk: list[str] = field(default_factory=list)  # розшифровка, ще не віддана на конспект
    chat: deque = field(default_factory=lambda: deque(maxlen=300))  # (час, ChatMessage)
    seen: set[str] = field(default_factory=set)
    chat_primed: bool = False
    history: list[ChatMessage] = field(default_factory=list)  # чат до нашого приходу — лише для показу
    heard: deque = field(default_factory=lambda: deque(maxlen=80))  # (час, текст, чи впевнено) — свіже
    transcript: list[tuple[datetime, str]] = field(default_factory=list)  # уся пара — для звіту й цитат
    mentions: list[tuple[datetime, str]] = field(default_factory=list)
    peak: int = 0
    participants: int | None = None
    sent: list[tuple[datetime, str]] = field(default_factory=list)
    reacted: set[str] = field(default_factory=set)  # на що вже відреагували, щоб не повторюватись
    pending: Pending | None = None  # чекаємо на твоє «так»/«ні»
    awaiting: Awaiting | None = None  # хвиля «+», щодо якої питаємо мозок
    expect_roll_call: datetime | None = None  # мозок чув перекличку — чекаємо, чи відмічатимуться інші


@dataclass
class Pending:
    text: str
    until: datetime
    reason: str
    act_on_timeout: bool  # True — «напишу, якщо не скажеш ні»; False — «не напишу, якщо не скажеш так»


@dataclass
class Awaiting:
    reply: str
    mark: str
    what: str
    context: str
    since: datetime


def _norm(text: str) -> str:
    text = " ".join(text.casefold().replace("’", "'").split()).strip(" .!)")
    return "+" if re.fullmatch(r"\++", text) else text


def _snippet(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else "…" + text[-limit:]


def name_patterns(name: str) -> list[tuple[re.Pattern, bool]]:
    """Шаблони для «Прізвище Ім'я» з урахуванням відмінків: Ляшко → Ляшка, Ляшку; Денис → Денисе.

    Повертає (шаблон, чи це прізвище).
    """
    patterns = []
    for index, word in enumerate(name.split()[:2]):
        word = word.casefold()
        stem = word[:-1] if len(word) >= 4 and word[-1] in "аоуеиіїяюєй" else word
        patterns.append((re.compile(rf"(?<!\w){re.escape(stem)}\w*", re.I), index == 0))
    return patterns


class Assistant:
    def __init__(
        self,
        me: MeSettings,
        notify: Callable[..., None],
        wake_brain: Callable[[str], None] | None = None,
        media=None,
    ):
        self.me = me
        self.notify = notify  # notify(text, urgent=False)
        self.media = media  # Notifier з photo/video/voice/document, або None
        self.on_moment: Callable[[str, str, datetime], None] | None = None  # (ключ, заголовок, коли) → вирізка
        self.on_chunk: Callable[[str, str], None] | None = None  # (звіт, шматок розшифровки) → конспект
        self.on_finished: Callable[[LessonRecord], None] | None = None  # пара скінчилась → звіт
        self.wake_brain = wake_brain  # None — «мозок» вимкнено, працюють лише рефлекси
        self.meetings: dict[str, Meeting] = {}
        self._order: list[str] = []
        self._actions: list[Action] = []
        self._lock = threading.RLock()
        self._patterns = name_patterns(me.name)
        self._classmates: list[re.Pattern] = []  # прізвища однокурсників — для переклички голосом
        self.ears_on = False  # чи справді слухаємо пару (від цього залежить, чи можна діяти без питань)
        self._last_mention: datetime | None = None
        self._last_roll_call: datetime | None = None

    # --- Життєвий цикл зустрічі ------------------------------------------------

    def attach(
        self, key: str, title: str, teacher: str, start: datetime, end: datetime, now: datetime | None = None
    ) -> None:
        with self._lock:
            meeting = Meeting(key, title, teacher, start, end, joined=now or start, report_id=f"{key}#1")
            self.meetings[key] = meeting
            self._order.append(key)

    def next_lesson(self, key: str, title: str, start: datetime, end: datetime, now: datetime | None = None) -> None:
        """Наступна пара в тій самій зустрічі: звіт про попередню і чистий аркуш."""
        with self._lock:
            meeting = self.meetings.get(key)
            if meeting is None:
                return
            self._finish(meeting, now or start)
            number = meeting.lesson_number + 1
            fresh = Meeting(key, title, meeting.teacher, start, end, joined=now or start, report_id=f"{key}#{number + 1}")
            fresh.lesson_number = number
            fresh.seen, fresh.chat_primed = meeting.seen, True
            self.meetings[key] = fresh

    def detach(self, key: str, now: datetime | None = None) -> None:
        with self._lock:
            meeting = self.meetings.pop(key, None)
            if key in self._order:
                self._order.remove(key)
            if meeting is not None:
                self._finish(meeting, now or datetime.now(meeting.start.tzinfo))

    def _finish(self, meeting: Meeting, now: datetime) -> None:
        if self.on_finished is None:
            return
        self.on_finished(
            LessonRecord(
                report_id=meeting.report_id,
                title=meeting.title,
                teacher=meeting.teacher,
                start=meeting.start,
                end=meeting.end,
                joined=meeting.joined or meeting.start,
                left=now,
                transcript=list(meeting.transcript),
                chat=list(meeting.chat),
                mentions=list(meeting.mentions),
                sent=list(meeting.sent),
                peak=meeting.peak,
                rest=" ".join(meeting.chunk),
            )
        )

    def set_classmates(self, names: list[str]) -> None:
        """Прізвища однокурсників (з сайту розкладу): по них видно, що викладач читає список групи."""
        mine = name_patterns(self.me.name)[0][0].pattern if self.me.name else None
        patterns = [name_patterns(name)[0][0] for name in names if name.split()]
        with self._lock:
            self._classmates = [pattern for pattern in patterns if pattern.pattern != mine]

    def current(self) -> Meeting | None:
        with self._lock:
            return self.meetings[self._order[-1]] if self._order else None

    # --- Очі й вуха --------------------------------------------------------------

    def on_participants(self, key: str, count: int | None) -> None:
        with self._lock:
            if meeting := self.meetings.get(key):
                meeting.participants = count
                meeting.peak = max(meeting.peak, count or 0)

    def on_chat(self, key: str, messages: list[ChatMessage], now: datetime) -> None:
        with self._lock:
            meeting = self.meetings.get(key)
            if meeting is None:
                return
            new = [message for message in messages if message.id not in meeting.seen]
            meeting.seen.update(message.id for message in new)
            if not meeting.chat_primed:
                # Перше читання — це історія чату до нашого приходу: на неї не реагуємо.
                meeting.chat_primed = True
                meeting.history = new[-50:]
                return
            for message in new:
                meeting.chat.append((now, message))
            others = [message for message in new if not self._is_me(message.author)]
            for message in others:
                self._check_mention(meeting, message.text, now, f"у чаті ({message.author or 'хтось'})")
            if self.wake_brain and any(ATTENTION_RE.search(message.text) for message in others):
                self.wake_brain(key)
            if others:
                self._chat_reflexes(meeting, now)

    def on_heard(self, text: str, now: datetime, confident: bool = True) -> None:
        """Розпізнана фраза з пари (викликається з потоку «вух»).

        Мова сама ніколи не запускає дію в зустрічі: вона може лише сповістити тебе, розбудити мозок
        чи зробити помічника обережнішим. Невпевнено розпізнане (``confident=False``) позначаємо «[?]».
        """
        text = text.strip()
        with self._lock:
            meeting = self.current()
            if meeting is None or not text:
                return
            meeting.heard.append((now, text, confident))
            meeting.transcript.append((now, text if confident else f"[?] {text}"))
            meeting.chunk.append(text)
            if self.on_chunk and sum(len(part) for part in meeting.chunk) >= CHUNK_CHARS:
                self.on_chunk(meeting.report_id, " ".join(meeting.chunk))
                meeting.chunk = []
            mentioned = self._check_mention(meeting, text, now, "на парі")
            if mentioned:
                self._voice_roll_call(meeting, now)
            if not mentioned and ATTENTION_RE.search(text):
                if self.wake_brain:
                    self.wake_brain(meeting.key)
                elif ROLL_CALL_RE.search(text) and self._cooled(self._last_roll_call, now, ROLL_CALL_COOLDOWN):
                    self._last_roll_call = now
                    self.notify(f"📋 Схоже, на «{meeting.title}» перекличка: «{_snippet(text)}»", urgent=True)

    # --- Рефлекси ----------------------------------------------------------------

    def _is_me(self, author: str) -> bool:
        if ME_AUTHOR_RE.match(author.strip()):
            return True
        surname = next((pattern for pattern, is_surname in self._patterns if is_surname), None)
        return bool(surname and surname.search(author))

    @staticmethod
    def _cooled(last: datetime | None, now: datetime, cooldown: timedelta) -> bool:
        return last is None or now - last >= cooldown

    def _check_mention(self, meeting: Meeting, text: str, now: datetime, where: str) -> bool:
        hits = [is_surname for pattern, is_surname in self._patterns if pattern.search(text)]
        if not hits:
            return False
        meeting.mentions.append((now, text))
        if self._cooled(self._last_mention, now, MENTION_COOLDOWN):
            self._last_mention = now
            lead = "🚨 Тебе кличуть" if any(hits) else "❗ Можливо, звертаються до тебе"
            self.notify(f"{lead} {where} на «{meeting.title}»: «{_snippet(text)}»", urgent=True)
            if self.wake_brain:
                self.wake_brain(meeting.key)
            if self.on_moment:
                self.on_moment(meeting.key, f"🎬 Момент з «{meeting.title}», {now:%H:%M}", now)
        return True

    def _voice_roll_call(self, meeting: Meeting, now: datetime) -> None:
        """Викладач читає список групи вголос і дійшов до тебе — відмічаємось у чаті.

        Одне розпізнане «Ляшко» нічого не доводить, а от кілька різних прізвищ однокурсників поспіль
        перед ним — так звучить лише перекличка. Відповісти голосом ти не можеш, тож пишемо в чат.
        """
        if not self._classmates or "__voice__" in meeting.reacted:
            return
        recent = " ".join(text for t, text, _ in meeting.heard if now - t <= VOICE_ROLL_CALL_WINDOW)
        named = sum(1 for pattern in self._classmates if pattern.search(recent))
        if named >= VOICE_ROLL_CALL_NAMES:
            self.propose_chat(
                meeting, self.me.present, f"Викладач читає список групи ({named} прізвищ) і назвав тебе",
                now, mark="__voice__",
            )

    def _speech_clean(self, meeting: Meeting, since: datetime, now: datetime) -> bool:
        """Вуха працюють і все, що прозвучало навколо хвилі, розпізнано впевнено.

        Тоді мовчання про «запис», «проєкт», «голосування» справді означає, що їх не казали.
        """
        if not self.ears_on or not any(now - t <= timedelta(minutes=10) for t, _, _ in meeting.heard):
            return False  # не чуємо пару — не знаємо, що там казали
        return all(sure for t, _, sure in meeting.heard if t >= since - timedelta(minutes=3))

    def whisper_hints(self) -> str:
        """Імена й назва пари — підказка для розпізнавання мови, щоб не перекручувало прізвища."""
        with self._lock:
            meeting = self.current()
            parts = [self.me.name]
            if meeting is not None:
                parts += [meeting.teacher, re.sub(r"\s*\(.*?\)", "", meeting.title)]
        return ", ".join(part for part in parts if part)

    def quote(self, key: str, start: datetime, end: datetime, limit: int = 1500) -> str:
        """Що казали й писали між start і end — детальна цитата для вирізки."""
        with self._lock:
            meeting = self.meetings.get(key)
            if meeting is None:
                return ""
            lines = [(t, text) for t, text in meeting.transcript if start <= t <= end]
            lines += [(t, f"💬 {m.author or '?'}: {m.text}") for t, m in meeting.chat if start <= t <= end]
        text = "\n".join(line for _, line in sorted(lines, key=lambda item: item[0]))
        return text if len(text) <= limit else "…" + text[-limit:]

    def _chat_reflexes(self, meeting: Meeting, now: datetime) -> None:
        recent = [(t, m) for t, m in meeting.chat if now - t <= BURST_WINDOW and not self._is_me(m.author)]
        # 1. Кілька людей пишуть одне й те саме («+», «присутній», «дякую»).
        authors: dict[int, set[str]] = {}
        sample: dict[int, str] = {}
        first: dict[int, datetime] = {}
        ids: dict[int, set[str]] = {}
        for t, message in recent:
            text = _norm(message.text)
            for group, (pattern, _) in enumerate(ECHO_GROUPS):
                if pattern.match(text):
                    authors.setdefault(group, set()).add(message.author or message.id)
                    sample.setdefault(group, message.text.strip())
                    first.setdefault(group, t)
                    ids.setdefault(group, set()).add(message.id)
                    break
        for group, who in sorted(authors.items(), key=lambda item: -len(item[1])):
            mark = f"echo:{group}"
            expected = meeting.expect_roll_call is not None and now - meeting.expect_roll_call <= EXPECT_WINDOW
            needed = BURST_MIN_AUTHORS - 1 if expected and group == 0 else BURST_MIN_AUTHORS
            if len(who) < needed or mark in meeting.reacted:
                continue
            reply = ECHO_GROUPS[group][1] or self.me.present
            if group == 0:
                # «+» може означати що завгодно — дивимось, на що його ставлять.
                self._consider(meeting, reply, mark, "Група ставить «+» у чат", first[group], ids[group], PLUS_REQUEST_RE, now)
            else:
                # «Присутній», «дякую», «добрий день» ні на що не підписують.
                self.propose_chat(meeting, reply, f"Група пише «{sample[group]}» у чат", now, mark=mark)
            return
        # 2. Люди пишуть у чат свої прізвища — теж дивимось навіщо.
        by_name = [
            (t, m)
            for t, m in recent
            if m.author
            and len(m.text) <= 60
            and any(word[:-1] in m.text.casefold() for word in m.author.casefold().split() if len(word) > 3)
        ]
        if len({m.author for _, m in by_name}) >= BURST_MIN_AUTHORS and self.me.name and "__name__" not in meeting.reacted:
            since = min(t for t, _ in by_name)
            self._consider(
                meeting, self.me.name, "__name__", "Група пише в чат прізвища", since, {m.id for _, m in by_name},
                NAME_REQUEST_RE, now,
            )

    def _intent(self, meeting: Meeting, since: datetime, wave: set[str], request: re.Pattern) -> tuple[str, str]:
        """Навіщо група це пише: ("attendance" | "not_attendance" | "unknown", цитата-контекст).

        Дивимось на речення саме про «плюс» (чи прізвища): у мові викладача за хвилину до хвилі
        й у чаті за дві. Розпізнана мова може помилятися, тож для неї правила суворіші:
        - перекличку мова підтверджує лише тим самим реченням і лише якщо розпізнала його впевнено;
        - сусіднє чи невпевнене речення може тільки заборонити («хто хоче проєкт? ставте плюс»),
          але не дозволити.
        Текст чату точний, тож там на «навіщо» можна дивитись і в попередньому повідомленні.
        """
        speech = [
            (sentence.strip(), confident)
            for t, text, confident in meeting.heard
            if t >= since - timedelta(minutes=1)
            for sentence in re.split(r"(?<=[.!?])\s+", text)
            if sentence.strip()
        ]
        chat = [f"{m.author}: {m.text}" for t, m in meeting.chat if t >= since - timedelta(minutes=2) and m.id not in wave]

        def verdict(sentence: str) -> str | None:
            if NOT_ATTENDANCE_RE.search(sentence):
                return "not_attendance"
            if ATTENDANCE_RE.search(sentence):
                return "attendance"
            return None

        verdicts, quotes = [], []
        for index, (sentence, confident) in enumerate(speech):
            if not request.search(sentence):
                continue
            own = verdict(sentence)
            previous = speech[index - 1][0] if index > 0 else ""
            if own is None and verdict(previous) == "not_attendance":
                own, sentence = "not_attendance", f"{previous} {sentence}"
            elif own == "attendance" and not confident:
                own = None  # почулось — не підстава писати в чат
            verdicts.append(own)
            quotes.append(f"🎙 {sentence}")
        for index, line in enumerate(chat):
            if request.search(line):
                found = verdict(line)
                if found is None and index > 0:
                    found, line = verdict(chat[index - 1]), f"{chat[index - 1]} … {line}"
                verdicts.append(found)
                quotes.append(f"💬 {line}")
        if not quotes:
            # Про плюс прямо ніхто не казав: чат може і дозволити, і заборонити, мова — лише заборонити.
            for sentence, _ in speech:
                if verdict(sentence) == "not_attendance":
                    verdicts.append("not_attendance")
                    quotes.append(f"🎙 {sentence}")
            for line in chat:
                verdicts.append(verdict(line))
                quotes.append(f"💬 {line}")
        context = _snippet(" … ".join(quotes[-3:]), 300)
        if "not_attendance" in verdicts:
            return "not_attendance", context
        if "attendance" in verdicts:
            return "attendance", context
        return "unknown", context

    def _consider(
        self, meeting: Meeting, reply: str, mark: str, what: str, since: datetime, wave: set[str],
        request: re.Pattern, now: datetime,
    ) -> None:
        """Писати чи ні — за збігом незалежних сигналів.

        - хтось прямо назвав перекличку (чат або чітко почуте речення) → пишемо одразу;
        - про запис, голосування, «зрозуміло?» → лише питаємо, без відповіді не пишемо;
        - причину не названо, але мозок раніше чув перекличку → пишемо одразу (мозок + група);
        - причину не названо → питаємо мозок; без мозку, якщо вуха чітко чули останні хвилини
          й ніхто не казав про запис, → «напишу за 45 с, якщо не скажеш ні»; інакше — лише питаємо.
        """
        if mark in meeting.reacted:
            return
        intent, context = self._intent(meeting, since, wave, request)
        meeting.reacted.add(mark)
        expected = meeting.expect_roll_call is not None and now - meeting.expect_roll_call <= EXPECT_WINDOW
        if intent == "attendance":
            self._offer(meeting, reply, f"{what} — це перекличка", now, context=context)
        elif intent == "not_attendance":
            hint = "Схоже, це не перекличка, а запис, голосування чи «зрозуміло?»"
            self._offer(meeting, reply, f"{what}. {hint} — сам не пишу", now, ask=True, context=context)
        elif expected:
            self._offer(meeting, reply, f"{what}, а мозок перед цим чув перекличку", now, context=context)
        elif self.wake_brain:
            meeting.awaiting = Awaiting(reply, mark, what, context, now)
            self.wake_brain(meeting.key)
        else:
            self._unclear(meeting, reply, what, since, now, context)

    def _unclear(self, meeting: Meeting, reply: str, what: str, since: datetime, now: datetime, context: str) -> None:
        if self._speech_clean(meeting, since, now):
            self._offer(
                meeting, reply, f"{what}; про запис чи голосування ніхто не казав", now, context=context,
                ask=True, act_on_timeout=True,
            )
        else:
            self._offer(meeting, reply, f"{what}. Не зрозумів, навіщо — сам не пишу", now, ask=True, context=context)

    # --- Руки --------------------------------------------------------------------

    def _can_chat(self, meeting: Meeting, now: datetime) -> bool:
        if len(meeting.sent) >= MAX_CHAT_PER_MEETING:
            return False
        return not meeting.sent or now - meeting.sent[-1][0] >= CHAT_COOLDOWN

    def propose_chat(self, meeting: Meeting, text: str, reason: str, now: datetime, mark: str) -> None:
        if mark in meeting.reacted:
            return
        meeting.reacted.add(mark)
        self._offer(meeting, text, reason, now)

    def _offer(
        self, meeting: Meeting, text: str, reason: str, now: datetime, ask: bool = False, context: str = "",
        act_on_timeout: bool = False,
    ) -> None:
        """Написати в чат сам, спитати тебе чи лише сповістити — за [me] chat_reply і лімітами.

        ``act_on_timeout`` — «напишу, якщо за 45 с не скажеш ні»: для випадків, коли сигнали за,
        але не всі. У режимі chat_reply = "ask" без твого «так» не пишемо ніколи.
        """
        quote = f"\nКонтекст: «{context}»" if context else ""
        policy = self.me.chat_reply
        if policy == "off":
            self.notify(f"{reason} на «{meeting.title}».{quote}\nСам не пишу (chat_reply = \"off\").", urgent=True)
        elif not self._can_chat(meeting, now):
            self.notify(f"{reason} на «{meeting.title}», але я вже писав у чат нещодавно — пропускаю.{quote}", urgent=True)
        elif act_on_timeout and policy == "auto":
            meeting.pending = Pending(text, now + VETO_WINDOW, reason, act_on_timeout=True)
            seconds = int(VETO_WINDOW.total_seconds())
            self.notify(
                f"{reason} на «{meeting.title}».{quote}\nНапишу «{text}» за {seconds} с, якщо не відповіси «ні» "
                "(«так» — одразу).",
                urgent=True,
            )
        elif ask or policy == "ask":
            meeting.pending = Pending(text, now + ASK_TIMEOUT, reason, act_on_timeout=False)
            self.notify(f"{reason} на «{meeting.title}».{quote}\nНаписати «{text}»? Відповідай: так / ні", urgent=True)
        else:
            because = f"{reason}: «{context}»" if context else reason
            self._actions.append(Action("chat", meeting.key, text, because))

    def chat_done(self, key: str, text: str, ok: bool, now: datetime, reason: str = "") -> None:
        with self._lock:
            meeting = self.meetings.get(key)
            title = meeting.title if meeting else "пару"
            if ok:
                if meeting:
                    meeting.sent.append((now, text))
                suffix = f" ({reason})" if reason else ""
                self.notify(f"💬 Написав «{text}» у чат «{title}»{suffix}")
            else:
                self.notify(f"⚠️ Не вдалося написати «{text}» у чат «{title}» — може, чат вимкнений?", urgent=True)

    def take_actions(self) -> list[Action]:
        with self._lock:
            actions, self._actions = self._actions, []
            return actions

    def tick(self, now: datetime) -> None:
        with self._lock:
            for meeting in self.meetings.values():
                waiting = meeting.awaiting
                if waiting and now - waiting.since > BRAIN_ANSWER_TIMEOUT:
                    meeting.awaiting = None
                    self._unclear(meeting, waiting.reply, f"{waiting.what} (мозок не встиг)", waiting.since, now, waiting.context)
                pending = meeting.pending
                if pending and now > pending.until:
                    meeting.pending = None
                    if pending.act_on_timeout:
                        self._actions.append(Action("chat", meeting.key, pending.text, f"{pending.reason}; ти не заперечив"))
                    else:
                        self.notify(f"Не дочекався відповіді — у чат «{meeting.title}» не пишу.")

    # --- Мозок -------------------------------------------------------------------

    def snapshot(self, key: str, now: datetime) -> str | None:
        """Опис ситуації для мовної моделі."""
        with self._lock:
            meeting = self.meetings.get(key)
            if meeting is None:
                return None
            lines = [
                f"Пара: «{meeting.title}», викладач: {meeting.teacher or 'невідомо'}.",
                f"Пара з {meeting.start:%H:%M} до {meeting.end:%H:%M}, зараз {now:%H:%M}.",
                f"Учасників у зустрічі: {meeting.participants if meeting.participants else 'невідомо'}.",
                f"Студент: {self.me.name or 'невідомо'}. Його немає біля комп'ютера.",
            ]
            if meeting.sent:
                done = ", ".join(f"«{text}» о {t:%H:%M}" for t, text in meeting.sent)
                lines.append(f"Вже написано в чат: {done}.")
            heard = [(t, text, sure) for t, text, sure in meeting.heard if now - t <= timedelta(minutes=3)]
            if heard:
                lines.append(
                    "\nЩо сказали на парі за останні хвилини (автоматичне розпізнавання з помилками; "
                    "«[?]» — розпізнано невпевнено, на такому не ґрунтуй висновків):"
                )
                lines += [f"[{t:%H:%M}] {'' if sure else '[?] '}{text}" for t, text, sure in heard][-20:]
            chat = list(meeting.chat)[-15:]
            if chat:
                lines.append("\nОстанні повідомлення в чаті:")
                lines += [f"[{t:%H:%M}] {m.author or '?'}: {m.text}" for t, m in chat]
            if meeting.awaiting:
                lines.append(
                    f"\nПИТАННЯ: {meeting.awaiting.what.lower()}. Якщо викладач робить перекличку — обери "
                    f"{'chat_name' if meeting.awaiting.mark == '__name__' else 'chat_plus'}. Якщо це запис на щось, "
                    "голосування, згода чи «зрозуміло?» — обери notify і поясни, на що саме це пишуть."
                )
            return "\n".join(lines)

    def apply_decision(self, key: str, decision: Decision, now: datetime) -> None:
        with self._lock:
            meeting = self.meetings.get(key)
            if meeting is None or decision.action == "none":
                return
            why = decision.reason or decision.message
            log.info("Мозок: %s — %s", decision.action, why)
            waiting = meeting.awaiting
            if waiting is not None:
                # Мозок відповідав на питання про хвилю «+»/прізвищ.
                meeting.awaiting = None
                if decision.action in ("chat_plus", "chat_name"):
                    self._offer(meeting, waiting.reply, f"{waiting.what} — 🧠 {why}", now, context=waiting.context)
                elif decision.action == "notify":
                    # Мозок пояснив, що це щось інше (запис, голосування) — без тебе не пишемо.
                    self._offer(
                        meeting, waiting.reply, f"{waiting.what}. 🧠 {decision.message or why} — сам не пишу", now,
                        ask=True, context=waiting.context,
                    )
                else:
                    self._unclear(meeting, waiting.reply, waiting.what, waiting.since, now, waiting.context)
                return
            if decision.action == "notify":
                self.notify(f"🧠 {decision.message or why} («{meeting.title}»)", urgent=True)
            elif decision.action in ("chat_plus", "chat_name"):
                # Мозок спирається лише на розпізнану мову, а вона може помилитись («плюс» з лекції про C++).
                # Тож чекаємо підтвердження від групи: щойно двоє однокурсників відмітяться — відмічаємось теж.
                if meeting.expect_roll_call is None or now - meeting.expect_roll_call > EXPECT_WINDOW:
                    meeting.expect_roll_call = now
                    self.notify(
                        f"🧠 Схоже, на «{meeting.title}» перекличка ({why}). Відмічусь, щойно в чаті відмітяться інші."
                    )
            elif decision.action == "leave":
                # Виходить помічник лише за учасниками й розкладом; «на сьогодні все» з розшифровки — лише порада.
                self.notify(f"🧠 Схоже, пару «{meeting.title}» закінчили ({why}). Напиши «вийди», щоб вийти зараз.")

    # --- Команди з Telegram ------------------------------------------------------

    def on_command(self, text: str, now: datetime, is_reply: bool = False) -> str:
        command = text.strip()
        low = command.casefold()
        with self._lock:
            meeting = self.current()
            if low in ("/start", "/help", "help", "допомога", "?"):
                return HELP
            if low in ("/status", "статус"):
                return self._status(meeting, now)
            if meeting is None:
                return "Зараз я не на парі."
            if low in ("чат", "/chat"):
                return self._chat_view(meeting)
            if low in ("екран", "скрін", "скріншот", "/screen"):
                self._actions.append(Action("screenshot", meeting.key))
                return "Роблю скріншот..."
            if is_reply and low not in COMMANDS and not low.startswith(("напиши ", "/say ")):
                # Відповідь на будь-яке моє повідомлення — це текст для чату пари.
                reply = command[:500]
                self._actions.append(Action("chat", meeting.key, reply, "ти відповів у Telegram"))
                return f"Пишу «{reply}»."
            if low in ("так", "yes", "ок", "ok", "давай", "+") and meeting.pending:
                pending, meeting.pending = meeting.pending, None
                self._actions.append(Action("chat", meeting.key, pending.text, f"{pending.reason}; ти підтвердив"))
                return f"Пишу «{pending.text}»."
            if low in ("ні", "no", "не треба", "не пиши"):
                meeting.pending = None
                return "Добре, не пишу."
            if low == "+":
                self._actions.append(Action("chat", meeting.key, "+", "ти попросив"))
                return "Пишу «+»."
            for prefix in ("напиши ", "/say "):
                if low.startswith(prefix) and command[len(prefix) :].strip():
                    reply = command[len(prefix) :].strip()[:500]
                    self._actions.append(Action("chat", meeting.key, reply, "ти попросив"))
                    return f"Пишу «{reply}»."
            if low in ("вийди", "/leave"):
                self._actions.append(Action("leave", meeting.key, reason="ти попросив у Telegram"))
                return "Виходжу."
            return "Не зрозумів.\n\n" + HELP

    def _chat_view(self, meeting: Meeting) -> str:
        messages = [(None, m) for m in meeting.history] + list(meeting.chat)
        if not messages:
            return f"У чаті «{meeting.title}» поки тихо."
        lines = [f"💬 Чат «{meeting.title}»:"]
        for t, m in messages[-20:]:
            when = f"{t:%H:%M}" if t else "раніше"
            lines.append(f"{when} {m.author or '?'}: {m.text}")
        lines.append("\n↩️ Відповідай на це повідомлення — і я напишу твій текст у чат.")
        return "\n".join(lines)

    def photo(self, content: bytes, caption: str) -> None:
        if self.media is not None:
            self.media.photo(content, caption)

    def _status(self, meeting: Meeting | None, now: datetime) -> str:
        if meeting is None:
            return "Зараз я не на парі."
        left = int((meeting.end - now).total_seconds() // 60)
        lines = [f"📚 «{meeting.title}», " + (f"до кінця {left} хв" if left > 0 else f"затримується на {-left} хв")]
        if meeting.participants:
            lines.append(f"👥 Учасників: {meeting.participants}")
        if meeting.heard:
            lines.append("👂 " + " … ".join(text for _, text in list(meeting.heard)[-3:]))
        if meeting.chat:
            lines.append("💬 " + " | ".join(f"{m.author}: {m.text}" for _, m in list(meeting.chat)[-3:]))
        return "\n".join(lines)
