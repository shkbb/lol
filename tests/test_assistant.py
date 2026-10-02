from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from autopara.assistant import Action, Assistant, ChatMessage, Decision, name_patterns
from autopara.config import MeSettings

TZ = ZoneInfo("Europe/Kyiv")
KEY = "lesson"


def at(hhmm):
    return datetime.fromisoformat(f"2026-10-05T{hhmm}").replace(tzinfo=TZ)


class Inbox:
    def __init__(self):
        self.messages = []

    def __call__(self, text, urgent=False):
        self.messages.append((text, urgent))

    def text(self):
        return "\n".join(m for m, _ in self.messages)


def make(chat_reply="auto", brain=False):
    inbox, woken = Inbox(), []
    me = MeSettings(name="Ляшко Денис", chat_reply=chat_reply)
    assistant = Assistant(me, inbox, woken.append if brain else None)
    assistant.attach(KEY, "Бази даних (Лк)", "Петренко П.П.", at("09:30"), at("10:50"))
    assistant.on_chat(KEY, [ChatMessage("old", "Іван", "+")], at("09:31"))  # історія чату — без реакції
    return assistant, inbox, woken


def chat(assistant, now, *messages):
    meeting = assistant.meetings[KEY]
    existing = [m for _, m in meeting.chat]
    new = [ChatMessage(f"{now}-{i}", author, text) for i, (author, text) in enumerate(messages)]
    assistant.on_chat(KEY, existing + new, at(now))


def test_chat_history_before_we_came_is_ignored():
    assistant = Assistant(MeSettings(name="Ляшко Денис"), Inbox())
    assistant.attach(KEY, "Бази даних (Лк)", "", at("09:30"), at("10:50"))
    assistant.on_chat(KEY, [ChatMessage(str(i), f"Студент {i}", "+") for i in range(5)], at("09:40"))
    assert assistant.take_actions() == []


def test_plus_burst_is_mirrored_once():
    assistant, inbox, _ = make()
    chat(assistant, "10:00", ("Петренко П.П.", "Поставте + хто є"), ("Іван Коваль", "+"), ("Олена Бойко", "+"))
    assert assistant.take_actions() == []  # лише двоє — ще не «група»
    chat(assistant, "10:01", ("Марко Лис", "++"))
    assert assistant.take_actions() == [Action("chat", KEY, "+", "Група пише «+» у чат")]
    chat(assistant, "10:02", ("Ще Хтось", "+"))
    assert assistant.take_actions() == []


def test_my_own_messages_do_not_count():
    assistant, _, _ = make()
    chat(assistant, "10:00", ("Ви", "+"), ("Ляшко Денис Олегович", "+"), ("Іван Коваль", "+"), ("Олена Бойко", "+"))
    assert assistant.take_actions() == []


def test_presence_words_are_counted_together_and_answered_with_my_phrase():
    assistant, _, _ = make()
    chat(assistant, "10:00", ("Іван Коваль", "Присутній"), ("Олена Бойко", "присутня"), ("Марко Лис", "тут"))
    assert [a.text for a in assistant.take_actions()] == ["Присутній"]


def test_quiz_answers_are_never_mirrored():
    assistant, _, _ = make()
    chat(assistant, "10:00", ("Іван Коваль", "б"), ("Олена Бойко", "б"), ("Марко Лис", "б"), ("Ліна Ткач", "б"))
    assert assistant.take_actions() == []


def test_roll_call_by_surnames():
    assistant, _, _ = make()
    chat(assistant, "10:00", ("Іван Коваль", "Коваль Іван"), ("Олена Бойко", "Бойко"), ("Марко Лис", "Лис Марко ТЦР-32"))
    assert [a.text for a in assistant.take_actions()] == ["Ляшко Денис"]


def test_ask_mode_waits_for_confirmation():
    assistant, inbox, _ = make(chat_reply="ask")
    chat(assistant, "10:00", ("Іван Коваль", "+"), ("Олена Бойко", "+"), ("Марко Лис", "+"))
    assert assistant.take_actions() == []
    assert "Написати «+»? Відповідай: так / ні" in inbox.text()
    assert assistant.on_command("Так", at("10:01")) == "Пишу «+»."
    assert [a.text for a in assistant.take_actions()] == ["+"]


