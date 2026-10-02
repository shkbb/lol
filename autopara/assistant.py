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
# Що варто показати «мозку»: прохання до всіх щось зробити.
ATTENTION_RE = re.compile(
    r"плюс|перекличк|відміч|відміт|присутн|хто (є|тут|зараз)|напишіть|поставте|відпишіть|"
    r"увімкніть|ввімкніть|включіть|камер|здайте|надішліть|дедлайн|контрольн",
    re.I,
)
ME_AUTHOR_RE = re.compile(r"^(you|ви|вы)$", re.I)

HELP = (
    "Я пишу, коли щось відбувається на парі. Що можна відповісти:\n"
    "так / ні — на моє питання «написати в чат?»\n"
    "+ — написати «+» у чат\n"
    "напиши <текст> — написати твій текст у чат\n"
    "статус — що зараз на парі\n"
    "вийди — вийти з пари"
)


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
class Meeting:
    key: str
    title: str
    teacher: str
    start: datetime
    end: datetime
    chat: deque = field(default_factory=lambda: deque(maxlen=300))  # (час, ChatMessage)
    seen: set[str] = field(default_factory=set)
    chat_primed: bool = False
    heard: deque = field(default_factory=lambda: deque(maxlen=80))  # (час, текст)
    participants: int | None = None
    sent: list[tuple[datetime, str]] = field(default_factory=list)
    reacted: set[str] = field(default_factory=set)  # на що вже відреагували, щоб не повторюватись
    pending: tuple[str, datetime, str] | None = None  # (текст, до коли чекаємо, чому) — режим "ask"


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
    def __init__(self, me: MeSettings, notify: Callable[..., None], wake_brain: Callable[[str], None] | None = None):
        self.me = me
        self.notify = notify  # notify(text, urgent=False)
        self.wake_brain = wake_brain  # None — «мозок» вимкнено, працюють лише рефлекси
        self.meetings: dict[str, Meeting] = {}
        self._order: list[str] = []
        self._actions: list[Action] = []
        self._lock = threading.RLock()
        self._patterns = name_patterns(me.name)
        self._last_mention: datetime | None = None
        self._last_roll_call: datetime | None = None

    # --- Життєвий цикл зустрічі ------------------------------------------------

    def attach(self, key: str, title: str, teacher: str, start: datetime, end: datetime) -> None:
        with self._lock:
            self.meetings[key] = Meeting(key, title, teacher, start, end)
            self._order.append(key)

    def next_lesson(self, key: str, title: str, start: datetime, end: datetime) -> None:
        """Наступна пара в тій самій зустрічі."""
        with self._lock:
            if meeting := self.meetings.get(key):
                meeting.title, meeting.start, meeting.end = title, start, end
                meeting.reacted.clear()
                meeting.sent.clear()

    def detach(self, key: str) -> None:
        with self._lock:
            self.meetings.pop(key, None)
            if key in self._order:
                self._order.remove(key)

    def current(self) -> Meeting | None:
        with self._lock:
            return self.meetings[self._order[-1]] if self._order else None

    # --- Очі й вуха --------------------------------------------------------------

    def on_participants(self, key: str, count: int | None) -> None:
        with self._lock:
            if meeting := self.meetings.get(key):
                meeting.participants = count

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

    def on_heard(self, text: str, now: datetime) -> None:
        """Розпізнана фраза з пари (викликається з потоку «вух»)."""
        text = text.strip()
        with self._lock:
            meeting = self.current()
            if meeting is None or not text:
                return
            meeting.heard.append((now, text))
            mentioned = self._check_mention(meeting, text, now, "на парі")
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
        if self._cooled(self._last_mention, now, MENTION_COOLDOWN):
            self._last_mention = now
            lead = "🚨 Тебе кличуть" if any(hits) else "❗ Можливо, звертаються до тебе"
            self.notify(f"{lead} {where} на «{meeting.title}»: «{_snippet(text)}»", urgent=True)
            if self.wake_brain:
                self.wake_brain(meeting.key)
        return True

    def _recent_chat(self, meeting: Meeting, now: datetime) -> list[ChatMessage]:
        return [m for t, m in meeting.chat if now - t <= BURST_WINDOW and not self._is_me(m.author)]

    def _chat_reflexes(self, meeting: Meeting, now: datetime) -> None:
        recent = self._recent_chat(meeting, now)
        # 1. Кілька людей пишуть одне й те саме («+», «присутній», «дякую») — дублюємо.
        authors: dict[int, set[str]] = {}
        sample: dict[int, str] = {}
        for message in recent:
            text = _norm(message.text)
            for group, (pattern, _) in enumerate(ECHO_GROUPS):
                if pattern.match(text):
                    authors.setdefault(group, set()).add(message.author or message.id)
                    sample.setdefault(group, message.text.strip())
                    break
        for group, who in sorted(authors.items(), key=lambda item: -len(item[1])):
            mark = f"echo:{group}"
            if len(who) >= BURST_MIN_AUTHORS and mark not in meeting.reacted:
                reply = ECHO_GROUPS[group][1] or self.me.present
                self.propose_chat(meeting, reply, f"Група пише «{sample[group]}» у чат", now, mark=mark)
                return
        # 2. Люди відмічаються прізвищами — пишемо своє.
        by_name = {
            message.author
            for message in recent
            if message.author
            and len(message.text) <= 60
            and any(word[:-1] in message.text.casefold() for word in message.author.casefold().split() if len(word) > 3)
        }
        if len(by_name) >= BURST_MIN_AUTHORS and self.me.name and "__name__" not in meeting.reacted:
            self.propose_chat(meeting, self.me.name, "Група відмічається в чаті прізвищами", now, mark="__name__")

    # --- Руки --------------------------------------------------------------------

    def _can_chat(self, meeting: Meeting, now: datetime) -> bool:
        if len(meeting.sent) >= MAX_CHAT_PER_MEETING:
            return False
        return not meeting.sent or now - meeting.sent[-1][0] >= CHAT_COOLDOWN

    def propose_chat(self, meeting: Meeting, text: str, reason: str, now: datetime, mark: str) -> None:
        if mark in meeting.reacted:
            return
        meeting.reacted.add(mark)
        policy = self.me.chat_reply
        if policy == "off":
            self.notify(f"{reason} на «{meeting.title}». Сам не пишу (chat_reply = \"off\").", urgent=True)
        elif not self._can_chat(meeting, now):
            self.notify(f"{reason} на «{meeting.title}», але я вже писав у чат нещодавно — пропускаю.", urgent=True)
        elif policy == "ask":
            meeting.pending = (text, now + ASK_TIMEOUT, reason)
            self.notify(f"{reason} на «{meeting.title}». Написати «{text}»? Відповідай: так / ні", urgent=True)
        else:
            self._actions.append(Action("chat", meeting.key, text, reason))

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
                if meeting.pending and now > meeting.pending[1]:
                    meeting.pending = None
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
            heard = [(t, text) for t, text in meeting.heard if now - t <= timedelta(minutes=3)]
            if heard:
                lines.append("\nЩо сказали на парі за останні хвилини (автоматичне розпізнавання, можливі помилки):")
                lines += [f"[{t:%H:%M}] {text}" for t, text in heard][-20:]
            chat = list(meeting.chat)[-15:]
            if chat:
                lines.append("\nОстанні повідомлення в чаті:")
                lines += [f"[{t:%H:%M}] {m.author or '?'}: {m.text}" for t, m in chat]
            return "\n".join(lines)

    def apply_decision(self, key: str, decision: Decision, now: datetime) -> None:
        with self._lock:
            meeting = self.meetings.get(key)
            if meeting is None or decision.action == "none":
                return
            why = decision.reason or decision.message
            log.info("Мозок: %s — %s", decision.action, why)
            if decision.action == "notify":
                self.notify(f"🧠 {decision.message or why} («{meeting.title}»)", urgent=True)
            elif decision.action == "chat_plus":
                self.propose_chat(meeting, "+", f"🧠 {why}", now, mark="echo:0")
            elif decision.action == "chat_name" and self.me.name:
                self.propose_chat(meeting, self.me.name, f"🧠 {why}", now, mark="__name__")
            elif decision.action == "leave":
                # Мовна модель може помилитись — на початку пари її «на сьогодні все» не слухаємо.
                if now >= meeting.start + (meeting.end - meeting.start) / 2:
                    self._actions.append(Action("leave", key, reason=f"🧠 {why}"))
                else:
                    self.notify(
                        f"🧠 Пропоную вийти з «{meeting.title}» ({why}), але пара ще в розпалі — лишаюсь. "
                        "Напиши «вийди», якщо згоден."
                    )

    # --- Команди з Telegram ------------------------------------------------------

    def on_command(self, text: str, now: datetime) -> str:
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
            if low in ("так", "yes", "ок", "ok", "давай", "+") and meeting.pending:
                reply, _, reason = meeting.pending
                meeting.pending = None
                self._actions.append(Action("chat", meeting.key, reply, f"{reason}; ти підтвердив"))
                return f"Пишу «{reply}»."
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
