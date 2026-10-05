"""Друге розпізнавання після пари: звук пари пишеться у файл, після пари розпізнається вдруге точніше,
і звіт робиться вже з цієї розшифровки (жива лишається для порівняння)."""

import struct
import threading
import time
import types

from autopara import ears
from autopara.ears import ModelGate, WhisperTranscriber, split_quietly
from autopara.refine import LessonAudio, Refiner, record_from_json, record_to_json
from autopara.report import FINAL_PROMPT, Reporter

from .test_report import TZ, FakeBrain, InlineWorker, Inbox, at, lesson_assistant


def tone(seconds, amplitude):
    return struct.pack(f"<{int(16000 * seconds)}h", *([amplitude, -amplitude] * int(8000 * seconds)))


def test_live_ears_always_go_first():
    gate = ModelGate(idle_seconds=0.3)
    order = []
    with gate.live():
        order.append("live")
    started = time.monotonic()

    def background():
        with gate.background():
            order.append("background")

    thread = threading.Thread(target=background)
    thread.start()
    time.sleep(0.1)
    with gate.live():  # вуха щойно працювали — уточнення ще чекає
        order.append("live again")
    thread.join(5)
    assert order == ["live", "live again", "background"]
    assert time.monotonic() - started >= 0.3  # уточнення пішло лише після паузи у вух

    stop = threading.Event()
    stop.set()
    with gate.live():
        pass
    try:
        with gate.background(stop):
            raise AssertionError("не мало дочекатись")
    except InterruptedError:
        pass  # програма зупиняється — уточнення не чекає далі


def test_long_recording_is_split_in_a_quiet_place():
    pcm = tone(50, 3000) + tone(1, 0) + tone(60, 3000)
    pieces = split_quietly(pcm, 60)
    assert [round(offset) for offset, _ in pieces] == [0, 50]  # різ у тиші перед межею, а не на 60-й секунді
    assert sum(len(piece) for _, piece in pieces) == len(pcm)
    assert len(split_quietly(tone(10, 3000), 60)) == 1


class Segment:
    def __init__(self, start, text):
        self.start, self.end, self.text = start, start + 2, text
        self.avg_logprob, self.no_speech_prob = -0.2, 0.01


def test_transcribe_long_uses_context_and_keeps_time(monkeypatch):
    calls = []

    class WhisperModel:
        def __init__(self, name, device, compute_type):
            pass

        def transcribe(self, audio, **options):
            calls.append(options)
            return iter([Segment(1.0, "Добрий день"), Segment(5.0, "далі далі далі далі тема"),
                         Segment(9.0, "Ляшко Денис, Бази даних")]), None

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = WhisperModel
    monkeypatch.setitem(__import__("sys").modules, "faster_whisper", module)
    transcriber = WhisperTranscriber("large-v3", "uk")
    transcriber.gate = ModelGate(idle_seconds=0)
    lines = transcriber.transcribe_long(tone(12, 3000), hint="Ляшко Денис, Бази даних")
    assert lines == [(1.0, "Добрий день"), (5.0, "далі тема")]  # зациклення й «вимовлена» підказка — геть
    assert calls[0]["beam_size"] == 5 and calls[0]["condition_on_previous_text"]
    assert calls[0]["initial_prompt"] == "Ляшко Денис, Бази даних"


class FakeTranscriber:
    def __init__(self, fail=False, silent=False):
        self.fail, self.silent = fail, silent
        self.calls = []

    def transcribe_long(self, pcm, hint="", stop=None):
        self.calls.append((len(pcm), hint))
        if self.fail:
            raise RuntimeError("модель зламалась")
        if self.silent:
            return []
        return [(2.0, "Добрий день, сьогодні індекси"), (40.0, "Домашнє завдання: лаба 3 до п'ятниці")]


