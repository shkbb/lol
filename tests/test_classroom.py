"""Classroom через браузер — на імітації його сторінок у справжньому Chromium."""

import pytest

pytest.importorskip("playwright")

from autopara.classroom import Classroom, classify_link  # noqa: E402
from autopara.joiner import Browser  # noqa: E402

ACCOUNT = "st1234567@stud.duikt.edu.ua"

TODO = """<!doctype html><meta charset="utf-8"><main>
<a href="/u/1/c/NzczOTA4MDA5NzIz/a/ODEzODA5MDg0ODE5/details"><div>Лабораторна робота_1</div><div>Технології Інтернету речей</div><div>До 2 жовт.</div></a>
<a href="/u/1/c/ODc3MTU5ODM5Nzc0/a/ODc3MTU5ODM5ODM1/details"><div>ПР № 1.2 Запити вибірки</div><div>2026 Бази даних</div></a>
<a href="/u/1/c/ODc3MTU5ODM5Nzc0/a/ODc3MTU5ODM5ODM1/details">ще раз те саме посилання</a>
<a href="/u/1/c/ODc3MTU5ODM5Nzc0">курс</a>
</main>"""

DETAILS = """<!doctype html><meta charset="utf-8"><title>ПР № 1.2 Запити вибірки - Classroom</title>
<main>
  <h1>ПР № 1.2 Запити вибірки в реляційні бази даних</h1>
  <div>Термін: 9 жовт.</div>
  <p>Варіанти, схема БД та скрипт генерації — у вкладених файлах. Варіант — за списком групи.</p>
  <a href="https://drive.google.com/file/d/FILE123/view?usp=classroom_web">Варіанти.pdf</a>
  <a href="https://docs.google.com/document/d/DOC456/edit">Схема БД</a>
  <a href="https://docs.google.com/forms/d/e/FORM789/viewform">Тест</a>
  <section aria-label="Ваша робота">
    <div id="status">Призначено</div>
    <div id="files"></div>
    <button id="add">Додати або створити</button>
    <div id="menu" role="menu" hidden><div role="menuitem" id="file">Файл</div><div role="menuitem">Посилання</div></div>
    <button id="turnin">Здати</button>
  </section>
</main>
<div id="picker"></div>
<div id="dialog" role="dialog" hidden></div>
<script>
// Як справжній Classroom, стан здачі зберігається між перезавантаженнями сторінки.
const $ = (id) => document.getElementById(id);
const turnedIn = () => localStorage.getItem("turnedIn") === "1";
function draw() {
  $("status").textContent = turnedIn() ? "Здано" : "Призначено";
  $("turnin").textContent = turnedIn() ? "Скасувати здачу" : "Здати";
}
function ask(question, yes, onYes) {
  $("dialog").innerHTML = question + ' <button id="cancel">Скасувати</button><button id="yes">' + yes + "</button>";
  $("dialog").hidden = false;
  $("cancel").onclick = () => { $("dialog").hidden = true; };
  $("yes").onclick = () => { $("dialog").hidden = true; onYes(); draw(); };
}
$("add").onclick = () => { $("menu").hidden = false; };
$("file").onclick = () => {
  $("menu").hidden = true;
  $("picker").innerHTML = '<iframe id="pickerframe" src="https://docs.google.com/picker?x=1"></iframe>';
};
window.addEventListener("message", (event) => {
  for (const name of event.data.files) $("files").insertAdjacentHTML("beforeend", "<div>" + name + "</div>");
  $("picker").innerHTML = "";
});
$("turnin").onclick = () => turnedIn()
  ? ask("Скасувати здачу?", "Скасувати здачу", () => localStorage.removeItem("turnedIn"))
  : ask("Здати роботу?", "Здати", () => localStorage.setItem("turnedIn", "1"));
draw();
</script>"""

PICKER = """<!doctype html><meta charset="utf-8"><div>Завантажити</div>
<input type="file" id="input" multiple hidden>
<button id="browse">Огляд</button>
<script>
document.getElementById("browse").onclick = () => document.getElementById("input").click();
document.getElementById("input").onchange = (e) =>
  parent.postMessage({ files: [...e.target.files].map((f) => f.name) }, "*");
</script>"""


@pytest.fixture
def classroom(tmp_path):
    browser = Browser(tmp_path / "profile", channel="", headless=True)
    context = browser.context()
    context.route("https://classroom.google.com/a/not-turned-in/**", lambda route: route.fulfill(body=TODO, content_type="text/html"))
    context.route("https://classroom.google.com/c/**", lambda route: route.fulfill(body=DETAILS, content_type="text/html"))
    context.route("https://docs.google.com/picker**", lambda route: route.fulfill(body=PICKER, content_type="text/html"))
    yield Classroom(browser.new_page, ACCOUNT), browser
    browser.close()


def test_todo_list_from_stable_links(classroom):
    reader, _ = classroom
    items = reader.todo()
    assert [(i.key, i.title, i.course) for i in items] == [
        ("NzczOTA4MDA5NzIz/ODEzODA5MDg0ODE5", "Лабораторна робота_1", "Технології Інтернету речей"),
        ("ODc3MTU5ODM5Nzc0/ODc3MTU5ODM5ODM1", "ПР № 1.2 Запити вибірки", "2026 Бази даних"),
    ]
    assert "До 2 жовт." in items[0].listing


def test_details_text_and_attachments(classroom):
    reader, _ = classroom
    item = reader.details(reader.todo()[1])
    assert "Варіант — за списком групи" in item.text and item.status == "assigned"
    kinds = [(a.kind, a.file_id) for a in item.attachments]
    assert kinds == [("drive", "FILE123"), ("document", "DOC456"), ("forms", "FORM789")]
    assert item.is_quiz  # є Google Forms — це тест, не для агента
    url, extension = item.attachments[1].download_url(ACCOUNT)
    assert url.startswith("https://docs.google.com/document/d/DOC456/export?format=docx") and extension == ".docx"


def test_dry_run_then_real_submission_then_unsubmit(classroom, tmp_path):
    reader, browser = classroom
    item = reader.todo()[1]
    report = tmp_path / "Ляшко_Д.О._ПР1.2.pdf"
    report.write_bytes(b"%PDF-1.4 test")

    dry = reader.submit(item, [report], dry_run=True)
    assert dry.ok and "пробна" in dry.note and dry.screenshot

    real = reader.submit(item, [report])
    assert real.ok and real.note == "здано" and real.screenshot

    back = reader.unsubmit(item)
    assert back.ok, back.note


def test_link_classification():
    assert classify_link("https://forms.gle/abc").kind == "forms"
    assert classify_link("https://drive.google.com/open?id=XYZ").file_id == "XYZ"
    assert classify_link("https://www.youtube.com/watch?v=1").kind == "link"
    assert classify_link("https://accounts.google.com/x") is None
