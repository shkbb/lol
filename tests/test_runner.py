from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from autopara.assistant import Assistant, ChatMessage
from autopara.config import Config, JoinSettings, MeSettings, ScheduleSettings
from autopara.leaving import Observation
from autopara.links import LinkRule
from autopara.rozklad import RozkladError, Target
from autopara.runner import Runner
from autopara.session import JoinError, Session

from .conftest import make_lesson

TZ = ZoneInfo("Europe/Kyiv")
MEET = "https://meet.google.com/aaa-bbbb-ccc"
ZOOM = "https://zoom.us/j/12345678901"


def at(hhmm):
    return datetime.fromisoformat(f"2026-10-05T{hhmm}").replace(tzinfo=TZ)


class FakeClient:
    def __init__(self, lessons):
        self.lessons = lessons
        self.fail = False
        self.calls = 0

    def find_group(self, name, faculty_id=None, course=None):
        return Target(2, 3, 22, group_name=name)

    def schedule(self, target, start, end):
        self.calls += 1
        if self.fail:
            raise RozkladError("сайт лежить")
        return [l for l in self.lessons if start <= l.day <= end]


class FakeJoiner:
    def __init__(self):
        self.joined, self.left = [], []
        self.errors = []
        self.seen = Observation()
        self.chat, self.written = [], []

    def observe(self, session):
        return self.seen

    def read_chat(self, session):
        return list(self.chat)

    def send_chat(self, session, text):
        self.written.append(text)
        return True

    def screenshot(self, session):
        return b"jpeg"

    def join(self, visit):
        if self.errors:
            raise self.errors.pop(0)
        self.joined.append(visit.title)
        return Session(visit, visit.leave_at, page="page")

    def alive(self, session):
        return True

    def leave(self, session):
        self.left.append(session.visit.title)


class FakeGuard:
    holding = False

    def hold(self, needed):
        self.holding = needed

    def release(self):
        self.holding = False


def make_runner(lessons, **join):
    config = Config(
        schedule=ScheduleSettings(group="ТЦР-32"),
        join=JoinSettings(**{"minutes_before": 2, "smart_leave": False, **join}),
        links=[LinkRule(subject="Бази", url=MEET), LinkRule(subject="Мережі", url=ZOOM)],
    )
    client, joiner = FakeClient(lessons), FakeJoiner()
    return Runner(config, client, joiner, sleep_guard=FakeGuard()), client, joiner


def test_joins_once_on_time_and_leaves_after():
    runner, _, joiner = make_runner([make_lesson()])
    runner.tick(at("09:00"))
    assert joiner.joined == []
    runner.tick(at("09:28"))
    runner.tick(at("09:29"))
    assert joiner.joined == ["Бази даних"]
    runner.tick(at("10:49"))
    assert joiner.left == []
    runner.tick(at("10:50"))
    assert joiner.left == ["Бази даних"]


def test_starting_mid_lesson_joins_immediately_but_not_at_the_very_end():
    runner, _, joiner = make_runner([make_lesson()])
    runner.tick(at("10:00"))
    assert joiner.joined == ["Бази даних"]
    late_runner, _, late_joiner = make_runner([make_lesson()])
    late_runner.tick(at("10:47"))
    assert late_joiner.joined == []


def test_failed_join_is_retried_then_given_up():
    runner, _, joiner = make_runner([make_lesson()])
    joiner.errors = [JoinError("мережа"), JoinError("мережа"), JoinError("мережа")]
    runner.tick(at("09:28"))
    runner.tick(at("09:29"))  # ще рано для повтору
    runner.tick(at("09:30"))
    runner.tick(at("09:32"))
    runner.tick(at("09:40"))
    assert joiner.joined == [] and joiner.errors == []
    assert runner.attempts[runner.plan.visits[0].key] == 3


def test_fatal_join_error_is_not_retried():
    runner, _, joiner = make_runner([make_lesson()])
    joiner.errors = [JoinError("потрібно увійти", retry=False), JoinError("не має дійти")]
    runner.tick(at("09:28"))
    runner.tick(at("09:35"))
    assert len(joiner.errors) == 1


def test_schedule_errors_do_not_stop_the_loop():
    runner, client, joiner = make_runner([make_lesson()])
    client.fail = True
    runner.tick(at("09:00"))
    runner.tick(at("09:01"))
    assert client.calls == 1  # наступна спроба — через 5 хв
    client.fail = False
    runner.tick(at("09:05"))
    runner.tick(at("09:28"))
    assert joiner.joined == ["Бази даних"]


