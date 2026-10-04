"""Збирає помічника відповідно до конфіга: сповіщення, команди з Telegram, мозок, вуха, вирізки й звіти."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator

from .assistant import Assistant
from .brain import BrainConnector, BrainWorker, OllamaBrain
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
    WhisperTranscriber,
    find_cable,
    pick_source,
    pulse_available,
    whisper_available,
)
from .media import AudioBuffer, Clipper, FrameBuffer
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


@contextmanager
def assistant_stack(config: Config, clock: Callable[[], datetime]) -> Iterator[Stack]:
    stoppers: list[Callable[[], None]] = []
    bot = None
    if config.telegram.token and config.telegram.chat_id:
        bot = TelegramBot(config.telegram.token, config.telegram.chat_id)
    elif config.telegram.token:
        log.warning("Telegram: бот не підключений до твого чату — підключи його у вікні програми (вкладка Telegram)")
    notifier = Notifier(bot, desktop=config.telegram.desktop)

    brain = worker = None
    if config.brain.enabled:
        brain = OllamaBrain(config.brain.url, config.brain.model)
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
    if brain is not None:
        stack.choose = brain.choose  # запасний вихід для входу на пару: яка кнопка «увійти»

    if listening:
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

    if config.ears.enabled:
        if problem := whisper_available():
            log.warning("Вуха вимкнено: %s (у вікні програми — кнопка «Встановити»)", problem)
        else:
            queue = AudioQueue()  # сюди йде звук пари, звідки б він не брався
            stoppers += _audio_sources(config, queue, stack)
            ears = Ears(
                None,
                lambda text, confident: assistant.on_heard(text, clock(), confident),
                queue,
                on_audio=audio.add if audio else None,
                load=lambda: WhisperTranscriber(config.ears.model, config.ears.language, hints=assistant.whisper_hints),
                on_ready=lambda: setattr(assistant, "ears_on", True),
            )
            ears.start()
            stoppers.append(ears.stop)

    if clips:
        assistant.on_moment = Clipper(stack.frames, audio, notifier, assistant.quote).capture

    reporter = None
    if config.telegram.reports:
        reporter = Reporter(notifier, None, None, config.data_dir / "transcripts")
        assistant.on_chunk = reporter.chunk
        assistant.on_finished = reporter.finish

    if worker:

        def brain_ready() -> None:
            assistant.wake_brain = worker.wake
            if reporter is not None:
                reporter.brain, reporter.worker = brain, worker
            log.info("Мозок: модель %s", config.brain.model)

        connector = BrainConnector(brain, brain_ready)
        connector.start()
        stoppers.append(connector.stop)

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
