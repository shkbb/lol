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
    assert assistant.take_actions() == [
        Action("chat", KEY, "+", "Група ставить «+» у чат — це перекличка: «💬 Петренко П.П.: Поставте + хто є»")
    ]
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
    chat(assistant, "09:59", ("Петренко П.П.", "Напишіть прізвища, хто присутній"))
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
    assert assistant.on_command("так", at("10:07")) == "Це питання вже неактуальне."


def test_off_mode_only_notifies():
    assistant, inbox, _ = make(chat_reply="off")
    chat(assistant, "10:00", ("Іван Коваль", "+"), ("Олена Бойко", "+"), ("Марко Лис", "+"))
    assert assistant.take_actions() == [] and "Сам не пишу" in inbox.text()


def test_off_mode_never_writes_by_itself_but_your_commands_do():
    """«Ніколи»: лише слухає, сповіщає й пропонує кнопки; пише тільки за твоєю командою."""
    assistant, inbox = listening("off")
    say(assistant, "Ляшко, ви тут?")
    assistant.apply_decision(KEY, Decision("chat_plus", reason="перекличка"), at("10:01"))
    chat(assistant, "10:02", ("Іван Коваль", "Дякую"), ("Олена Бойко", "дякую!"), ("Марко Лис", "Дякую за пару"))
    assert assistant.take_actions() == []
    assert "Сам не пишу" in inbox.text() and "Відмічусь" not in inbox.text() and "Дякую" not in inbox.text()
    assistant.on_command("+", at("10:03"))
    assistant.on_command("напиши Так, я тут", at("10:03"))
    assert [a.text for a in assistant.take_actions()] == ["+", "Так, я тут"]


def test_chat_rate_limit():
    assistant, inbox, _ = make()
    chat(assistant, "10:00", ("Петренко П.П.", "Перекличка: +"), ("Іван Коваль", "+"), ("Олена Бойко", "+"), ("Марко Лис", "+"))
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


def test_brain_alone_never_acts_in_the_meeting():
    # Мозок спирається лише на розпізнану мову: «плюс» з лекції про C++ не має нічого запускати.
    assistant, inbox, _ = make()
    assistant.on_heard("Ця мова має плюси й мінуси, наприклад C плюс плюс.", at("10:00"))
    assistant.apply_decision(KEY, Decision("chat_plus", reason="викладач сказав «плюс»"), at("10:00:30"))
    assert assistant.take_actions() == []
    assert "Відмічусь, щойно в чаті відмітяться інші" in inbox.text()
    assistant.tick(at("10:10"))
    assert assistant.take_actions() == []  # група не підтвердила — нічого не пишемо
    assistant.apply_decision(KEY, Decision("leave", reason="почулось «на сьогодні все»"), at("10:40"))
    assert assistant.take_actions() == [] and "Напиши «вийди»" in inbox.text()
    assistant.apply_decision(KEY, Decision("notify", message="Викладач просить увімкнути камери"), at("10:41"))
    assert "увімкнути камери" in inbox.text()


def test_telegram_commands():
    assistant, _, _ = make()
    assert assistant.on_command("напиши Перепрошую, зв'язок пропав", at("10:00")) == "Пишу «Перепрошую, зв'язок пропав»."
    assert assistant.on_command("вийди", at("10:00")) == "Виходжу."
    assert [a.kind for a in assistant.take_actions()] == ["chat", "leave"]
    assistant.on_participants(KEY, 23)
    status = assistant.on_command("статус", at("10:00"))
    assert "Бази даних" in status and "до кінця 50 хв" in status and "23" in status
    assistant.on_heard("Ляшко Денис, ви тут?", at("10:01"))  # наживо «статус» падав, щойно вуха щось чули
    assert "👂 Ляшко Денис, ви тут?" in assistant.on_command("статус", at("10:02"))


