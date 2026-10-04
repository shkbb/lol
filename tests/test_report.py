import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from autopara.assistant import Assistant, ChatMessage, LessonRecord
from autopara.brain import BrainWorker
from autopara.config import MeSettings
from autopara.report import FINAL_PROMPT, Reporter, render

TZ = ZoneInfo("Europe/Kyiv")


def at(hhmm):
    return datetime.fromisoformat(f"2026-10-05T{hhmm}").replace(tzinfo=TZ)


class Inbox(list):
    def __call__(self, text, urgent=False):
        self.append(text)

    def document(self, content, filename, caption):
        self.append((filename, content.decode("utf-8"), caption))


class FakeBrain:
    def __init__(self):
        self.calls = []

    def summarize(self, instruction, text):
        self.calls.append((instruction, text))
        if instruction == FINAL_PROMPT:
            return "Про що: індекси в БД\nЩо робили: розбирали B-дерева\nЗавдання й дедлайни: лаба 3 до п'ятниці\nВажливе: нічого"
        return f"• конспект {len(self.calls)}"


class InlineWorker:
    def submit(self, job):
        job()


def lesson_assistant(reporter):
    assistant = Assistant(MeSettings(name="Ляшко Денис"), lambda text, urgent=False: None)
    assistant.on_chunk, assistant.on_finished = reporter.chunk, reporter.finish
    assistant.attach("pair", "Бази даних (Лк)", "Петренко П.П.", at("09:30"), at("10:50"), now=at("09:28"))
    assistant.on_chat("pair", [], at("09:29"))
    return assistant


def test_report_with_brain_and_transcript_file(tmp_path):
    inbox, brain = Inbox(), FakeBrain()
    reporter = Reporter(inbox, brain, InlineWorker(), tmp_path)
    assistant = lesson_assistant(reporter)
    for minute in range(10, 40):
        assistant.on_heard("Розглядаємо індекси і B-дерева, " * 5, at(f"09:{minute}"))
    assistant.on_heard("Ляшко, ви тут?", at("09:41"))
    assistant.on_participants("pair", 24)
    assistant.on_chat("pair", [ChatMessage("1", "Іван Коваль", "А лаба 3 до коли?")], at("09:45"))
    assistant.detach("pair", at("10:52"))

    chunks = [text for instruction, text in brain.calls if instruction != FINAL_PROMPT]
    assert len(chunks) >= 2  # конспектували частинами ще під час пари
    report, (filename, transcript, caption) = inbox
    assert report.startswith("📝 Звіт: «Бази даних (Лк)», 09:30–10:50")
    assert "на парі з 09:28 до 10:52 · 👥 до 24 учасників" in report
    assert "Завдання й дедлайни: лаба 3 до п'ятниці" in report
    assert "🔔 Тебе згадували:\n  09:41 «Ляшко, ви тут?»" in report
    assert filename == "2026-10-05 09-30 Лк.txt" and caption == "Розшифровка «Бази даних (Лк)»"
    assert "[09:41:00] Ляшко, ви тут?" in transcript and "Іван Коваль: А лаба 3 до коли?" in transcript
    saved = (tmp_path / "Бази даних" / filename).read_text(encoding="utf-8")  # тека предмета
    assert "--- Підсумок ---\nПро що:" in saved and "[09:41:00] Ляшко, ви тут?" in saved


def test_lesson_is_kept_even_without_telegram_reports(tmp_path):
    inbox = Inbox()
    reporter = Reporter(inbox, None, None, tmp_path, send=False)
    assistant = lesson_assistant(reporter)
    assistant.on_heard("Сьогодні нормалізація", at("09:40"))
    assistant.detach("pair", at("10:52"))
    assert list(inbox) == [] and (tmp_path / "Бази даних" / "2026-10-05 09-30 Лк.txt").exists()


def test_report_without_brain_or_ears():
    record = LessonRecord("pair#1", "Філософія (Лк)", "", at("11:10"), at("12:30"), at("11:08"), at("12:31"),
                          [], [], [], [(at("11:40"), "+")], 0, "")
    report = render(record, "")
    assert "Вуха вимкнені" in report and "💬 Я писав у чат: «+» о 11:40" in report


def test_next_pair_in_the_same_meeting_gets_its_own_report():
    inbox = Inbox()
    assistant = lesson_assistant(Reporter(inbox))
    assistant.on_heard("Перша пара", at("10:00"))
    assistant.next_lesson("pair", "Бази даних (Пз)", at("11:10"), at("12:30"), now=at("11:08"))
    assistant.on_heard("Друга пара", at("11:30"))
    assistant.detach("pair", at("12:31"))
    deadline = threading.Event()
    deadline.wait(0.5)  # без мозку звіт складається в окремому потоці
    reports = [item for item in inbox if isinstance(item, str)]
    assert [r.splitlines()[0] for r in reports] == [
        "📝 Звіт: «Бази даних (Лк)», 09:30–10:50",
        "📝 Звіт: «Бази даних (Пз)», 11:10–12:30",
    ]


def test_decisions_go_before_background_summaries():
    order = []

    class Brain:
        def decide(self, situation):
            order.append("decision")
            from autopara.assistant import Decision
            return Decision("none")

    class Assistant_:
        def snapshot(self, key, now):
            return "ситуація"

        def apply_decision(self, key, decision, now):
            pass

    worker = BrainWorker(Brain(), clock=lambda: None)
    worker.assistant = Assistant_()
    worker.submit(lambda: order.append("summary"))
    worker.wake("pair")
    worker.start()
    for _ in range(50):
        if len(order) == 2:
            break
        threading.Event().wait(0.05)
    worker.stop()
    assert order == ["decision", "summary"]
