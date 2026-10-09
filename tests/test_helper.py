"""Помічник з навчання: розклад — з даних, про пари — лише із знайденого в записах і з посиланнями."""

from datetime import datetime

from autopara import notes
from autopara.helper import Helper, Library, sections, sound, tokens


def record(root, title, start, teacher, summary, lines, chat=()):
    path = notes.lesson_path(root, title, datetime.fromisoformat(start))
    begin = datetime.fromisoformat(start)
    header = f"{title} — {begin:%d.%m.%Y %H:%M}–{begin.replace(hour=begin.hour + 1):%H:%M}, {teacher}"
    notes.write(path, header, summary, list(lines), list(chat))
    return path


def library(tmp_path):
    record(tmp_path, "Безпека розробки програмного забезпечення (Пз)", "2026-10-09T12:40", "Петренко П.П.",
           "**Про що:** лабораторна №3, валідація вхідних даних.\n**Завдання й дедлайни:** захистити 1 і 2 лабораторні.",
           ["[12:55:28] Ви використовували посилання на PCI DSS Standard, але він стосується тільки фінансових операцій",
            "[12:57:36] якщо оплату процесить банк через gateway, відповідність PCI DSS не потрібна",
            "[12:58:02] Третя лабораторна у нас по валідації вхідних даних"])
    record(tmp_path, "Технології проєктування та адміністрування баз даних (Пз)", "2026-10-06T09:30", "Мельник О.В.",
           "**Про що:** первинні й сурогатні ключі.", ["[09:40:00] Сурогатний ключ — це штучний ідентифікатор"])
    record(tmp_path, "Якість і тестування програмного забезпечення (Лк)", "2026-10-05T08:00", "Сидоренко І.І.",
           "**Про що:** Playwright і TypeScript, лекцію веде Дмитро Глухов.", ["[08:04:35] Магістр Дмитро Глухов"])
    lib = Library(tmp_path)
    lib.refresh()
    return lib


NOW = datetime(2026, 10, 9, 19, 30)


def test_questions_are_routed_and_schedule_needs_no_model(tmp_path):
    asked = []
    helper = Helper(library(tmp_path), lambda system, text: asked.append(text) or "відповідь",
                    schedule=lambda now: "📅 завтра:\n  08:00–09:20 Архітектура ПЗ (Лк)")
    assert helper.route("Яка наступна пара?") == "schedule"
    assert helper.route("Що задали з безпеки?") == "tasks"
    assert helper.route("Що було вчора?") == "lesson"
    assert helper.route("Коли говорили про PCI DSS?") == "search"
    assert helper.answer("Яка наступна пара?", NOW).startswith("📅") and asked == []  # без мовної моделі


def test_right_lesson_is_found_by_subject_date_and_last(tmp_path):
    helper = Helper(library(tmp_path), None)
    last = helper.sources("Що було на останній парі?", "lesson", NOW)
    assert last.startswith("[Безпека розробки програмного забезпечення (Пз), 09.10.2026")
    tests = helper.sources("Про що була пара з тестування 05.10?", "lesson", NOW)
    assert "Playwright" in tests and "Безпека" not in tests.split("\n")[0]
    assert helper.sources("Що було вчора?", "lesson", NOW).startswith("За 08.10.2026 записів пар немає")
    db = helper.sources("Що задали з баз даних?", "tasks", NOW)
    assert "сурогатні ключі" in db


def test_search_finds_words_in_any_form_and_cites_place(tmp_path):
    lib = library(tmp_path)
    assert tokens("лабораторну") == tokens("лабораторна")  # відмінки — одне слово
    found = lib.search("коли говорили про стандарт PCI DSS?")
    assert found and found[0][1].doc.short.startswith("Безпека") and found[0][1].time == "12:55"
    assert Helper(lib, None).sources("Коли говорили про PCI DSS?", "search", NOW).startswith("[Безпека розробки, 09.10 12:55]")


def test_lessons_tasks_and_teachers_come_straight_from_summaries(tmp_path):
    asked = []
    helper = Helper(library(tmp_path), lambda system, text: asked.append(text) or "відповідь")
    last = helper.answer("Що було на останній парі?", NOW)
    assert last.startswith("📖 Безпека розробки програмного забезпечення (Пз), 09.10 12:40") and "валідація вхідних даних" in last
    tasks = helper.answer("Що задали з безпеки розробки?", NOW)
    assert tasks.startswith("📝") and "захистити 1 і 2 лабораторні" in tasks and "Безпека розробки (Пз), 09.10" in tasks
    assert helper.answer("Що було вчора?", NOW).startswith("За 08.10 (четвер) записів пар немає")
    assert "Мельник О.В." in helper.answer("Хто викладає бази даних?", NOW)
    assert asked == []  # мовна модель не потрібна — отже, й вигадати нічого не може


def test_summary_placeholders_and_empty_deadlines_are_dropped():
    parts = sections("**Про що:** ключі.\n**Завдання й дедлайни:** Зняти відео до [дата]. Захистити лабу 2. "
                     "Дедлайни інших завдань не вказані.\n**Важливе:** нічого")
    assert parts["Завдання й дедлайни"] == "Захистити лабу 2." and parts["Про що"] == "ключі."


def test_search_answer_is_cited_by_code_and_general_knowledge_is_marked(tmp_path):
    prompts = []
    helper = Helper(library(tmp_path), lambda system, text: prompts.append((system, text)) or "**Не потрібна**, якщо платить банк.")
    reply = helper.answer("Коли говорили про PCI DSS?", NOW, dialog="Студент: привіт\nautopara: привіт!")
    assert reply.startswith("Не потрібна, якщо платить банк.") and reply.endswith("Джерела: Безпека розробки, 09.10 12:55")
    system, text = prompts[-1]
    assert "Знайдено в записах пар" in text and "Розмова перед цим" in text and text.endswith("Студент пише: Коли говорили про PCI DSS?")
    assert helper.answer("Поясни, що таке REST API", NOW).startswith("📚 ")  # у записах немає — загальні знання
    assert "Знайдено в записах" not in prompts[-1][1]
    assert helper.answer("Що казали про Kubernetes?", NOW).startswith("У записах пар про Kubernetes нічого не знайшов")
    helper.answer("Привіт! Як справи?", NOW)
    assert "Знайдено в записах" not in prompts[-1][1]  # привітання — не пошук


def test_terms_match_in_any_spelling(tmp_path):
    record(tmp_path, "Безпека розробки програмного забезпечення (Лк)", "2026-10-05T11:10", "Петренко П.П.", "",
           ["[11:44:00] Два приклади в індустрії — Ovasp Zap, тобто ОВАСП, які займаються безпекою"])
    lib = Library(tmp_path)
    lib.refresh()
    found = lib.search("Що казали про OWASP?")
    assert found and found[0][1].time == "11:44"
    assert sound("OWASP") == sound("ОВАСП") == sound("Ovasp")


def test_without_language_model_data_answers_still_work(tmp_path):
    helper = Helper(library(tmp_path), None)
    assert helper.quick("Що задали з безпеки?", NOW).startswith("📝")
    assert helper.quick("Поясни, що таке REST API", NOW) is None  # тут потрібна модель
    assert helper.answer("Поясни, що таке REST API", NOW).startswith("Мовна модель ще не готова")