def test_snapshot_for_the_brain():
    assistant, _, _ = make()
    assistant.on_heard("Сьогодні говоримо про індекси", at("10:00"))
    chat(assistant, "10:01", ("Іван Коваль", "А це буде на модулі?"))
    snapshot = assistant.snapshot(KEY, at("10:02"))
    assert "Бази даних" in snapshot and "індекси" in snapshot and "Іван Коваль: А це буде на модулі?" in snapshot
    assert assistant.snapshot(KEY, at("10:02") + timedelta(minutes=10)).count("індекси") == 0



# --- «+» не ставимо наосліп --------------------------------------------------


def plus_wave(assistant, now="10:00"):
    chat(assistant, now, ("Іван Коваль", "+"), ("Олена Бойко", "+"), ("Марко Лис", "+"))


def test_plus_for_a_project_is_never_automatic():
    assistant, inbox, _ = make()
    chat(assistant, "09:59", ("Петренко П.П.", "Хто хоче взяти додатковий проєкт — ставте +"))
    plus_wave(assistant)
    assert assistant.take_actions() == []
    text, urgent = inbox.messages[-1]
    assert urgent and "не перекличка" in text and "додатковий проєкт" in text and "Написати «+»? Відповідай: так / ні" in text
    assert assistant.on_command("так", at("10:01")) == "Пишу «+»."  # але ти можеш погодитись


def test_roll_call_said_clearly_in_one_sentence():
    assistant, _, _ = make()
    assistant.on_heard("Поставте плюс у чат, хто на парі.", at("09:59:30"))
    plus_wave(assistant)
    assert [a.text for a in assistant.take_actions()] == ["+"]


def test_neighbour_sentence_can_not_approve():
    # «Хто на парі» і «плюс» у різних реченнях — Whisper міг їх склеїти з чогось іншого. Питаємо.
    assistant, inbox, _ = make()
    assistant.on_heard("Так, давайте перевіримо, хто на парі. Поставте плюс у чат.", at("09:59:30"))
    plus_wave(assistant)
    assert assistant.take_actions() == [] and "так / ні" in inbox.text()


def test_unconfident_speech_can_not_approve():
    assistant, inbox, _ = make()
    assistant.on_heard("Поставте плюс у чат, хто на парі.", at("09:59:30"), confident=False)
    plus_wave(assistant)
    assert assistant.take_actions() == [] and "так / ні" in inbox.text()


def test_unconfident_speech_still_vetoes():
    assistant, inbox, _ = make()
    chat(assistant, "09:59", ("Петренко П.П.", "Перекличка, ставте +"))
    assistant.on_heard("Хто хоче проєкт? Ставте плюс.", at("09:59:40"), confident=False)
    plus_wave(assistant)
    assert assistant.take_actions() == [] and "не перекличка" in inbox.text()


def test_previous_sentence_signup_vetoes():
    assistant, inbox, _ = make()
    assistant.on_heard("Хто хоче взяти додатковий проєкт? Ставте плюс у чат.", at("09:59:30"))
    plus_wave(assistant)
    assert assistant.take_actions() == [] and "не перекличка" in inbox.text()


def test_unconfident_speech_is_marked_for_the_brain_and_the_report():
    assistant, _, _ = make()
    assistant.on_heard("щось нерозбірливе", at("10:00"), confident=False)
    assert "[?] щось нерозбірливе" in assistant.snapshot(KEY, at("10:00"))
    assert assistant.meetings[KEY].transcript[-1][1] == "[?] щось нерозбірливе"


def test_signup_heard_in_speech_wins_over_lecture_topic():
    assistant, inbox, _ = make()
    assistant.on_heard("Сьогодні говоримо про перекличку процесів. Хто готовий виступити з доповіддю, поставте плюс.", at("09:59:30"))
    plus_wave(assistant)
    assert assistant.take_actions() == [] and "не перекличка" in inbox.text()


def test_lecture_about_projects_does_not_block_a_roll_call():
    assistant, _, _ = make()
    assistant.on_heard("Отже, проєкт має три етапи. А тепер перевіримо присутність: плюс у чат.", at("09:59:30"))
    plus_wave(assistant)
    assert [a.text for a in assistant.take_actions()] == ["+"]


