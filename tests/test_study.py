"""Навчання: від завдання в Classroom до здачі — лише після «так», і лише повністю готове."""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from autopara.agent import AgentError, AgentResult
from autopara.assistant import ChatMessage, LessonRecord
from autopara.classroom import Assignment, Attachment, ClassroomError, SubmitResult
from autopara.config import Config, MeSettings, ScheduleSettings, StudySettings
from autopara.study import StudyManager, due_of, match_subject, short_name

TZ = ZoneInfo("Europe/Kyiv")
NOW = datetime(2026, 10, 2, 16, 0, tzinfo=TZ)


class FakeAgent:
    """Агент, що «виконує» роботу: пише звіт у out/, RESULT.md, DEFENSE.md і вердикт перевірки."""

    def __init__(self, verdict="STATUS: COMPLETE"):
        self.verdict = verdict
        self.calls: list[tuple[str, Path]] = []
        self.unavailable = ""

    def problem(self):
        return self.unavailable or None

    def used_today(self):
        return len(self.calls)

    def cancel(self):
        pass

    def run(self, instruction, workdir, timeout_minutes=None):
        if self.unavailable:
            raise AgentError(self.unavailable)
        workdir = Path(workdir)
        self.calls.append((instruction, workdir))
        if "LECTURE.md" in instruction:
            (workdir / "SUMMARY.md").write_text("Тема: нормалізація. Викладач: звіти лише в PDF.", encoding="utf-8")
            return AgentResult(True, "ok")
        if "перевіряльник" in instruction:
            with (workdir / "RESULT.md").open("a", encoding="utf-8") as f:
                f.write("\n" + self.verdict + "\n")
            return AgentResult(True, self.verdict)
        task = (workdir / "TASK.md").read_text(encoding="utf-8")
        (workdir / "out" / "Ляшко_Д.О._ПР1.2.pdf").write_bytes(b"%PDF-1.4 " + task.encode("utf-8")[:50])
        (workdir / "RESULT.md").write_text("Зроблено 10 запитів, скріншоти в звіті.", encoding="utf-8")
        (workdir / "DEFENSE.md").write_text("Питання: що таке JOIN?", encoding="utf-8")
        return AgentResult(True, "готово")


class FakeClassroom:
    def __init__(self, items):
        self.items = items
        self.details_text = {}
        self.submitted: list[tuple[str, list[str], bool]] = []
        self.unsubmitted: list[str] = []
        self.todo_calls = 0
        self.error = None

    def todo(self):
        self.todo_calls += 1
        if self.error:
            raise self.error
        return [Assignment(i.course_id, i.item_id, i.title, i.course, i.listing) for i in self.items]

    def cookies(self):
        return []

    def details(self, item):
        item.text = self.details_text.get(item.key, "Виконайте запити згідно варіанту.")
        if "Тест" in item.title:
            item.attachments = [Attachment("https://forms.gle/x", "Тест", "forms")]
        return item

    def submit(self, item, files, dry_run=False):
        self.submitted.append((item.key, [f.name for f in files], dry_run))
        return SubmitResult(True, "пробна здача" if dry_run else "здано", b"png")

    def unsubmit(self, item):
        self.unsubmitted.append(item.key)
        return SubmitResult(True, "здачу скасовано", b"png")


class FakeNotifier:
    def __init__(self):
        self.texts: list[str] = []
        self.asks: list[tuple[int, str, list]] = []
        self.documents: list[tuple[str, str]] = []
        self.photos: list[str] = []
        self._next_id = 100

    def __call__(self, text, urgent=False):
        self.texts.append(text)

    def _sent(self, on_sent):
        self._next_id += 1
        if on_sent:
            on_sent(self._next_id)
        return self._next_id

    def ask(self, text, buttons, urgent=True, on_sent=None):
        message_id = self._sent(on_sent)
        self.asks.append((message_id, text, buttons))
        if not buttons:
            self.texts.append(text)

    def photo(self, content, caption="", on_sent=None):
        self._sent(on_sent)
        self.photos.append(caption)

    def document(self, content, filename, caption="", on_sent=None):
        self._sent(on_sent)
        self.documents.append((filename, caption))

    def all_text(self):
        return "\n".join(text for _, text, _ in self.asks)


def make(tmp_path, items=(), verdict="STATUS: COMPLETE", clock=lambda: NOW):
    config = Config(
        schedule=ScheduleSettings(group="ТЦР-32"),
        me=MeSettings(name="Денис Ляшко"),
        study=StudySettings(enabled=True, full_name="Ляшко Денис Олегович"),
    )
    config.agent.attempts = 2
    agent, notifier = FakeAgent(verdict), FakeNotifier()
    manager = StudyManager(config, agent, notifier, clock, tmp_path / "study")
    manager.classroom = FakeClassroom(list(items))
    return manager, agent, notifier, manager.classroom


