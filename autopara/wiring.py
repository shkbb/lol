"""Збирає помічника відповідно до конфіга: сповіщення, команди з Telegram, мозок, вуха, вирізки й звіти."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator

from .assistant import Assistant
from .brain import BrainWorker, OllamaBrain
from .config import Config
from .ears import BrowserAudio, Ears, EarsError, NullSink, WhisperTranscriber, pick_source, pulse_available
from .media import AudioBuffer, Clipper, FrameBuffer
from .report import Reporter
from .telegram import Notifier, TelegramBot

log = logging.getLogger(__name__)


@dataclass
class Stack:
    assistant: Assistant
    audio_sink: str = ""  # куди браузер віддає звук (для вух, системний звук Linux), або ""
    audio_tap: Callable[[str], None] | None = None  # або: сторінка пари віддає звук сюди (вуха через браузер)
    frames: FrameBuffer | None = None  # кадри сторінки для відеовирізок, або None


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
        if problem := brain.check():
            log.warning("Мозок: %s. Поки не виправиш — працюватимуть лише рефлекси.", problem)
        worker = BrainWorker(brain, clock)
        stoppers.append(worker.stop)

    assistant = Assistant(config.me, notifier, worker.wake if worker else None, media=notifier)
    listening = bot is not None and config.telegram.commands
    assistant.can_ask = listening
    assistant.alert_mentions = config.telegram.mentions
    if worker:
        worker.assistant = assistant
        worker.start()
        log.info("Мозок: модель %s", config.brain.model)
    stack = Stack(assistant)

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
        try:
            transcriber = WhisperTranscriber(config.ears.model, config.ears.language, hints=assistant.whisper_hints)
        except EarsError as e:
            log.warning("Вуха вимкнено: %s", e)
        else:
            source = pick_source(config.ears.source)
            on_text = lambda text, confident: assistant.on_heard(text, clock(), confident)  # noqa: E731
            ears = None
            if source == "pulse":
                sink = NullSink(loopback=not config.join.mute_sound)
                try:
                    if problem := pulse_available():
                        raise EarsError(problem)
                    sink.create()
                except EarsError as e:
                    sink.close()
                    log.warning("Системний звук недоступний (%s) — слухаю пару через браузер", e)
                    source = "browser"
                else:
                    ears = Ears(transcriber, on_text, sink.monitor, on_audio=audio.add if audio else None)
                    stoppers.append(sink.close)
                    stack.audio_sink = sink.name
            if source == "browser":
                stream = BrowserAudio()
                ears = Ears(transcriber, on_text, stream, on_audio=audio.add if audio else None)
                stack.audio_tap = stream.feed_base64
            ears.start()
            assistant.ears_on = True
            stoppers.append(ears.stop)
            where = "системний звук" if source == "pulse" else "звук зі сторінки пари"
            log.info("Вуха: слухаю пари (модель %s, %s)", config.ears.model, where)

    if clips:
        assistant.on_moment = Clipper(stack.frames, audio, notifier, assistant.quote).capture

    if config.telegram.reports:
        reporter = Reporter(notifier, brain, worker, config.data_dir / "transcripts")
        assistant.on_chunk = reporter.chunk
        assistant.on_finished = reporter.finish

    try:
        yield stack
    finally:
        for stop_one in reversed(stoppers):
            stop_one()
        notifier.flush()