def test_ask_mode_times_out():
    assistant, inbox, _ = make(chat_reply="ask")
    chat(assistant, "10:00", ("Іван Коваль", "+"), ("Олена Бойко", "+"), ("Марко Лис", "+"))
    assistant.tick(at("10:06"))
    assert "Не дочекався" in inbox.text()
    assert assistant.on_command("так", at("10:07")) == "Не зрозумів.\n\n" + assistant.on_command("/help", at("10:07"))


def test_off_mode_only_notifies():
    assistant, inbox, _ = make(chat_reply="off")
    chat(assistant, "10:00", ("Іван Коваль", "+"), ("Олена Бойко", "+"), ("Марко Лис", "+"))
    assert assistant.take_actions() == [] and "Сам не пишу" in inbox.text()


def test_chat_rate_limit():
    assistant, inbox, _ = make()
    chat(assistant, "10:00", ("Іван Коваль", "+"), ("Олена Бойко", "+"), ("Марко Лис", "+"))
    [action] = assistant.take_actions()
    assistant.chat_done(KEY, action.text, True, at("10:00"), action.reason)
    chat(assistant, "10:01", ("Іван Коваль", "Дякую"), ("Олена Бойко", "дякую!"), ("Марко Лис", "Дякую за пару"))
    assert assistant.take_actions() == []
    assert "писав у чат нещодавно" in inbox.text()


@pytest.mark.parametrize("phrase", ["Ляшко, ви тут?", "Ляшка не бачу", "Денисе, відповідайте"])
def test_mention_in_speech_is_urgent(phrase):
    assistant, inbox, _ = make()
    assistant.on_heard(phrase, at("10:00"))
    [(text, urgent)] = inbox.messages
    assert urgent and phrase in text


def test_name_patterns_decline():
    (surname, is_surname), (first, _) = name_patterns("Ляшко Денис")
    assert is_surname and surname.search("Ляшком") and surname.search("ЛЯШКУ")
    assert first.search("Денисе") and not first.search("артеріальний")


def test_roll_call_in_speech_without_brain():
    assistant, inbox, woken = make()
    assistant.on_heard("Так, давайте перекличка, поставте плюс у чат", at("10:00"))
    assert "перекличка" in inbox.text() and woken == []


def test_attention_wakes_brain_instead():
    assistant, inbox, woken = make(brain=True)
    assistant.on_heard("Поставте плюс у чат, будь ласка", at("10:00"))
    assert woken == [KEY] and inbox.messages == []


def test_brain_decisions_are_constrained():
    assistant, inbox, _ = make()
    assistant.apply_decision(KEY, Decision("chat_plus", reason="викладач просить +"), at("10:00"))
    assert [a.text for a in assistant.take_actions()] == ["+"]
    assistant.apply_decision(KEY, Decision("leave", reason="попрощались"), at("09:50"))
    assert assistant.take_actions() == [] and "пара ще в розпалі" in inbox.text()
    assistant.apply_decision(KEY, Decision("leave", reason="попрощались"), at("10:30"))
    assert [a.kind for a in assistant.take_actions()] == ["leave"]
    assistant.apply_decision(KEY, Decision("notify", message="Викладач просить увімкнути камери"), at("10:31"))
    assert "увімкнути камери" in inbox.text()


def test_telegram_commands():
    assistant, _, _ = make()
    assert assistant.on_command("напиши Перепрошую, зв'язок пропав", at("10:00")) == "Пишу «Перепрошую, зв'язок пропав»."
    assert assistant.on_command("вийди", at("10:00")) == "Виходжу."
    assert [a.kind for a in assistant.take_actions()] == ["chat", "leave"]
    assistant.on_participants(KEY, 23)
    status = assistant.on_command("статус", at("10:00"))
    assert "Бази даних" in status and "до кінця 50 хв" in status and "23" in status


def test_snapshot_for_the_brain():
    assistant, _, _ = make()
    assistant.on_heard("Сьогодні говоримо про індекси", at("10:00"))
    chat(assistant, "10:01", ("Іван Коваль", "А це буде на модулі?"))
    snapshot = assistant.snapshot(KEY, at("10:02"))
    assert "Бази даних" in snapshot and "індекси" in snapshot and "Іван Коваль: А це буде на модулі?" in snapshot
    assert assistant.snapshot(KEY, at("10:02") + timedelta(minutes=10)).count("індекси") == 0