def run_jobs(manager):
    """Те, що робить фоновий потік, — тут синхронно."""
    while not manager._jobs.empty():
        job = manager._jobs.get()
        if job is None:
            continue
        kind, payload = job
        if kind == "work":
            manager._work(payload)
        else:
            manager._lecture(*payload)


def ticks(manager, count, start=NOW):
    for i in range(count):
        manager.browser_tick(start + timedelta(minutes=i))


LAB = Assignment("C1", "A1", "ПР № 1.2 Запити вибірки", "2026 Бази даних", "ПР № 1.2 · 2026 Бази даних · До 9 жовт.")
QUIZ = Assignment("C2", "A2", "Тест з теми 2", "Безпека ПЗ", "Тест з теми 2 · Безпека ПЗ · До 3 жовт.")


def test_full_path_only_after_yes_with_dry_run_first(tmp_path):
    manager, agent, notifier, classroom = make(tmp_path, [LAB, QUIZ])
    ticks(manager, 3)  # перевірка списку, потім по одній сторінці завдання
    lab, quiz = manager.task(1), manager.task(2)
    assert "незданих завдань: 2" in notifier.texts[0] and "після твого «✅ Так»" in notifier.texts[0]
    assert quiz.status == "skipped" and "тест чи захист" in notifier.all_text()
    assert lab.status == "working" and lab.due.startswith("2026-10-09")

    run_jobs(manager)
    assert lab.status == "ready"
    task_md = (Path(lab.folder) / "TASK.md").read_text(encoding="utf-8")
    assert "Ляшко Денис Олегович" in task_md and "Ляшко_Д.О._" in task_md and "Виконайте запити" in task_md
    assert [name for name, _ in notifier.documents] == ["Ляшко_Д.О._ПР1.2.pdf", "DEFENSE.md"]
    message_id, text, buttons = notifier.asks[-1]
    assert "Готово" in text and "Здати?" in text and "STATUS" not in text
    assert [b[1] for row in buttons for b in row] == ["study:yes:1:1", "study:redo:1", "study:no:1"]

    ticks(manager, 5, NOW + timedelta(hours=1))
    assert classroom.submitted == []  # без «так» — нічого не здаємо

    assert "здаю" in manager.on_command("study:yes:1:1")
    ticks(manager, 1, NOW + timedelta(hours=1))
    assert classroom.submitted == [("C1/A1", ["Ляшко_Д.О._ПР1.2.pdf"], True)]  # перший раз — пробна
    assert lab.status == "ready" and "Пробна здача" in notifier.asks[-1][1]

    assert notifier.asks[-1][2][0][0][1] == "study:yes:1:1"  # та сама версія
    manager.on_command("study:yes:1:1")
    ticks(manager, 1, NOW + timedelta(hours=1))
    assert classroom.submitted[-1] == ("C1/A1", ["Ляшко_Д.О._ПР1.2.pdf"], False)
    assert lab.status == "submitted" and "Здано" in notifier.asks[-1][1]

    assert "Заберу" in manager.on_command("study:back:1")
    ticks(manager, 1, NOW + timedelta(hours=1))
    assert classroom.unsubmitted == ["C1/A1"] and lab.status == "ready"


def test_incomplete_work_is_never_sent(tmp_path):
    manager, agent, notifier, classroom = make(tmp_path, [LAB], verdict="STATUS: INCOMPLETE: немає доступу до Tinkercad")
    ticks(manager, 2)
    run_jobs(manager)
    lab = manager.task(1)
    assert lab.status == "stuck" and "Tinkercad" in lab.error
    assert notifier.documents == []  # жодних напівготових файлів
    _, text, buttons = notifier.asks[-1]
    assert "не вдалося довести" in text and buttons == [[("🔁 Спробувати ще", "study:retry:1")]]
    assert len([c for c in agent.calls if "перевіряльник" in c[0]]) == 2  # кожна спроба — з перевіркою
    assert manager.on_command("study:yes:1:0").endswith("здавати нічого.")
    ticks(manager, 3, NOW + timedelta(hours=1))
    assert classroom.submitted == []


def test_stale_verdict_does_not_count(tmp_path):
    """Після переробки старий «STATUS: COMPLETE» у RESULT.md не має прийматися за новий."""
    manager, agent, notifier, _ = make(tmp_path, [LAB])
    ticks(manager, 2)
    run_jobs(manager)
    assert manager.task(1).status == "ready"
    agent.verdict = "STATUS: INCOMPLETE: зламався запуск"
    manager.on_command("study:retry:1")
    run_jobs(manager)
    assert manager.task(1).status == "stuck"