def test_unknown_plus_without_brain_asks():
    assistant, inbox, _ = make()
    plus_wave(assistant)
    assert assistant.take_actions() == [] and "Не зрозумів, навіщо" in inbox.text()


def test_unknown_plus_asks_the_brain():
    assistant, inbox, woken = make(brain=True)
    plus_wave(assistant)
    assert woken == [KEY] and assistant.take_actions() == []
    assert "ПИТАННЯ: група ставить «+» у чат" in assistant.snapshot(KEY, at("10:00"))
    assistant.apply_decision(KEY, Decision("chat_plus", reason="викладач робить перекличку"), at("10:00:30"))
    assert [a.text for a in assistant.take_actions()] == ["+"]


def test_brain_saying_its_a_vote_turns_into_a_question():
    assistant, inbox, _ = make(brain=True)
    plus_wave(assistant)
    assistant.apply_decision(KEY, Decision("notify", message="Голосують за перенесення пари"), at("10:00:30"))
    assert assistant.take_actions() == []
    assert "Голосують за перенесення пари" in inbox.text() and "так / ні" in inbox.text()


def test_brain_too_slow_falls_back_to_the_same_rules():
    assistant, inbox, _ = make(brain=True)
    plus_wave(assistant)
    assistant.tick(at("10:02"))
    assert "мозок не встиг" in inbox.text() and "так / ні" in inbox.text()  # вуха вимкнені — лише питаємо


def test_brain_plus_is_guarded_against_signups():
    assistant, inbox, _ = make(brain=True)
    assistant.on_heard("Хто бажає поїхати на конференцію — плюс у чат.", at("10:00"))
    assistant.apply_decision(KEY, Decision("chat_plus", reason="просять плюс"), at("10:00:20"))
    chat(assistant, "10:01", ("Іван Коваль", "+"), ("Олена Бойко", "+"))
    assert assistant.take_actions() == [] and "не перекличка" in inbox.text()


# --- Telegram: чат, екран, відповіді --------------------------------------


def test_reply_to_any_bot_message_goes_to_chat():
    assistant, _, _ = make()
    assert assistant.on_command("Так, я тут, проблеми з мікрофоном", at("10:00"), is_reply=True) == (
        "Пишу «Так, я тут, проблеми з мікрофоном»."
    )
    assert [a.text for a in assistant.take_actions()] == ["Так, я тут, проблеми з мікрофоном"]


def test_command_words_stay_commands_even_as_replies():
    assistant, _, _ = make()
    assert assistant.on_command("статус", at("10:00"), is_reply=True).startswith("📚")
    assert assistant.on_command("ні", at("10:00"), is_reply=True) == "Добре, не пишу."
    assert assistant.take_actions() == []


def test_chat_view_includes_history_before_we_came():
    assistant, _, _ = make()
    chat(assistant, "10:00", ("Іван Коваль", "Посилання на презентацію?"))
    view = assistant.on_command("чат", at("10:01"))
    assert "раніше Іван: +" in view and "10:00 Іван Коваль: Посилання на презентацію?" in view
    assert "Відповідай на це повідомлення" in view


def test_screenshot_command():
    assistant, _, _ = make()
    assert assistant.on_command("екран", at("10:00")) == "Роблю скріншот..."
    assert assistant.take_actions() == [Action("screenshot", KEY)]



# --- Автономність: діємо, коли незалежні сигнали згодні ----------------------


def listening(chat_reply="auto"):
    assistant, inbox, woken = make(chat_reply)
    assistant.ears_on = True
    assistant.on_heard("Отже, сьогодні розбираємо нормалізацію таблиць.", at("09:58"))
    return assistant, inbox


def test_brain_and_group_together_act_without_asking():
    assistant, inbox, _ = make(brain=True)
    assistant.apply_decision(KEY, Decision("chat_plus", reason="викладач просить відмітитись"), at("10:00"))
    chat(assistant, "10:01", ("Іван Коваль", "+"), ("Олена Бойко", "+"))  # досить двох
    assert [a.text for a in assistant.take_actions()] == ["+"]