def test_replanning_mid_call_does_not_rejoin():
    lessons = [make_lesson(), make_lesson(start="11:10", end="12:30", type="Пз")]
    runner, _, joiner = make_runner(lessons)
    runner.tick(at("09:28"))
    # Розклад оновився вже після першої пари — друга виглядає як окремий візит на ту саму зустріч.
    runner.next_refresh = None
    runner.tick(at("11:09"))
    runner.tick(at("12:29"))
    assert joiner.joined == ["Бази даних + Бази даних (Пз)"]
    assert joiner.left == []
    runner.tick(at("12:30"))
    assert len(joiner.left) == 1


def test_open_meeting_is_reused_for_the_next_pair():
    # Без auto_leave вкладка лишається відкритою — наступна пара в тій самій зустрічі не відкриває другу.
    lessons = [make_lesson(), make_lesson(start="14:30", end="15:50", type="Пз")]
    runner, _, joiner = make_runner(lessons)
    runner.config.join.auto_leave = False
    runner.tick(at("09:28"))
    runner.tick(at("14:28"))
    assert joiner.joined == ["Бази даних"]
    assert [s.leave_at for s in runner.active.values()] == [at("15:50")]


# --- Розумний вихід ----------------------------------------------------------


def test_smart_leave_leaves_with_the_crowd_when_pair_ends_early():
    runner, _, joiner = make_runner([make_lesson()], smart_leave=True)
    joiner.seen = Observation(participants=2)
    runner.tick(at("09:28"))
    joiner.seen = Observation(participants=25)
    runner.tick(at("09:45"))
    # Пару відпустили о 10:15 — люди виходять, а за розкладом ще 35 хвилин.
    joiner.seen = Observation(participants=6)
    runner.tick(at("10:15"))
    assert joiner.left == []  # не першими: спершу невелика пауза
    runner.tick(at("10:16"))
    assert joiner.left == ["Бази даних"]


def test_smart_leave_stays_while_pair_runs_over():
    runner, _, joiner = make_runner([make_lesson()], smart_leave=True)
    joiner.seen = Observation(participants=25)
    runner.tick(at("09:28"))
    runner.tick(at("10:58"))  # за розкладом пара скінчилась о 10:50, але всі ще тут
    assert joiner.left == []
    joiner.seen = Observation(participants=10)
    runner.tick(at("11:02"))
    runner.tick(at("11:03"))
    assert joiner.left == ["Бази даних"]


def test_next_pair_with_other_link_closes_the_previous_one():
    lessons = [make_lesson(), make_lesson(subject="Мережі", start="11:10", end="12:30")]
    runner, _, joiner = make_runner(lessons, smart_leave=True)
    joiner.seen = Observation(participants=25)
    runner.tick(at("09:28"))
    runner.tick(at("11:08"))  # Бази даних затягнулись, а вже треба на Мережі
    assert joiner.left == ["Бази даних"]
    assert joiner.joined == ["Бази даних", "Мережі"]


def test_same_meeting_next_pair_restarts_the_decision():
    lessons = [make_lesson(), make_lesson(start="11:10", end="12:30", type="Пз")]
    runner, _, joiner = make_runner(lessons, smart_leave=True)
    joiner.seen = Observation(participants=25)
    runner.tick(at("09:28"))
    runner.tick(at("11:08"))  # на перерві ніхто не вийшов — лишаємось, а не заходимо вдруге
    assert joiner.joined == ["Бази даних"] and joiner.left == []
    [session] = runner.active.values()
    assert session.decider.end == at("12:30")


def test_sleep_is_blocked_from_a_day_before_a_lesson_until_it_ends():
    runner, _, joiner = make_runner([make_lesson()])
    runner.tick(at("07:00") - timedelta(days=1, hours=1))  # пара аж за добу з годиною — спати можна
    assert not runner.sleep_guard.holding
    runner.tick(at("07:00") - timedelta(hours=20))  # уже ввечері напередодні — ноутбук не засне вночі
    assert runner.sleep_guard.holding
    runner.tick(at("09:28"))
    runner.tick(at("10:50"))
    assert not runner.sleep_guard.holding


# --- Помічник ----------------------------------------------------------------


def make_assisted_runner(lessons):
    runner, client, joiner = make_runner(lessons)
    inbox = []
    runner.assistant = Assistant(MeSettings(name="Ляшко Денис"), lambda text, urgent=False: inbox.append(text))
    return runner, joiner, inbox


