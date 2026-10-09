"""Збирає помічника відповідно до конфіга: сповіщення, команди з Telegram, мозок, вуха, вирізки й звіти."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator

from . import accel
from .assistant import Assistant
from .brain import BrainConnector, BrainError, BrainMemoryGuard, BrainWorker, OllamaBrain
from .config import Config
from .ears import (
    AudioQueue,
    DeviceAppCapture,
    Ears,
    EarsError,
    NoAppCapture,
    NullSink,
    PulseAppCapture,
    PulseRecorder,
    VoiceMeter,
    WhisperTranscriber,
    available_ram_gb,
    find_cable,
    pick_source,
    pulse_available,
    resolve_refine_model,
    whisper_available,
)
from .media import AudioBuffer, Clipper, FrameBuffer
from . import notes
from .refine import LessonAudio, Refiner
from .helper import Helper, Library
from .report import Reporter
from .telegram import Notifier, TelegramBot

log = logging.getLogger(__name__)


@dataclass
class Stack:
    assistant: Assistant
    audio_sink: str = ""  # куди браузер віддає звук (для вух, системний звук Linux), або ""
    audio_tap: Callable[[str], None] | None = None  # або: сторінка пари віддає звук сюди (вуха через браузер)
    app_audio: object | None = None  # звук Zoom-клієнта на час пари: start() / stop()
    zoom_app_heard: bool = True  # чи чують вуха Zoom-клієнт (інакше в режимі auto Zoom відкривається в браузері)
    frames: FrameBuffer | None = None  # кадри сторінки для відеовирізок, або None
    choose: Callable[[str, list[str]], int | None] | None = None  # мозок обирає зі списку (кнопку входу тощо)
    # Чи можна зараз уточнювати розшифровку (важка робота): за замовчуванням — коли немає пари; програма
    # (app.py) уточнює це розкладом — ще й коли найближча пара не скоро.
    refine_allowed: Callable[[], bool] = lambda: True
    voice_meter: object | None = None  # ears.VoiceMeter, якщо вуха ввімкнені
    schedule_text: Callable[[datetime], str] = lambda now: ""  # розклад текстом (app.py підставляє з Runner)


@contextmanager
def assistant_stack(config: Config, clock: Callable[[], datetime]) -> Iterator[Stack]:
    stoppers: list[Callable[[], None]] = []
    bot = None
    if config.telegram.token and config.telegram.chat_id:
        bot = TelegramBot(config.telegram.token, config.telegram.chat_id)
    elif config.telegram.token:
        log.warning("Telegram: бот не підключений до твого чату — підключи його у вікні програми (вкладка Telegram)")
    notifier = Notifier(bot, desktop=config.telegram.desktop)
    from .updater import take_done

    if done := take_done(config.data_dir):  # щойно оновились (чи відкотились) — сказати, що змінилось
        notifier(done)

    brain = worker = None
    if config.brain.enabled:
        brain = OllamaBrain(config.brain.url, config.brain.model, busy=lambda: not stack.refine_allowed())
        worker = BrainWorker(brain, clock)
        stoppers.append(worker.stop)

    # Мозок підключається, щойно він справді готовий (див. BrainConnector); до того — лише рефлекси.
    assistant = Assistant(config.me, notifier, None, media=notifier)
    listening = bot is not None and config.telegram.commands
    assistant.can_ask = listening
    assistant.alert_mentions = config.telegram.mentions
    if worker:
        worker.assistant = assistant
        worker.start()
    stack = Stack(assistant)
    stack.refine_allowed = lambda: not assistant.meetings
    if brain is not None:
        stack.choose = brain.choose  # запасний вихід для входу на пару: яка кнопка «увійти»

    if listening:
        bot.reply_goes_to_chat = assistant.answers_alert  # ↩️ у чат пари — лише на «тебе кличуть» тощо
        stop = threading.Event()
        listener = threading.Thread(
            target=bot.listen,
            args=(lambda text, is_reply: assistant.on_command(text, clock(), is_reply), stop),
            name="telegram",
            daemon=True,
        )
        listener.start()
        stoppers.append(stop.set)

    audio = None
    clips = bot is not None and config.telegram.clips and config.telegram.mentions
    if clips:
        stack.frames = FrameBuffer()
        audio = AudioBuffer() if config.ears.enabled else None

    ears = None
    lesson_audio = None
    if config.ears.enabled:
        if problem := whisper_available():
            log.warning("Вуха вимкнено: %s (у вікні програми — кнопка «Встановити»)", problem)
        else:
            queue = AudioQueue()  # сюди йде звук пари, звідки б він не брався
            # Звук кожної пари ще й у файл — після пари розпізнаємо його вдруге, точніше (refine.py).
            lesson_audio = LessonAudio(config.data_dir / "audio", clock)
            queue.listeners.append(lesson_audio.feed)
            stack.voice_meter = VoiceMeter()  # сторож вух: коли востаннє була мова (Runner._watch_ears)
            queue.listeners.append(stack.voice_meter)
            stoppers.append(lesson_audio.close)
            stoppers += _audio_sources(config, queue, stack)
            ears = Ears(
                None,
                lambda text, confident: assistant.on_heard(text, clock(), confident),
                queue,
                on_audio=audio.add if audio else None,
                load=lambda: WhisperTranscriber(config.ears.model, config.ears.language, hints=assistant.whisper_hints,
                                                device=config.ears.device),
                on_ready=lambda: setattr(assistant, "ears_on", True),
            )
            ears.start()
            stoppers.append(ears.stop)

    if clips:
        assistant.on_moment = Clipper(stack.frames, audio, notifier, assistant.quote).capture

    # Кожна пара — запис у теку її предмета (завжди); звіт у Telegram — якщо ввімкнено.
    lessons_folder = config.data_dir / notes.FOLDER
    reporter = Reporter(notifier, None, None, lessons_folder, send=config.telegram.reports)
    assistant.on_chunk = reporter.chunk
    assistant.on_finished = reporter.finish
    refine_name = resolve_refine_model(config.ears.refine_model, config.ears.model,
                                       accel.whisper_device(config.ears.device) if ears is not None else None)
    if ears is not None and lesson_audio is not None and refine_name != "off":
        refiner = Refiner(
            config.data_dir / "audio",
            refine_name,
            config.ears.language,
            lambda record: ", ".join(part for part in (config.me.name, record.teacher, record.title) if part),
            reporter.complete,
            reporter.fallback,
            allowed=lambda: stack.refine_allowed(),
            free_gb=available_ram_gb,
            device=config.ears.device,
        )
        reporter.audio, reporter.refiner = lesson_audio, refiner
        assistant.on_started = lesson_audio.start
        refiner.start()
        stoppers.append(refiner.stop)

    if worker:

        helper = Helper(Library(lessons_folder), brain.summarize,
                        schedule=lambda now: stack.schedule_text(now))

        def answer(question: str, context: str, current: str) -> None:
            """Повідомлення в Telegram (окремим потоком): розклад — одразу з даних; про пари — пошук у записах і
            відповідь лише з них, з посиланнями; решта — звичайна розмова (див. helper.py)."""
            now = clock()
            dialog, events = assistant.chat_memory(now)
            try:
                reply = helper.answer(question, now, live=context if current else "", dialog=dialog, events=events)
            except BrainError as e:
                reply = f"не вийшло відповісти: {e}"
            else:
                assistant.remember_reply(reply)
            notifier(reply if reply.startswith(("📅", "Найближчими")) else f"🧠 {reply}")

        def brain_ready() -> None:
            assistant.wake_brain = worker.wake
            assistant.ask_brain = lambda question, context, current: threading.Thread(
                target=answer, args=(question, context, current), name="brain-question", daemon=True
            ).start()
            reporter.brain, reporter.worker = brain, worker
            log.info("Мозок: модель %s", config.brain.model)

        connector = BrainConnector(brain, brain_ready)
        connector.start()
        stoppers.append(connector.stop)
        guard = BrainMemoryGuard(brain, available_ram_gb)  # мало пам'яті — модель мозку геть, поки не знадобиться
        guard.start()
        stoppers.append(guard.stop)
        reporter.after_report = lambda: stack.refine_allowed() and brain.unload()  # пар найближчим часом немає

    try:
        yield stack
    finally:
        for stop_one in reversed(stoppers):
            stop_one()
        notifier.flush()


def _audio_sources(config: Config, queue: AudioQueue, stack: Stack) -> list[Callable[[], None]]:
    """Підключає до черги вух звук пар: у браузері — зі сторінки пари, у Zoom-клієнті — його потоки."""
    stoppers: list[Callable[[], None]] = []
    shared_sink = None
    if pick_source(config.ears.source) == "pulse":
        sink = NullSink(loopback=not config.join.mute_sound)
        try:
            if problem := pulse_available():
                raise EarsError(problem)
            sink.create()
        except EarsError as e:
            sink.close()
            log.warning("Системний звук недоступний (%s) — слухаю пари зі сторінки", e)
        else:
            recorder = PulseRecorder(sink.monitor, queue)
            recorder.start()
            stoppers += [sink.close, recorder.stop]
            stack.audio_sink = sink.name
            shared_sink = sink
    if shared_sink is None:
        stack.audio_tap = queue.feed_base64
    if config.join.zoom_mode != "browser":
        cable = config.ears.app_device or (find_cable() if pulse_available() is not None else "")
        if pulse_available() is None:
            stack.app_audio = PulseAppCapture(queue, hear_too=not config.join.mute_sound, sink=shared_sink)
        elif cable:
            stack.app_audio = DeviceAppCapture(queue, cable)
        else:
            stack.app_audio = NoAppCapture("встанови віртуальний кабель (README → «Вуха») або заходь у Zoom через браузер")
            stack.zoom_app_heard = False
            if config.join.zoom_mode == "auto":
                log.info("Zoom відкриватиму в браузері, щоб вуха його чули (з віртуальним кабелем — у Zoom-клієнті)")
        stoppers.append(stack.app_audio.stop)
    return stoppers