def test_unclear_wave_with_clean_hearing_acts_after_veto_window():
    assistant, inbox = listening()
    plus_wave(assistant)
    assert assistant.take_actions() == []
    assert "Напишу «+» за 45 с, якщо не відповіси «ні»" in inbox.text()
    assistant.tick(at("10:00:30"))
    assert assistant.take_actions() == []
    assistant.tick(at("10:00:50"))
    [action] = assistant.take_actions()
    assert action.text == "+" and action.reason.endswith("ти не заперечив")


def test_you_can_veto_in_time():
    assistant, inbox = listening()
    plus_wave(assistant)
    assert assistant.on_command("ні", at("10:00:20")) == "Добре, не пишу."
    assistant.tick(at("10:01"))
    assert assistant.take_actions() == []


def test_unclear_wave_with_garbled_hearing_only_asks():
    assistant, inbox = listening()
    assistant.on_heard("хто хо про ект", at("09:59:40"), confident=False)  # могли сказати що завгодно
    plus_wave(assistant)
    assistant.tick(at("10:02"))
    assert assistant.take_actions() == [] and "Не зрозумів, навіщо" in inbox.text()


def test_unclear_wave_without_ears_only_asks():
    assistant, inbox, _ = make()
    plus_wave(assistant)
    assistant.tick(at("10:02"))
    assert assistant.take_actions() == []


def test_ask_mode_never_acts_on_its_own():
    assistant, inbox = listening(chat_reply="ask")
    plus_wave(assistant)
    assistant.tick(at("10:06"))
    assert assistant.take_actions() == [] and "Не дочекався відповіді" in inbox.text()


GROUP = ["Бойко Олена", "Коваль Іван", "Лис Марко", "Ткач Ліна", "Ляшко Денис"]


def say(assistant, *lines, start="10:00:00", confident=True):
    base = datetime.fromisoformat(f"2026-10-05T{start}").replace(tzinfo=TZ)
    for i, line in enumerate(lines):
        assistant.on_heard(line, base + timedelta(seconds=10 * i), confident=confident)


def test_voice_roll_call_with_answers():
    assistant, inbox = listening()
    assistant.set_classmates(GROUP)
    say(assistant, "Бойко.", "Є.", "Коваль — тут.", "Лис.", "Я тут.")
    assert assistant.take_actions() == []
    say(assistant, "Ляшко.", start="10:01:00")
    [action] = assistant.take_actions()
    assert action.text == "Присутній" and "відповідали «є/тут»" in action.reason


def test_voice_roll_call_answered_in_chat():
    assistant, inbox = listening()
    assistant.set_classmates(GROUP)
    say(assistant, "Бойко.", "Коваль.")
    chat(assistant, "10:00:20", ("Олена Бойко", "+"), ("Іван Коваль", "тут"))
    say(assistant, "Ляшко.", start="10:00:30")
    assert [a.text for a in assistant.take_actions()] == ["Присутній"]


def test_russian_roll_call():
    assistant, inbox = listening()
    assistant.set_classmates(GROUP)
    say(assistant, "Так, отмечаемся. Бойко.", "Есть.", "Коваль.", "Здесь.", "Ляшко.")
    assert [a.text for a in assistant.take_actions()] == ["Присутній"]


def test_debtors_list_is_not_a_roll_call():
    # Викладач перелічує, хто не здав лабу, — «Присутній» тут був би приколом.
    assistant, inbox = listening()
    assistant.set_classmates(GROUP)
    say(assistant, "Лабораторну номер три досі не здали: Бойко, Коваль, Лис і Ляшко.")
    assert assistant.take_actions() == []
    assert "схоже не на перекличку" in inbox.text() and "Тебе кличуть" in inbox.text()


def test_names_without_answers_only_ask():
    assistant, inbox = listening()
    assistant.set_classmates(GROUP)
    say(assistant, "Бойко.", "Коваль.", "Лис.", "Ляшко.")
    assert assistant.take_actions() == [] and "ніхто не відповідав" in inbox.text()