def test_assistant_mirrors_plus_through_the_browser():
    runner, joiner, inbox = make_assisted_runner([make_lesson()])
    joiner.chat = [ChatMessage("0", "Іван Коваль", "Всім привіт")]
    runner.tick(at("09:28"))
    assert inbox == ["✅ Зайшов на «Бази даних», мікрофон і камера вимкнені"]
    joiner.chat += [ChatMessage("t", "Петренко П.П.", "Перекличка, поставте +")]
    joiner.chat += [ChatMessage(str(i), name, "+") for i, name in enumerate(["Іван Коваль", "Олена Бойко", "Марко Лис"], 1)]
    runner.tick(at("10:00"))
    assert joiner.written == ["+"]
    assert inbox[-1] == (
        "💬 Написав «+» у чат «Бази даних» "
        "(Група ставить «+» у чат — це перекличка: «💬 Петренко П.П.: Перекличка, поставте +»)"
    )


def test_leave_command_from_telegram():
    runner, joiner, inbox = make_assisted_runner([make_lesson()])
    runner.tick(at("09:28"))
    assert runner.assistant.on_command("вийди", at("09:40")) == "Виходжу."
    runner.tick(at("09:40"))
    assert joiner.left == ["Бази даних"]
    assert inbox[-1] == "👋 Вийшов з «Бази даних»: ти попросив у Telegram"
    runner.tick(at("09:45"))
    assert joiner.joined == ["Бази даних"]  # назад не заходимо


def test_failed_join_is_reported_with_the_link():
    runner, joiner, inbox = make_assisted_runner([make_lesson()])
    joiner.errors = [JoinError("потрібно увійти в Google-акаунт", retry=False)]
    runner.tick(at("09:28"))
    assert inbox == [f"❌ Не вдалося зайти на «Бази даних»: потрібно увійти в Google-акаунт. Зайди сам: {MEET}"]


def test_screenshot_reaches_telegram():
    runner, joiner, inbox = make_assisted_runner([make_lesson()])
    photos = []

    class Media:
        def photo(self, content, caption):
            photos.append((content, caption))

    runner.assistant.media = Media()
    runner.tick(at("09:28"))
    runner.assistant.on_command("екран", at("09:40"))
    runner.tick(at("09:40"))
    assert photos == [(b"jpeg", "🖥 «Бази даних», 09:40")]


def test_classmates_are_loaded_for_voice_roll_calls():
    runner, client, joiner = make_runner([make_lesson()])
    runner.assistant = Assistant(MeSettings(name="Ляшко Денис"), lambda text, urgent=False: None)
    client.students = lambda faculty, course, group: [
        type("S", (), {"name": n})() for n in ("Бойко Олена", "Коваль Іван", "Ляшко Денис")
    ]
    runner.tick(at("07:00"))
    assert len(runner.assistant._classmates) == 2  # себе не рахуємо


def test_join_and_leave_messages_can_be_switched_off():
    runner, joiner, inbox = make_assisted_runner([make_lesson()])
    runner.config.telegram.joins = False
    runner.tick(at("09:28"))
    runner.tick(at("10:50"))
    assert joiner.left == ["Бази даних"] and inbox == []
    joiner.errors = [JoinError("потрібно увійти в Google-акаунт", retry=False)]
    runner.plan = None
    runner.finished.clear()
    runner.next_refresh = None
    runner.tick(at("09:28"))
    assert inbox and inbox[-1].startswith("❌")  # невдача — повідомляємо завжди


class FakeAppAudio:
    def __init__(self):
        self.log = []

    def start(self):
        self.log.append("start")

    def stop(self):
        self.log.append("stop")


def test_zoom_client_class_is_heard_and_reported():
    """Пара в Zoom-клієнті: вуха слухають саме його, помічник стежить, а писати в чат не береться."""
    runner, joiner, inbox = make_assisted_runner([make_lesson(subject="Мережі")])
    joiner.join = lambda visit: Session(visit, visit.leave_at)  # Zoom-клієнт: вкладки браузера немає
    runner.app_audio = audio = FakeAppAudio()
    finished = []
    runner.assistant.on_finished = finished.append
    runner.tick(at("09:28"))
    assert audio.log == ["start"]
    meeting = runner.assistant.current()
    assert meeting is not None and meeting.can_chat is False
    runner.assistant.on_heard("Ляшко, ви тут?", at("09:40"))
    assert any("напиши сам: «Так, я тут»" in text for text in inbox)
    assert joiner.written == []
    runner.tick(at("10:51"))
    assert audio.log == ["start", "stop"]
    [record] = finished
    assert record.transcript and "Ляшко, ви тут?" in record.transcript[0][1]  # у звіт розшифровка потрапила