def test_redo_by_reply_or_button_with_comment(tmp_path):
    manager, agent, notifier, _ = make(tmp_path, [LAB])
    ticks(manager, 2)
    run_jobs(manager)
    ready_message = notifier.asks[-1][0]

    answer = manager.on_command("додай пояснення до кожного запиту", reply_to=ready_message)
    assert "Переробляю" in answer and manager.task(1).status == "working"
    run_jobs(manager)
    task_md = (Path(manager.task(1).folder) / "TASK.md").read_text(encoding="utf-8")
    assert "Зауваження студента" in task_md and "додай пояснення до кожного запиту" in task_md
    assert manager.task(1).status == "ready" and manager.task(1).version == 2
    assert "старішої версії" in manager.on_command("study:yes:1:1")  # стара кнопка не здає нову версію
    assert manager.task(1).status == "ready"

    assert manager.on_command("study:redo:1") == ""  # без коментаря — питаємо, що змінити
    prompt_id, prompt, _ = notifier.asks[-1]
    assert "Що змінити" in prompt
    manager.on_command("варіант 7, а не 5", reply_to=prompt_id)
    assert manager.task(1).feedback[-1] == "варіант 7, а не 5"
    assert manager.on_command("статус", reply_to=0) is None  # не про навчання — це помічнику пари


def test_agent_unavailable_waits_and_resumes(tmp_path):
    manager, agent, notifier, _ = make(tmp_path, [LAB])
    agent.unavailable = "Antigravity: вичерпано ліміт підписки"
    ticks(manager, 2)
    run_jobs(manager)
    assert manager.task(1).status == "waiting" and "ліміт підписки" in notifier.texts[-1]
    agent.unavailable = ""
    manager.browser_tick(NOW + timedelta(minutes=30))
    assert manager.task(1).status == "working"
    run_jobs(manager)
    assert manager.task(1).status == "ready"


def test_classroom_login_problem_is_told_once_and_retried_later(tmp_path):
    manager, _, notifier, classroom = make(tmp_path, [LAB])
    classroom.error = ClassroomError("увійди в st@stud.duikt.edu.ua у браузері програми")
    ticks(manager, 10)
    assert classroom.todo_calls == 1 and notifier.texts == ["⚠️ Classroom: увійди в st@stud.duikt.edu.ua у браузері програми"]
    classroom.error = None
    manager.browser_tick(NOW + timedelta(minutes=25))
    assert classroom.todo_calls == 2 and manager.task(1) is not None


def test_state_survives_restart(tmp_path):
    manager, *_ = make(tmp_path, [LAB])
    ticks(manager, 2)
    again = StudyManager(manager.config, FakeAgent(), FakeNotifier(), lambda: NOW, tmp_path / "study")
    assert again.task(1).status == "working" and again.state["next_number"] == 2
    again.start()
    again.stop()  # потік завершиться; далі дивимось, що кладеться в чергу
    while not again._jobs.empty():
        again._jobs.get()
    assert again._restarted == [1]
    again.classroom = manager.classroom
    again.browser_tick(NOW + timedelta(minutes=5))  # спершу кукі для матеріалів, потім доробляти
    assert again._jobs.get_nowait() == ("work", 1)


def test_tasks_done_without_us_leave_after_two_checks(tmp_path):
    manager, agent, notifier, classroom = make(tmp_path, [LAB, QUIZ])
    ticks(manager, 3)
    run_jobs(manager)
    assert manager.task(1).status == "ready"
    classroom.items = [QUIZ]  # LAB здали вручну
    manager.check_now()
    manager.browser_tick(NOW + timedelta(hours=1))
    assert manager.task(1).status == "ready"  # один раз — ще не віримо
    manager.check_now()
    manager.browser_tick(NOW + timedelta(hours=2))
    assert manager.task(1).status == "gone" and "здавати нічого" in manager.on_command("study:yes:1:1")
    classroom.items = []  # порожній список — підозріло, нічого не чіпаємо
    manager.check_now()
    manager.browser_tick(NOW + timedelta(hours=3))
    assert manager.task(2).status == "skipped"


def test_lecture_notes_feed_later_assignments(tmp_path):
    manager, agent, notifier, _ = make(tmp_path, [LAB])
    record = LessonRecord(
        report_id="r1", title="Бази даних (Лк)", teacher="Гордієнко", start=NOW - timedelta(hours=2),
        end=NOW - timedelta(minutes=30), joined=NOW - timedelta(hours=2), left=NOW - timedelta(minutes=30),
        transcript=[(NOW - timedelta(hours=1), "сьогодні говоримо про нормалізацію " * 20)],
        chat=[(NOW - timedelta(hours=1), ChatMessage("1", "Гордієнко", "звіти лише в PDF"))],
        mentions=[], sent=[], peak=20, rest="",
    )
    manager.lecture_record(record)
    run_jobs(manager)
    lecture = next(folder for _, folder in agent.calls)
    assert "звіти лише в PDF" in (lecture / "LECTURE.md").read_text(encoding="utf-8")
    assert notifier.texts[-1].startswith("📚 Вижимка: «Бази даних (Лк)»")

    ticks(manager, 2)
    run_jobs(manager)
    task_md = (Path(manager.task(1).folder) / "TASK.md").read_text(encoding="utf-8")
    assert "Викладач: звіти лише в PDF" in task_md  # побажання викладача з лекції дійшли до роботи