def test_teacher_asks_you_directly():
    assistant, inbox = listening()
    say(assistant, "Ляшко, ви з нами?")
    [action] = assistant.take_actions()
    assert action.text == "Так, я тут" and "питає, чи ти тут" in action.reason


@pytest.mark.parametrize("phrase", ["Ляшко Денис, ви присутні на парі?", "Ляшко Денис втут", "Ляшко, ти присутній?"])
def test_teacher_asks_you_directly_as_heard_live(phrase):
    """Так розпізнав Whisper наживо: «присутні на парі?» і злите «втут» — теж питання саме тобі."""
    assistant, inbox = listening()
    say(assistant, phrase)
    [action] = assistant.take_actions()
    assert action.text == "Так, я тут"


def test_unclear_direct_question_is_not_answered():
    assistant, inbox = listening()
    say(assistant, "Ляшко, ви тут?", confident=False)
    assert assistant.take_actions() == [] and "Тебе кличуть" in inbox.text()


def test_content_question_is_never_answered():
    assistant, inbox = listening()
    say(assistant, "Ляшко, яка складність у бінарного пошуку?")
    assert assistant.take_actions() == [] and "Тебе кличуть" in inbox.text()


def test_surzhyk_signup_is_recognised():
    assistant, inbox, _ = make()
    chat(assistant, "09:59", ("Петренко П.П.", "Кто хочет доклад на следующую пару — плюсаните"))
    plus_wave(assistant)
    assert assistant.take_actions() == [] and "не перекличка" in inbox.text()


def test_sound_check_plus_is_harmless():
    assistant, inbox = listening()
    assistant.on_heard("Якщо мене чути, поставте плюс у чат.", at("09:59:30"))
    plus_wave(assistant)
    assert [a.text for a in assistant.take_actions()] == ["+"]


def test_plus_for_already_done_is_a_claim():
    assistant, inbox = listening()
    assistant.on_heard("Поставте плюс, хто вже здав лабу.", at("09:59:30"))
    plus_wave(assistant)
    assert assistant.take_actions() == [] and "не перекличка" in inbox.text()


def test_single_mention_is_not_a_roll_call():
    assistant, inbox = listening()
    assistant.set_classmates(["Бойко Олена", "Коваль Іван", "Лис Марко"])
    assistant.on_heard("Ляшко, як ви думаєте?", at("10:00"))
    assert assistant.take_actions() == [] and "Тебе кличуть" in inbox.text()



# --- Кнопки в Telegram -------------------------------------------------------


class Media:
    def __init__(self):
        self.asked = []

    def ask(self, text, buttons, urgent=True):
        self.asked.append((text, buttons))


def test_mention_comes_with_quick_reply_buttons():
    assistant, inbox, _ = make()
    assistant.media = Media()
    assistant.on_heard("Ляшко, яка відповідь?", at("10:00"))
    [(text, buttons)] = assistant.media.asked
    assert text.startswith("🚨 Тебе кличуть")
    assert buttons[0] == [("💬 Присутній", "say:0"), ("💬 Так, я тут", "say:1")]
    assert assistant.on_command("say:1", at("10:00:10")) == "Пишу «Так, я тут»."
    assert [a.text for a in assistant.take_actions()] == ["Так, я тут"]


def test_questions_come_with_yes_no_buttons():
    assistant, inbox, _ = make()
    assistant.media = Media()
    plus_wave(assistant)
    [(text, buttons)] = assistant.media.asked
    assert buttons == [[("✅ Написати «+»", "так"), ("❌ Ні", "ні")]]


# --- Що можна вимкнути --------------------------------------------------------


