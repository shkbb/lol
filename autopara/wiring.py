"""Збирає помічника відповідно до конфіга: сповіщення, команди з Telegram, мозок і вуха."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator

from .assistant import Assistant
from .brain import BrainWorker, OllamaBrain
from .config import Config
from .ears import Ears, EarsError, NullSink, WhisperTranscriber, ears_available
from .telegram import Notifier, TelegramBot

log = logging.getLogger(__name__)


@contextmanager
def assistant_stack(config: Config, clock: Callable[[], datetime]) -> Iterator[tuple[Assistant, str]]:
    """Повертає (помічник, назва аудіовиходу для браузера або "")."""
    stoppers: list[Callable[[], None]] = []
    bot = None
    if config.telegram.token and config.telegram.chat_id:
        bot = TelegramBot(config.telegram.token, config.telegram.chat_id)
    elif config.telegram.token:
        log.warning("Telegram: немає chat_id — запусти `autopara telegram`")
    notifier = Notifier(bot)

    worker = None
    if config.brain.enabled:
        brain = OllamaBrain(config.brain.url, config.brain.model)
        if problem := brain.check():
            log.warning("Мозок: %s. Поки не виправиш — працюватимуть лише рефлекси.", problem)
        worker = BrainWorker(brain, clock)
        stoppers.append(worker.stop)

    assistant = Assistant(config.me, notifier, worker.wake if worker else None)
    if worker:
        worker.assistant = assistant
        worker.start()
        log.info("Мозок: модель %s", config.brain.model)

    if bot:
        stop = threading.Event()
        listener = threading.Thread(
            target=bot.listen, args=(lambda text: assistant.on_command(text, clock()), stop), name="telegram", daemon=True
        )
        listener.start()
        stoppers.append(stop.set)

    sink_name = ""
    if config.ears.enabled:
        if problem := ears_available():
            log.warning("Вуха вимкнено: %s", problem)
        else:
            sink = NullSink()
            try:
                sink.create()
                transcriber = WhisperTranscriber(config.ears.model, config.ears.language)
            except EarsError as e:
                sink.close()
                log.warning("Вуха вимкнено: %s", e)
            else:
                ears = Ears(transcriber, lambda text: assistant.on_heard(text, clock()), sink.monitor)
                ears.start()
                stoppers += [sink.close, ears.stop]
                sink_name = sink.name
                log.info("Вуха: слухаю пари (модель %s)", config.ears.model)

    try:
        yield assistant, sink_name
    finally:
        for stop_one in reversed(stoppers):
            stop_one()
        notifier.flush()