def test_short_lectures_are_not_summarized(tmp_path):
    manager, agent, *_ = make(tmp_path)
    manager.lecture("Java (Пз)", "", NOW, "коротко")
    assert manager._jobs.empty()


def test_only_tests_and_defenses_are_skipped(tmp_path):
    from autopara.study import NOT_FOR_AGENT_RE

    for title in ("Тест з теми 2", "ЗАХИСТ", "Захист лабораторних робіт", "МКР 1", "Модульна контрольна робота", "Контрольна робота №1"):
        assert NOT_FOR_AGENT_RE.search(title), title
    for title in ("Лабораторна робота 3. Unit тести", "Тестування API", "ПР № 1.2 Запити вибірки", "Е-проєкт"):
        assert not NOT_FOR_AGENT_RE.search(title), title
    ytpz = Assignment("C7", "A7", "Лабораторна робота №2", "Якість та тестування програмного забезпечення", "ЛР 2")
    manager, _, _, classroom = make(tmp_path, [ytpz])
    classroom.details_text["C7/A7"] = "Якість та тестування програмного забезпечення\nНапишіть тест-кейси для форми входу."
    ticks(manager, 2)
    assert manager.task(1).status == "working"  # «тестування» в назві курсу — не причина пропускати


def test_helpers():
    assert short_name("Ляшко Денис Олегович") == "Ляшко_Д.О."
    subjects = ["Бази даних", "Якість та тестування програмного забезпечення", "Технології Інтернету речей"]
    assert match_subject("2026 Бази даних", subjects) == "Бази даних"
    assert match_subject("ЯТПЗ - 31/2026", subjects) == "Якість та тестування програмного забезпечення"
    assert match_subject("IoT 2026", subjects) is None
    assert due_of("Лаба · До 2 жовт.", NOW).date().isoformat() == "2026-10-02"
    assert due_of("Due Dec 12", NOW).year == 2026 and due_of("без терміну", NOW) is None


def test_list_number_from_group_list(tmp_path):
    manager, *_ = make(tmp_path)
    manager.set_classmates(["Іваненко Олег Петрович", "Ляшко Денис Олегович", "Яковенко Ірина"])
    assert manager.list_number == 2


def test_tasks_ordered_by_deadline(tmp_path):
    soon = Assignment("C3", "A3", "Лаба 3", "Java", "Лаба 3 · Java · До 4 жовт.")
    later = Assignment("C4", "A4", "Лаба 4", "Java", "Лаба 4 · Java · До 20 жовт.")
    overdue = Assignment("C5", "A5", "Лаба 1", "Java", "Лаба 1 · Java · Пропущено · 20 вер.")
    none = Assignment("C6", "A6", "Е-проєкт", "Цифрові технології", "Е-проєкт · Цифрові технології")
    manager, *_ = make(tmp_path, [none, later, overdue, soon])
    manager.browser_tick(NOW)
    assert [task.title for task in manager._ordered()] == ["Лаба 3", "Лаба 4", "Лаба 1", "Е-проєкт"]


def test_replies_to_study_messages_never_reach_the_class_chat(tmp_path):
    """Помічник пересилає відповіді на свої повідомлення в чат пари — відповіді на наші він не має отримати."""
    manager, agent, notifier, _ = make(tmp_path, [LAB])
    manager.lecture("Бази даних (Лк)", "", NOW, "нормалізація " * 60)
    run_jobs(manager)
    summary_id = next(message_id for message_id, text, _ in notifier.asks if text.startswith("📚 Вижимка"))
    assert "не пересилаю" in manager.on_command("дякую", reply_to=summary_id)

    ticks(manager, 2)
    run_jobs(manager)
    document_ids = [m for m in manager.task(1).messages]
    assert len(document_ids) >= 3  # файл, шпаргалка й питання — усі наші
    assert "кнопку" in manager.on_command("так", reply_to=document_ids[0])  # «так» у відповідь не здає
    assert manager.task(1).status == "ready"
    assert "Переробляю" in manager.on_command("тут не той варіант", reply_to=document_ids[0])
    assert manager.on_command("дякую", reply_to=999_999) is None  # чуже — помічнику