def test_roll_call_switched_off_only_asks():
    assistant, inbox, _ = make()
    assistant.me.roll_call = False
    assistant.media = Media()
    chat(assistant, "09:59", ("Петренко П.П.", "Поставте + хто є"))
    plus_wave(assistant)
    assert assistant.take_actions() == []
    [(text, buttons)] = assistant.media.asked
    assert buttons == [[("✅ Написати «+»", "так"), ("❌ Ні", "ні")]]
    assert assistant.on_command("так", at("10:00:20")) == "Пишу «+»."  # з твоєї згоди — пише


def test_answer_here_switched_off_only_asks():
    assistant, inbox = listening()
    assistant.me.answer_here = False
    say(assistant, "Ляшко, ви з нами?")
    assert assistant.take_actions() == [] and "Написати «Так, я тут»?" in inbox.text()


def test_polite_switched_off_stays_silent():
    assistant, inbox, _ = make()
    assistant.me.polite = False
    chat(assistant, "10:49", ("Іван Коваль", "Дякую"), ("Олена Бойко", "дякую!"), ("Марко Лис", "Дякую за пару"))
    assert assistant.take_actions() == [] and "Дякую" not in inbox.text()


def test_without_bot_answers_it_never_waits_for_a_reply():
    """Бот не слухає команд — спитати не можна, тож «напишу, якщо не скажеш ні» перетворюється на сповіщення."""
    assistant, inbox = listening()
    assistant.can_ask = False
    assistant.media = Media()
    plus_wave(assistant)
    assistant.tick(at("10:02"))
    assert assistant.take_actions() == [] and assistant.media.asked == []
    assert "спитати тебе не можу" in inbox.text()
    # Надійні сигнали (перекличку назвали прямо) — як і раніше, діє сам.
    chat(assistant, "10:10", ("Петренко П.П.", "Перекличка: напишіть присутній"))
    chat(assistant, "10:11", ("Іван Коваль", "присутній"), ("Олена Бойко", "присутня"), ("Марко Лис", "тут"))
    assert [a.text for a in assistant.take_actions()] == ["Присутній"]


def test_mention_alerts_can_be_switched_off():
    assistant, inbox, _ = make()
    moments = []
    assistant.on_moment = lambda *args: moments.append(args)
    assistant.alert_mentions = False
    assistant.on_heard("Ляшко, яка відповідь?", at("10:00"))
    assert inbox.messages == [] and moments == []
    assert assistant.meetings[KEY].mentions  # у звіт згадка однаково потрапить


def test_free_question_goes_to_the_brain_with_what_we_know():
    assistant, inbox = listening("off")
    asked = []
    assistant.ask_brain = lambda question, context, current: asked.append((question, context))
    assistant.on_participants(KEY, 23)
    say(assistant, "Сьогодні говоримо про індекси й B-дерева.")
    assert assistant.on_command("Про що зараз говорить викладач?", at("10:05")).startswith("🧠")
    [(question, context)] = asked
    assert question == "Про що зараз говорить викладач?"
    assert "Бази даних" in context and "23" in context and "B-дерева" in context
    assert assistant.on_command("статус", at("10:05")).startswith("📚")  # команди — як і були, без мозку
    assistant.on_command("напиши тест", at("10:05"))
    assert len(asked) == 1


def test_question_without_a_lesson_is_about_past_ones():
    assistant = Assistant(MeSettings(name="Ляшко Денис"), Inbox())
    asked = []
    assistant.ask_brain = lambda question, context, current: asked.append(context)
    assistant.on_command("Що було на минулих парах?", at("12:00"))
    assert asked and "не на парі" in asked[0]


def test_plain_words_are_actions_not_questions():
    """«Кинь скрін, що ти на парі» — наживо пішло мозку, а той відповів «скріншотів немає». Тепер це скріншот."""
    assistant, inbox = listening("off")
    asked = []
    assistant.ask_brain = lambda question, context, current: asked.append(question)
    assert assistant.on_command("кинь скрін, що ти на парі", at("10:05")) == "Роблю скріншот..."
    assert assistant.take_actions()[-1].kind == "screenshot"
    assert assistant.on_command("що пишуть у чаті?", at("10:05")).startswith(("💬", "У чаті", "Чат"))
    assert assistant.on_command("ти на парі?", at("10:05")).startswith("📚")
    # Вийти чи написати в чат звичайними словами — ні: лише точною командою.
    assert "надішли одне слово: вийди" in assistant.on_command("можеш вийти з пари", at("10:05"))
    assert not [a for a in assistant.take_actions() if a.kind in ("leave", "chat")]
    assert asked == []