def wait_for(condition, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return
        time.sleep(0.05)
    raise AssertionError("не дочекався")


def lesson_with_audio(tmp_path, transcriber):
    inbox, brain = Inbox(), FakeBrain()
    reporter = Reporter(inbox, brain, InlineWorker(), tmp_path / "Пари")
    clock_now = [at("09:30")]
    audio = LessonAudio(tmp_path / "audio", lambda: clock_now[0])
    refiner = Refiner(tmp_path / "audio", lambda: transcriber, lambda record: f"Ляшко Денис, {record.title}",
                      reporter.complete, reporter.fallback)
    reporter.audio, reporter.refiner = audio, refiner
    return inbox, brain, reporter, audio, refiner


def test_report_comes_from_the_refined_transcript(tmp_path):
    transcriber = FakeTranscriber()
    inbox, brain, reporter, audio, refiner = lesson_with_audio(tmp_path, transcriber)
    refiner.start()
    try:
        assistant = lesson_assistant(reporter)
        assistant.on_started = audio.start
        audio.start("pair#1")  # як робить attach через on_started
        audio.feed(tone(60, 3000))  # хвилина звуку пари
        assistant.on_heard("Добрий день сьогодні індекси", at("09:31"))
        assistant.detach("pair", at("10:52"))
        assert inbox[0].startswith("📝 «Бази даних (Лк)» скінчилась. Звіт пришлю")  # звіт — пізніше
        wait_for(lambda: len(inbox) == 3)
    finally:
        refiner.stop()
    report, (filename, saved, caption) = inbox[1], inbox[2]
    assert "Завдання й дедлайни: лаба 3 до п'ятниці" in report and report.endswith("🎧 Розшифровку уточнено після пари.")
    assert transcriber.calls == [(16000 * 2 * 60, "Ляшко Денис, Бази даних (Лк)")]
    chunks = [text for instruction, text in brain.calls if instruction != FINAL_PROMPT]
    assert "лаба 3 до п'ятниці" in chunks[-1]  # конспект — з уточненої розшифровки
    # Час фраз — від початку звуку пари; жива розшифровка — в кінці, для порівняння.
    assert "--- Розшифровка ---\n[09:29:02] Добрий день, сьогодні індекси\n[09:29:40] Домашнє завдання" in saved
    assert saved.rstrip().endswith("--- Розшифровка наживо (для порівняння) ---\n[09:31:00] Добрий день сьогодні індекси")
    assert list((tmp_path / "audio").iterdir()) == []  # звук видалено, щойно розшифровано


import pytest  # noqa: E402


@pytest.mark.parametrize("transcriber", [FakeTranscriber(fail=True), FakeTranscriber(silent=True)],
                         ids=["model-broke", "heard-nothing"])
def test_refining_failure_still_sends_the_live_report(tmp_path, transcriber):
    inbox, _, reporter, audio, refiner = lesson_with_audio(tmp_path, transcriber)
    refiner.start()
    try:
        assistant = lesson_assistant(reporter)
        audio.start("pair#1")
        audio.feed(tone(60, 3000))
        assistant.on_heard("Сьогодні нормалізація", at("09:40"))
        assistant.detach("pair", at("10:52"))
        wait_for(lambda: len(inbox) == 3)
    finally:
        refiner.stop()
    assert inbox[1].startswith("📝 Звіт: «Бази даних (Лк)»") and "уточнено" not in inbox[1]
    assert "[09:40:00] Сьогодні нормалізація" in inbox[2][1]
    assert list((tmp_path / "audio").iterdir()) == []


def test_refining_continues_after_restart(tmp_path):
    inbox, _, reporter, audio, refiner = lesson_with_audio(tmp_path, None)  # модель ще не завантажилась…
    refiner.start()
    assistant = lesson_assistant(reporter)
    audio.start("pair#1")
    audio.feed(tone(60, 3000))
    assistant.on_heard("Сьогодні нормалізація", at("09:40"))
    assistant.detach("pair", at("10:52"))
    refiner.stop()  # …і програму закрили
    assert len(list((tmp_path / "audio").glob("*.raw"))) == 1 and len(inbox) == 1

    transcriber = FakeTranscriber()
    inbox2, _, _, _, refiner2 = lesson_with_audio(tmp_path, transcriber)
    refiner2.start()
    try:
        wait_for(lambda: len(inbox2) == 2)
    finally:
        refiner2.stop()
    assert inbox2[0].endswith("🎧 Розшифровку уточнено після пари.") and transcriber.calls


def test_short_or_silent_lessons_are_not_refined(tmp_path):
    audio = LessonAudio(tmp_path, lambda: at("09:30"))
    audio.start("a#1")
    assert audio.stop("a#1") is None  # звуку не було
    audio.start("b#1")
    audio.feed(tone(10, 3000))
    assert audio.stop("b#1") is None and list(tmp_path.iterdir()) == []  # 10 с — нічого уточнювати


def test_lesson_record_survives_json():
    inbox, brain = Inbox(), FakeBrain()
    records = []
    reporter = Reporter(inbox, brain, InlineWorker(), None)
    reporter.finish = records.append
    assistant = lesson_assistant(reporter)
    assistant.on_finished = records.append
    from autopara.assistant import ChatMessage

    assistant.on_heard("Ляшко, ви тут?", at("09:41"))
    assistant.on_chat("pair", [ChatMessage("1", "Іван Коваль", "+")], at("09:45"))
    assistant.detach("pair", at("10:52"))
    [record] = records
    assert record_from_json(record_to_json(record)) == record
    assert record.start.tzinfo == TZ