def test_chat_actions_happen_at_once_not_on_the_next_tick():
    """«напиши …» з Telegram (і «+» на перекличці) — одразу, а не через 20 с на наступному кроці."""
    runner, joiner, inbox = make_assisted_runner([make_lesson()])
    runner.tick(at("09:28"))
    runner.assistant.on_command("напиши тест", at("09:40"))
    runner.act_now(at("09:40"))  # так робить програма кожні пів секунди між кроками
    assert joiner.written == ["тест"]


def test_restart_does_not_rejoin_a_lesson_we_already_left(tmp_path):
    """Наживо: після перезапуску програма знову зайшла на пару, яка вже завершилась (зустріч закрили раніше)."""
    visited = tmp_path / "visited.json"
    runner, _, joiner = make_runner([make_lesson()])
    runner.visited_file = visited
    runner.tick(at("09:28"))
    runner._leave(next(iter(runner.active)), "зустріч завершилась")
    again, _, joiner2 = make_runner([make_lesson()])
    again.visited_file = visited
    again.finished = again._load_visited()
    again.tick(at("10:00"))
    assert joiner2.joined == []  # пара ще «йде» за розкладом, але з неї вже вийшли

    # А якщо програму закрили посеред пари — після запуску заходимо знову.
    runner3, _, joiner3 = make_runner([make_lesson(start="11:10", end="12:30")])
    runner3.visited_file = tmp_path / "other.json"
    runner3.tick(at("11:08"))
    runner3.finish_all()
    runner4, _, joiner4 = make_runner([make_lesson(start="11:10", end="12:30")])
    runner4.visited_file = tmp_path / "other.json"
    runner4.finished = runner4._load_visited()
    runner4.tick(at("11:20"))
    assert joiner4.joined == ["Бази даних"]


def test_today_note_tells_the_brain_what_was_and_what_is_next():
    runner, _, joiner = make_runner([make_lesson(), make_lesson(start="11:10", end="12:30", subject="Мережі")])
    runner.tick(at("09:28"))
    note = runner.today_note(at("10:00"))
    assert "09:30–10:50 «Бази даних» — іде зараз" in note and "11:10–12:30 «Мережі» — ще буде" in note
    runner._leave(next(iter(runner.active)), "пара скінчилась")
    assert "«Бази даних» — уже була" in runner.today_note(at("10:55"))


def test_schedule_site_down_after_restart_uses_the_saved_schedule(tmp_path):
    """Наживо: після перезапуску сайт розкладу не відповідав (DNS) — і програма не знала, куди заходити."""
    cache = tmp_path / "schedule-cache.json"
    runner, _, _ = make_runner([make_lesson()])
    runner.schedule_cache = cache
    runner.tick(at("08:00"))
    assert cache.exists()

    again, client, joiner = make_runner([make_lesson()])
    again.schedule_cache = cache
    client.fail = True  # сайт лежить
    again.tick(at("09:28"))
    assert joiner.joined == ["Бази даних"] and again.last_error

    other, client2, joiner2 = make_runner([make_lesson()])
    other.config.schedule.group = "ІП-31"  # розклад іншої групи не беремо
    other.schedule_cache = cache
    client2.fail = True
    other.tick(at("09:28"))
    assert joiner2.joined == []


def test_joined_by_link_then_the_scheduled_lesson_takes_over_the_title():
    """Наживо: зайшли за посиланням («Тестовий вхід»), потім ожив розклад — скріншоти мали б назву пари."""
    from autopara.planner import Visit
    from autopara.rozklad import Lesson as RLesson

    runner, _, joiner = make_runner([make_lesson()])
    now = at("09:20")
    test = Visit(MEET, "meet", [RLesson(now.date(), now.time(), at("10:20").time(), "Тестовий вхід")], now, at("10:20"))
    runner.extra_visits.append(test)
    runner.tick(now)
    runner.tick(at("09:28"))  # пара з розкладу — та сама зустріч
    [session] = runner.active.values()
    assert session.title == "Бази даних" and joiner.joined == ["Тестовий вхід"]