def test_bot_chats_about_anything_and_remembers_the_conversation():
    assistant = Assistant(MeSettings(name="Ляшко Денис"), Inbox())
    asked = []
    assistant.ask_brain = lambda question, context, current: asked.append((question, context))
    assistant.on_command("Привіт! Як справи?", at("12:00"))
    assistant.remember_reply("Привіт! Усе добре, чекаю на наступну пару.")
    assistant.on_command("А о котрій вона?", at("12:01"))
    question, context = asked[-1]
    assert question == "А о котрій вона?"
    assert "Студент: Привіт! Як справи?" in context and "autopara: Привіт! Усе добре" in context


def test_only_replies_to_alerts_go_to_the_meeting_chat():
    assistant, inbox = listening("off")
    say(assistant, "Ляшко Денис, ви тут?")
    alert = inbox.messages[-1][0]
    assert "Сам не пишу" in alert and assistant.answers_alert(alert)  # сповіщення, що чекає відповіді
    assert not assistant.answers_alert("🧠 Сьогодні говорили про індекси.")  # відповідь мозку — це розмова
    assert not assistant.answers_alert("")


def test_brain_does_not_retell_the_lecture():
    """Наживо мозок раз на кілька хвилин переказував лекцію («Викладач обговорює…») — це спам: усе є у звіті.
    А потім ще й дописував «Студенту Ляшко Денису варто звернути увагу», щоб пройти фільтр."""
    assistant, inbox, _ = make(brain=True)
    recap = "Викладач Петренко Петро Петрович продовжує розмову про безпеку розробки, згадує Fortify і SonarQube."
    assistant.apply_decision(KEY, Decision("notify", message=recap, reason="статичний аналіз коду"), at("10:01"))
    sly = ("Викладач обговорює створення скріншотів мобільних додатків. Студенту Ляшко Денису потрібно звернути увагу "
           "на ці аспекти при виконанні завдання.")
    assistant.apply_decision(KEY, Decision("notify", message=sly, reason="скріншоти для Google Play"), at("10:20"))
    assert inbox.messages == []
    # Те, що справді сказали на парі й стосується тебе, — завжди (судимо за цитатою, а не за словами мозку).
    assistant.apply_decision(KEY, Decision("notify", message="Оголосили контрольну в понеділок.",
                                           reason="у понеділок буде контрольна"), at("10:21"))
    assistant.apply_decision(KEY, Decision("notify", message="Тебе кличуть.", reason="Ляшко, ви тут?"), at("10:22"))
    assert len(inbox.messages) == 2 and "контрольну" in inbox.messages[0][0] and "кличуть" in inbox.messages[1][0]
    # Інше нетермінове — не частіше раз на 10 хв.
    assistant.apply_decision(KEY, Decision("notify", message="Почалась демонстрація екрана."), at("10:25"))
    assistant.apply_decision(KEY, Decision("notify", message="Почалась демонстрація екрана."), at("10:40"))
    assert len(inbox.messages) == 3


def test_brain_knows_why_the_app_left():
    """Наживо на «чому вийшов раніше?» мозок вигадав відповідь — бо не знав, що робила програма."""
    assistant = Assistant(MeSettings(name="Ляшко Денис"), Inbox())
    asked = []
    assistant.ask_brain = lambda question, context, current: asked.append(context)
    assistant.note_event(at("13:26"), "👋 Вийшов з «Програмування мобільних пристроїв (Лк)»: зустріч завершилась")
    assistant.on_command("чому вийшов раніше?", at("13:27"))
    assert "13:26 👋 Вийшов з «Програмування мобільних пристроїв (Лк)»: зустріч завершилась" in asked[0]
