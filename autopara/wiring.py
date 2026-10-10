"""Збирає помічника відповідно до конфіга: сповіщення, команди з Telegram, мозок, вуха, вирізки й звіти."""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterator

from . import accel, health
from .assistant import Assistant
from .brain import BrainConnector, BrainError, BrainMemoryGuard, BrainWorker, ChatBrain, OllamaBrain
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
    voice_pcm,
    whisper_available,
)
from .media import AudioBuffer, Clipper, FrameBuffer
from . import notes
from .refine import LessonAudio, Refiner
from .helper import Helper, Library, subject_terms
from .weekly import WeeklyDigest
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
    media: Callable[[object], str | None] | None = None  # голосове чи фото з Telegram (telegram.Incoming)
    helper: object | None = None  # помічник з навчання (helper.Helper) — відповіді в Telegram


BIG_CHAT_FREE_GB = 9.0  # стільки вільної пам'яті (без швидкої моделі) — і поза парами відповідає розумна модель


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
        # [ears] device = "cpu" — відеокарту не використовувати взагалі: і для вух, і для мовної моделі.
        no_gpu = False if config.ears.device == "cpu" else None
        models_lock = threading.RLock()  # швидка й розумна моделі — по черзі (див. ChatBrain)
        brain = OllamaBrain(config.brain.url, config.brain.model, busy=lambda: not stack.refine_allowed(), gpu=no_gpu,
                            lock=models_lock)
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
            args=(lambda text, is_reply: assistant.on_command(text, clock(), is_reply), stop,
                  lambda incoming: stack.media(incoming) if stack.media else "Голосові й фото я ще не вмію — напиши текстом."),
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
    def refine_hint(record) -> str:
        """Підказка для уточнення після пари: прізвище, викладач, пара · терміни предмета (див. ears.split_hint)."""
        names = ", ".join(part for part in (config.me.name.split()[:1] + [record.teacher, record.title]) if part)
        try:
            terms = assistant.terms_for(record.title) if assistant.terms_for else []
        except Exception:  # noqa: BLE001 — без словника однаково розпізнаємо
            terms = []
        return names + (" · " + ", ".join(terms) if terms else "")

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
            lambda record: refine_hint(record),
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

    # Помічник з навчання: розклад, «що було на парі», завдання — з даних, одразу й навіть без мовної моделі;
    # пошук у записах і розмова — коли модель готова (helper.ask). Див. helper.py.
    helper = Helper(Library(lessons_folder), None, schedule=lambda now: stack.schedule_text(now))

    asking = threading.local()  # на яке повідомлення відповідаємо (голосове розшифровується в іншому потоці)

    def reply_to() -> int:
        explicit = getattr(asking, "message_id", 0)
        current = bot.current if bot is not None else None
        return explicit or (current.message_id if current is not None else 0)

    def answer(question: str, context: str, current: str, to: int) -> None:
        """Відповідь, якій потрібна мовна модель (окремим потоком — це до хвилини), — реплаєм на питання."""
        now = clock()
        dialog, events = assistant.chat_memory(now)
        try:
            reply = helper.answer(question, now, live=context if current else "", dialog=dialog, events=events)
        except BrainError as e:
            reply = f"не вийшло відповісти: {e}"
        else:
            assistant.remember_reply(reply)
        notifier.reply(reply if reply.startswith(("📅", "📖", "📝", "👤", "У записах")) else f"🧠 {reply}", to)

    def ask(question: str, context: str, current: str) -> str | None:
        """Питання з Telegram: відповідь з даних — одразу (рядок); інакше 👀 на питання, а відповідь — реплаєм
        пізніше ("" — нічого не писати зараз)."""
        log.info("❓ %s", question[:200])
        try:
            ready = helper.quick(question, clock())
        except Exception as e:  # noqa: BLE001 — зіпсований запис пари не має ламати розмову
            log.warning("Помічник: %s", e)
            ready = None
        if ready is not None:
            assistant.remember_reply(ready)
            return ready
        if helper.ask is None:
            return "🧠 Мовна модель ще не готова (Ollama) — на таке відповім, щойно вона запуститься. Розклад, завдання й «що було на парі» — питай уже."
        to = reply_to()
        notifier.react(to)
        threading.Thread(target=answer, args=(question, context, current, to), name="brain-question", daemon=True).start()
        return ""

    def media(incoming) -> str | None:
        """Голосове — розшифрувати (поза парами) й відповісти як на текст; фото — показати мовній моделі."""
        if incoming.voice:
            if not stack.refine_allowed():
                return "🎧 Я зараз на парі — вуха зайняті нею. Напиши, будь ласка, текстом."
            transcriber = ears.transcribe if ears is not None else None
            if transcriber is None:
                return "Розпізнавання мови вимкнене чи ще вантажиться — напиши, будь ласка, текстом."
            notifier.react(incoming.message_id)
            threading.Thread(target=hear_voice, args=(incoming, transcriber), name="voice-question", daemon=True).start()
            return ""
        if helper.see is None:
            return "Мовна модель ще не готова (Ollama) — фото подивлюся, щойно вона запуститься."
        notifier.react(incoming.message_id)
        threading.Thread(target=see_photo, args=(incoming,), name="photo-question", daemon=True).start()
        return ""

    def hear_voice(incoming, transcriber) -> None:
        to = incoming.message_id
        try:
            pcm = voice_pcm(bot.download(incoming.voice))
            # Голосове коротке, а пари зараз немає — тож уважніше, ніж наживо (beam 5): так точніше за секунди.
            text = " ".join(line for _, line in transcriber.transcribe_long(pcm, beam_size=5)).strip()
        except Exception as e:  # noqa: BLE001 — не вийшло розшифрувати: так і сказати
            log.warning("Голосове: %s", e)
            notifier.reply("Не вийшло розшифрувати голосове — напиши, будь ласка, текстом.", to)
            return
        if not text:
            notifier.reply("Не розчув — спробуй ще раз або напиши текстом.", to)
            return
        notifier.reply(f"🎤 «{text}»", to)
        asking.message_id = to
        try:
            reply = assistant.on_command(text, clock(), False)
        finally:
            asking.message_id = 0
        if reply:
            notifier.reply(reply, to)

    def see_photo(incoming) -> None:
        to = incoming.message_id
        now = clock()
        dialog, events = assistant.chat_memory(now)
        try:
            reply = helper.look(incoming.text, bot.download(incoming.photo), now, dialog=dialog, events=events)
        except Exception as e:  # noqa: BLE001
            log.warning("Фото: %s", e)
            reply = "Не вийшло подивитися фото — надішли, будь ласка, ще раз."
        assistant.remember_reply(reply)
        notifier.reply(f"🧠 {reply}", to)

    if listening:
        assistant.ask_brain = ask
        stack.media = media

    chat_brain = None
    if brain is not None:
        big = None
        if config.brain.chat_model and config.brain.chat_model != config.brain.model:
            big = OllamaBrain(config.brain.url, config.brain.chat_model, timeout=600, busy=lambda: not stack.refine_allowed(),
                              gpu=False if config.ears.device == "cpu" else None, lock=models_lock)
            big.keep_alive = "60s"  # звіт — десяток шматків поспіль; далі сама геть з пам'яті (і швидку не пустить разом)
            big.think = False  # Qwen за замовчуванням «думає вголос» — це ще хвилини; відповідь і без того добра
        chat_brain = ChatBrain(brain, big, quiet=lambda: stack.refine_allowed(), free_gb=available_ram_gb,
                               need_gb=BIG_CHAT_FREE_GB, lock=models_lock)
        if big is not None:
            big_connector = BrainConnector(big, lambda: (setattr(chat_brain, "big_ready", True),
                                                         log.info("Мозок для розмов: %s", big.model)))
            big_connector.start()
            stoppers.append(big_connector.stop)

    def about_me(now: datetime) -> str:
        """Картка «Про тебе» для мовної моделі — лише правда з налаштувань і стану програми (див. helper.ABOUT_ME_RE)."""
        from . import __version__
        from .helper import next_lesson

        transcriber = ears.transcribe if ears is not None else None
        if transcriber is not None:
            device = getattr(getattr(transcriber, "device", None), "label", "процесор")
            ears_text = f"слухаєш пари: розпізнавання мови {getattr(transcriber, 'name', 'Whisper')} на {device}"
        elif ears is not None:
            ears_text = "розпізнавання мови ще вантажиться"
        else:
            ears_text = "розпізнавання мови вимкнене в налаштуваннях — пари не слухаєш"
        chat = {"off": "У чат пари сам не пишеш ніколи — лише коли студент скаже («+», «напиши …», кнопки під сповіщенням)",
                "ask": "У чат пари пишеш лише після «так» від студента",
                "auto": "На перекличці сам ставиш «+» чи «Так, я тут», коли впевнений; решту — лише за командою студента"}
        meeting = assistant.current()
        lines = [
            f"Про тебе (програма autopara {__version__}, група {config.schedule.group or 'не вибрана'}):",
            "• Сам заходиш на онлайн-пари за розкладом (Meet, Zoom); мікрофон і камера завжди вимкнені.",
            f"• Зараз {('на парі «' + meeting.title + '»') if meeting else 'не на парі'}; {ears_text}.",
            "• Під час пари сповіщаєш лише по ділу, дослівною цитатою: 🚨 студента кличуть (прізвище чи звертання на "
            "ім'я), 📋 перекличка, 📌 оголосили контрольну, дедлайн чи увімкнути камери.",
            "• " + chat.get(config.me.chat_reply, chat["off"]) + ".",
            ("• Після пари ще раз уважно розшифровуєш запис (" + refine_name + "), коли пар найближчим часом немає, і "
             "тоді надсилаєш звіт — тому він приходить пізніше." if refine_name != "off" and ears is not None else
             "• Після пари одразу надсилаєш звіт."),
            "• Записи пар (підсумок, розшифровка, чат) зберігаються на ноутбуці студента: ~/.autopara/Пари.",
            "• Розклад, «що було на парі», завдання, дедлайни, викладачів — відповідаєш одразу з даних; решту думає мовна "
            f"модель: під час пар — {config.brain.model} (~30–60 с)"
            + (f", поза парами, коли вистачає пам'яті, — {config.brain.chat_model} (розумний режим, 1–3 хв)"
               if config.brain.chat_model and chat_brain is not None and chat_brain.big is not None else "")
            + ("" if helper.ask is not None else "; зараз мовна модель ще не готова (Ollama)") + ".",
            "• Голосові розшифровуєш, коли немає пари (на парі просиш текстом); фото розглядаєш (кілька хвилин).",
            "• Працюєш локально на ноутбуці студента, без інтернету; відеокарта — "
            + ("не використовується (так у налаштуваннях)" if config.ears.device == "cpu" else "лише якщо вона допомагає (NVIDIA)") + ".",
            "• Налаштування — у вікні програми autopara: «Налаштування».",
        ]
        try:
            if upcoming := next_lesson(stack.schedule_text(now), now):
                lines.append(f"• Найближча пара: {upcoming}.")
        except Exception:  # noqa: BLE001
            pass
        return "\n".join(lines)

    helper.about = about_me
    stack.helper = helper

    def terms_for(title: str) -> list[str]:
        """Словник предмета з його записів (helper.subject_terms) — для Whisper наживо й після пари."""
        subject = notes.split_title(title)[0]
        helper.library.refresh()
        return subject_terms([doc for doc in helper.library.docs if doc.subject == subject])

    assistant.terms_for = terms_for

    if bot is not None and config.telegram.weekly:
        def upcoming(now: datetime) -> str:
            try:
                return stack.schedule_text(now, days=6)
            except TypeError:  # розклад без параметра днів
                return stack.schedule_text(now)

        weekly = WeeklyDigest(helper.library, notifier, clock, upcoming, config.data_dir / "weekly.json")
        weekly.start()
        stoppers.append(weekly.stop)

    # Сторож: мовчазні проблеми (диск, Ollama, застрягле уточнення, старий розклад) — у Telegram, з порадою.
    checks = [health.disk_check(config.data_dir), health.refine_check(config.data_dir / "audio"),
              health.schedule_check(config.data_dir / "schedule-cache.json")]
    if brain is not None:
        checks.append(health.ollama_check(brain.reachable))
    guard_health = health.Health(notifier, checks)
    guard_health.start()
    stoppers.append(guard_health.stop)

    if worker:

        def brain_ready() -> None:
            assistant.wake_brain = worker.wake
            helper.ask = lambda system, text: chat_brain.summarize(system, text, num_predict=900)
            helper.see = lambda system, text, image: brain.summarize(system, text, num_predict=700, images=[image])
            reporter.brain, reporter.worker = brain, worker
            reporter.writer = chat_brain  # підсумок після пари — розумною моделлю, коли пари поруч немає й є пам'ять
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
