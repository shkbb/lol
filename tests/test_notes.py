"""Записи пар по теках предметів і пошук у них для питань до мозку."""

from datetime import datetime

from autopara import notes


def lesson(root, title, start, summary="", lines=()):
    path = notes.lesson_path(root, title, datetime.fromisoformat(start))
    notes.write(path, f"{title} — {start}", summary, list(lines), [])
    return path


def test_each_lesson_goes_to_its_subject_folder(tmp_path):
    path = lesson(tmp_path, "Бази даних (Лк)", "2026-10-05T09:30", lines=["[09:31:00] Добрий день"])
    assert path == tmp_path / "Бази даних" / "2026-10-05 09-30 Лк.txt"
    assert notes.split_title("Тестовий вхід") == ("Тестовий вхід", "")
    assert notes.lesson_path(tmp_path, "Java: ООП (Лб)", datetime(2026, 10, 6, 8)).parent.name == "Java  ООП"

    notes.add_summary(path, "Про що: індекси.")
    notes.add_summary(path, "Про що: індекси й B-дерева.")  # удруге — замінює, не дублює
    text = path.read_text(encoding="utf-8")
    assert text.count("--- Підсумок ---") == 1 and "B-дерева" in text and "[09:31:00] Добрий день" in text
    assert notes.summary_of(path).endswith("Підсумок: Про що: індекси й B-дерева.")


def test_which_subject_the_question_is_about(tmp_path):
    known = ["Бази даних", "Конструювання програмного забезпечення JAVA", "Цифрові технології"]
    assert notes.find_subject("що було на останніх парах з баз даних?", known) == "Бази даних"
    assert notes.find_subject("про що зараз говорить викладач?", known, current="Цифрові технології") == "Цифрові технології"
    picked = []

    def choose(question, options):
        picked.append(options)
        return 1  # мозок зрозумів: «джава» — це «…JAVA»

    assert notes.find_subject("що задавали на джаві?", known, choose=choose) == known[1]
    assert picked[0][-1].startswith("жоден конкретний")  # можна відповісти «загальне питання»
    assert notes.find_subject("що було на минулих парах?", known, choose=lambda q, o: len(o) - 1) == ""


def test_gather_finds_summaries_and_mentions_within_budget(tmp_path):
    for day in range(1, 7):
        lesson(tmp_path, "Бази даних (Лк)", f"2026-10-0{day}T09:30", summary=f"Про що: тема {day}.")
    lesson(tmp_path, "Цифрові технології (Пз)", "2026-10-02T11:10",
           lines=["[11:20:00] Сьогодні налаштовуємо Docker", "[11:21:00] Контейнер запускаємо командою run"])

    text = notes.gather(tmp_path, "що було останні пари з баз даних?")
    assert "Останні пари з предмета «Бази даних»" in text
    assert text.index("тема 5") < text.index("тема 6") < text.index("← це найновіша пара")  # хронологічно
    assert "тема 1" not in text  # не більше п'яти останніх

    found = notes.gather(tmp_path, "коли ми говорили про Docker?")
    assert "Згадки в записах пар" in found and "налаштовуємо Docker" in found and "Цифрові технології" in found

    general = notes.gather(tmp_path, "що було на минулих парах?")
    assert general.startswith("Останні пари — за датою, остання внизу:")
    assert len(notes.gather(tmp_path, "що було останні пари з баз даних?", budget=300)) < 600
    assert notes.gather(tmp_path / "немає", "будь-що") == ""
