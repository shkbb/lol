"""Панелі, спільні для майстра першого запуску й налаштувань: група, посилання, Google, Telegram."""

from __future__ import annotations

import secrets

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from .. import app as app_module
from ..links import platform_of
from . import widgets as w

PLATFORM = {"meet": "Meet", "zoom": "Zoom", "other": "сайт"}


def open_url(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


def lesson_badge(lesson: dict) -> QLabel:
    if lesson.get("source") == "skip":
        return w.badge("не заходжу", "plain")
    if not lesson.get("url"):
        return w.badge("немає посилання", "bad")
    text = PLATFORM.get(lesson.get("platform") or "", "сайт")
    if lesson.get("source") == "schedule":
        text += " · з розкладу"
    widget = w.badge(text, "ok")
    widget.setToolTip(lesson["url"])
    return widget


# --- Група ----------------------------------------------------------------------------


class GroupPanel(QWidget):
    """Університет і група: почни вводити назву — програма знайде її на сайті розкладу."""

    chosen = Signal()

    def __init__(self, model, bridge, extended: bool = False):
        super().__init__()
        self.model, self.bridge = model, bridge
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.universities = QButtonGroup(self)
        row = QHBoxLayout()
        row.setSpacing(8)
        for code, name in model.meta.get("universities", {}).items():
            seg = w.button(name, "seg")
            seg.setCheckable(True)
            seg.setChecked(code == self._university())
            seg.clicked.connect(lambda *_, code=code: self._pick_university(code))
            self.universities.addButton(seg)
            row.addWidget(seg)
        row.addStretch(1)
        layout.addLayout(row)

        self.search = QLineEdit(model.get("schedule.group") or "")
        self.search.setClearButtonEnabled(True)
        self.search.textEdited.connect(self._typed)
        self.results = QListWidget()
        self.results.setObjectName("results")
        self.results.setMaximumHeight(220)
        self.results.hide()
        self.results.itemClicked.connect(self._picked)
        self.state = w.label("", "muted")
        layout.addWidget(w.field("Група", self.search))
        layout.addWidget(self.results)
        layout.addWidget(self.state)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self._find)
        self._placeholder()
        self._describe()
        self._prefetch()

        if extended:
            more = w.More()
            if self._university() == "duikt":
                more.add(self._student_control())
            more.add(w.number_field(model, "schedule.days_ahead", "На скільки днів уперед дивитися розклад", minimum=1, maximum=62))
            more.add(w.number_field(model, "schedule.refresh_minutes", "Оновлювати розклад кожні", "Пари іноді переносять.", minimum=5, suffix=" хв"))
            more.add(w.switch_row(model, "schedule.use_schedule_links", "Брати посилання з самого розкладу",
                                  "Якщо для пари немає твого посилання, а викладач вписав Meet чи Zoom у розклад."))
            layout.addWidget(more)

    def _prefetch(self) -> None:
        """Список груп ДУІКТ завантажується ~20 с (сайт віддає їх по факультетах і курсах) — починаємо одразу,
        поки людина читає, щоб пошук відповів миттєво."""
        if self.model.get("schedule.group"):
            return
        source = self.bridge.server.source_for(self._university())
        self.bridge.run(source.all_groups)

    def _university(self) -> str:
        return self.model.get("schedule.university") or "duikt"

    def _placeholder(self) -> None:
        kpi = self._university() == "kpi"
        self.search.setPlaceholderText("ІП-21 або посилання schedule.kpi.ua/?groupId=…" if kpi else "Наприклад, ТЦР-32")

    def _pick_university(self, code: str) -> None:
        if code == self._university():
            return
        self.model.set("schedule.university", code)
        for key in ("faculty_id", "course", "group_id", "group_uid", "student"):
            self.model.set(f"schedule.{key}", None)
        self.model.set("schedule.group", "")
        self.search.clear()
        self.results.hide()
        self._placeholder()
        self._describe()
        self._prefetch()

    def _typed(self, text: str) -> None:
        text = text.strip()
        if "groupId=" in text:  # КПІ: вставили посилання на розклад групи
            uid = text.split("groupId=", 1)[1].split("&")[0]
            self.model.set("schedule.group", text)
            self.model.set("schedule.group_uid", uid)
            self.results.hide()
            self._describe()
            self.chosen.emit()
            return
        self._timer.start()

    def _find(self) -> None:
        text = self.search.text().strip()
        if len(text) < 2:
            self.results.hide()
            return
        self.results.clear()
        wait = "Шукаю… (перший раз — кілька секунд)" if self._university() == "duikt" else "Шукаю…"
        self.results.addItem(wait)
        self.results.show()
        query = {"university": self._university(), "q": text}
        self.bridge.call_api(app_module.api_groups, query, ok=lambda data: self._show(text, data["groups"]), fail=self._failed)

    def _show(self, text: str, groups: list) -> None:
        if self.search.text().strip() != text:
            return  # уже ввели інше
        self.results.clear()
        if not groups:
            self.results.addItem("Нічого не знайшлося — перевір назву групи")
            return
        for group in groups[:30]:
            extra = " · ".join(filter(None, [group.get("faculty") or "", f"{group['course']} курс" if group.get("course") else ""]))
            item = QListWidgetItem(f"{group['name']}    {extra}")
            item.setData(256, group)
            self.results.addItem(item)

    def _failed(self, message: str) -> None:
        self.results.clear()
        self.results.addItem(f"Не вдалося знайти: {message}")

    def _picked(self, item: QListWidgetItem) -> None:
        group = item.data(256)
        if not group:
            return
        self.model.set("schedule.group", group["name"])
        self.model.set("schedule.faculty_id", group.get("faculty_id"))
        self.model.set("schedule.course", group.get("course"))
        self.model.set("schedule.group_id", group.get("group_id"))
        self.model.set("schedule.group_uid", group.get("uid") or None)
        self.model.set("schedule.student", None)
        self.search.setText(group["name"])
        self.results.hide()
        self._describe()
        self.model.save_now()
        self.chosen.emit()

    def _describe(self) -> None:
        group = self.model.get("schedule.group")
        course = self.model.get("schedule.course")
        if group:
            self.state.setText(f"✓ Обрано: {group}" + (f", {course} курс" if course else ""))
        else:
            self.state.setText("Групу ще не обрано")

    def _student_control(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        combo = QComboBox()
        current = self.model.get("schedule.student") or ""
        combo.addItem("— розклад усієї групи —", "")
        if current:
            combo.addItem(current, current)
            combo.setCurrentIndex(1)
        load = w.button("Завантажити список групи")

        def loaded(data: dict) -> None:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("— розклад усієї групи —", "")
            for name in data["students"]:
                combo.addItem(name, name)
            index = combo.findData(self.model.get("schedule.student") or "")
            combo.setCurrentIndex(max(index, 0))
            combo.blockSignals(False)
            load.setText("Список завантажено")

        def fetch() -> None:
            s = self.model.data.get("schedule", {})
            if s.get("faculty_id") and s.get("course") and s.get("group_id"):
                query = {"faculty_id": s["faculty_id"], "course": s["course"], "group_id": s["group_id"]}
            else:
                query = {"group": s.get("group", "")}
            load.setText("Завантажую…")
            self.bridge.call_api(app_module.api_students, query, ok=loaded, fail=lambda m: load.setText(f"Не вдалося: {m}"))

        load.clicked.connect(fetch)
        combo.currentIndexChanged.connect(lambda _: self.model.set("schedule.student", combo.currentData() or None))
        layout.addWidget(w.field("Мій розклад (з урахуванням підгруп)", combo,
                                 "Обери себе зі списку групи — тоді розклад буде саме твій."))
        layout.addLayout(w.hbox(load, "stretch"))
        return box


# --- Посилання на пари --------------------------------------------------------------------


def account_choices(model) -> list[tuple[str, str]]:
    main = model.get("join.meet_account") or ""
    others = [a for a in (model.get("join.accounts") or []) if a and a != main]
    return [("", f"основний{f' ({main})' if main else ''}")] + [(a, a) for a in others]


def subject_badge(lessons: list[dict]) -> QLabel:
    """Стан предмета: усі заняття з посиланням, частина чи жодне."""
    done = [lesson for lesson in lessons if lesson.get("url") or lesson.get("source") == "skip"]
    if not done:
        return w.badge("немає посилання", "bad")
    if len(done) < len(lessons):
        return w.badge("частково", "warn")
    if all(lesson.get("source") == "skip" for lesson in lessons):
        return w.badge("не заходжу", "plain")
    platforms = dict.fromkeys(PLATFORM.get(lesson.get("platform") or "", "сайт") for lesson in lessons if lesson.get("url"))
    text = " · ".join(platforms)
    if all(lesson.get("source") == "schedule" for lesson in lessons if lesson.get("url")):
        text += " · з розкладу"
    widget = w.badge(text, "ok")
    widget.setToolTip("\n".join(dict.fromkeys(lesson["url"] for lesson in lessons if lesson.get("url"))))
    return widget


class SubjectRow(QFrame):
    """Предмет із розкладу (усі його заняття — Лк, Пз, Лб — одним рядком): куди на нього заходити."""

    def __init__(self, panel: LinksPanel, subject: str, lessons: list[dict]):
        super().__init__()
        self.panel, self.subject, self.lessons = panel, subject, lessons
        self.setObjectName("row")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)
        types = list(dict.fromkeys(lesson["type"] for lesson in lessons if lesson.get("type")))
        teachers = list(dict.fromkeys(lesson["teacher"] for lesson in lessons if lesson.get("teacher")))
        head = QVBoxLayout()
        head.setSpacing(1)
        name = w.label(subject + (f"  ({' · '.join(types)})" if types else ""))
        name.setStyleSheet("font-weight: 700;")
        head.addWidget(name)
        if teachers:
            head.addWidget(w.label(", ".join(teachers), "hint"))
        resolved = any(lesson.get("url") or lesson.get("source") == "skip" for lesson in lessons)
        self.edit_button = w.button("Змінити" if resolved else "Задати", "", self._toggle)
        top = QHBoxLayout()
        top.addLayout(head, 1)
        top.addWidget(subject_badge(lessons))
        top.addWidget(self.edit_button)
        layout.addLayout(top)

        self.editor = QWidget()
        editor = QVBoxLayout(self.editor)
        editor.setContentsMargins(0, 4, 0, 0)
        editor.setSpacing(8)
        own = list(dict.fromkeys(lesson["url"] for lesson in lessons if lesson.get("source") == "config" and lesson.get("url")))
        self.url = QLineEdit(own[0] if len(own) == 1 else "")
        self.url.setPlaceholderText("https://meet.google.com/…  або  https://…zoom.us/j/…")
        self.url.returnPressed.connect(self._add)
        self.scope = QComboBox()
        self.scope.addItem("для всіх занять", "")
        for kind in types:
            self.scope.addItem(f"лише «{kind}»", kind)
        self.account = QComboBox()
        for value, text in account_choices(panel.model):
            self.account.addItem(text, value)
        self.skip = QCheckBox("не заходити")
        self.skip.toggled.connect(lambda on: self.url.setDisabled(on))
        self.error = w.label("", "hint")
        self.error.setStyleSheet("color: #c62828;")
        self.error.hide()
        editor.addWidget(self.url)
        options = QHBoxLayout()
        options.setSpacing(12)
        if len(types) > 1:
            options.addWidget(self.scope)
        if len(account_choices(panel.model)) > 1:
            options.addWidget(self.account)
        options.addWidget(self.skip)
        options.addStretch(1)
        options.addWidget(w.button("Зберегти", "primary", self._add))
        editor.addLayout(options)
        editor.addWidget(self.error)
        self.editor.hide()
        layout.addWidget(self.editor)

    def _toggle(self) -> None:
        self.editor.setVisible(not self.editor.isVisible())
        if self.editor.isVisible():
            self.url.setFocus()

    def _add(self) -> None:
        url = self.url.text().strip()
        if not self.skip.isChecked():
            if not url:
                return self._fail("Встав посилання або постав «не заходити»")
            if platform_of(url) == "other":
                return self._fail("Це не схоже на посилання Google Meet чи Zoom — перевір, що скопіював повністю")
        rule = {"subject": self.subject}
        if self.scope.currentData():
            rule["type"] = self.scope.currentData()
        if self.skip.isChecked():
            rule["skip"] = True
        else:
            rule["url"] = url
        if self.account.currentData():
            rule["account"] = self.account.currentData()
        self.panel.add_rule(rule)

    def _fail(self, text: str) -> None:
        self.error.setText(text)
        self.error.show()


class LinksPanel(QWidget):
    """Предмети з розкладу на два тижні вперед і куди на кожен заходити."""

    def __init__(self, model, bridge, extended: bool = False):
        super().__init__()
        self.model, self.bridge, self.extended = model, bridge, extended
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.note = w.label("", "muted")
        layout.addWidget(self.note)
        self.list = QVBoxLayout()
        self.list.setSpacing(8)
        layout.addLayout(self.list)
        self.rules_box = None
        if extended:
            self.rules_more = w.More("Усі правила")
            self.rules_box = QVBoxLayout()
            self.rules_box.setSpacing(8)
            self.rules_more.add(w.label("Перевіряються згори донизу: спрацьовує перше, що підійшло. «Предмет» — шматок назви, "
                                        "кілька варіантів через «|».", "hint"))
            self.rules_more.add(self.rules_box)
            self.rules_more.add(w.hbox(w.button("Додати правило вручну", "", self._new_rule), "stretch"))
            layout.addWidget(self.rules_more)
        self._pending = False
        model.saved_ok.connect(self._after_save)
        self.reload()

    def reload(self) -> None:
        w.clear(self.list)
        self._draw_rules()
        if not (self.model.get("schedule.group") or self.model.get("schedule.group_uid")):
            self.note.setText("Спершу обери групу — тоді тут з'являться твої предмети.")
            return
        self.note.setText("Завантажую предмети з розкладу…")
        self.bridge.call_api(app_module.api_lessons, {"days": "14"}, ok=self._show, fail=self._failed)

    def _show(self, data: dict) -> None:
        w.clear(self.list)
        subjects: dict = {}  # предмет → по одному заняттю кожного типу (Лк, Пз, Лб)
        for lesson in data["lessons"]:
            subjects.setdefault(lesson["subject"], {})[lesson.get("type") or ""] = lesson
        if not subjects:
            self.note.setText("Найближчі два тижні пар немає.")
            return

        def open_(lessons: list[dict]) -> bool:
            return any(not lesson.get("url") and lesson.get("source") != "skip" for lesson in lessons)

        rows = {subject: list(by_type.values()) for subject, by_type in subjects.items()}
        missing = sum(1 for lessons in rows.values() if open_(lessons))
        self.note.setText(
            f"Предметів без посилання: {missing}. Натисни «Задати» й встав посилання з Meet чи Zoom." if missing
            else "✓ Для всіх пар є посилання."
        )
        for subject, lessons in sorted(rows.items(), key=lambda item: (not open_(item[1]), item[0])):
            self.list.addWidget(SubjectRow(self, subject, lessons))

    def _failed(self, message: str) -> None:
        self.note.setText(f"Не вдалося завантажити розклад: {message}")

    def add_rule(self, rule: dict) -> None:
        self.model.links.insert(0, rule)
        self.model.touch()
        self._pending = True
        self.model.save_now()
        self.note.setText("Зберігаю…")

    def _after_save(self) -> None:
        if self._pending:
            self._pending = False
            self.reload()

    # Усі правила — для тих, кому треба тонко: різні посилання за викладачем, вручну тощо.

    def _draw_rules(self) -> None:
        if self.rules_box is None:
            return
        w.clear(self.rules_box)
        self.rules_more.toggle.setText(f"Усі правила ({len(self.model.links)})")
        for index, rule in enumerate(self.model.links):
            self.rules_box.addLayout(self._rule_row(index, rule))

    def _rule_row(self, index: int, rule: dict) -> QHBoxLayout:
        def bound(key: str, placeholder: str, width: int | None = None) -> QLineEdit:
            edit = QLineEdit(str(rule.get(key) or ""))
            edit.setPlaceholderText(placeholder)
            if width:
                edit.setMaximumWidth(width)

            def edited(text: str) -> None:
                if text.strip():
                    rule[key] = text.strip()
                else:
                    rule.pop(key, None)
                self.model.touch()

            edit.textEdited.connect(edited)
            return edit

        def move(delta: int) -> None:
            target = index + delta
            links = self.model.links
            if 0 <= target < len(links):
                links[index], links[target] = links[target], links[index]
                self.model.touch()
                self._draw_rules()

        def remove() -> None:
            del self.model.links[index]
            self.model.touch()
            self._pending = True
            self._draw_rules()

        url = w.label("не заходити", "muted") if rule.get("skip") else bound("url", "посилання")
        row = w.hbox(bound("subject", "предмет"), bound("type", "тип", 70), url, spacing=6)
        row.setStretch(0, 2)
        row.setStretch(2, 3)
        for text, tip, action in (("↑", "Вище", lambda: move(-1)), ("↓", "Нижче", lambda: move(1)), ("✕", "Прибрати", remove)):
            small = w.button(text, "", action)
            small.setToolTip(tip)
            small.setFixedWidth(36)
            row.addWidget(small)
        return row

    def _new_rule(self) -> None:
        self.model.links.append({"subject": "", "url": ""})
        self.model.touch()
        self.rules_more.toggle.setChecked(True)
        self._draw_rules()


# --- Акаунти Google -------------------------------------------------------------------------------


def preferred_account(accounts: list[str]) -> str:
    """Основний — університетська пошта (не gmail.com), бо пари найчастіше пускають саме з неї."""
    for account in accounts:
        if not account.lower().endswith(("@gmail.com", "@googlemail.com")):
            return account
    return accounts[0] if accounts else ""


class AccountsPanel(QWidget):
    """«Увійти в Google» → програма сама бачить, у які акаунти ти увійшов, і бере університетський."""

    def __init__(self, model, bridge, extended: bool = False):
        super().__init__()
        self.model, self.bridge = model, bridge
        self._accounts: list[str] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.status = w.label("", "big")
        self.explain = w.label("", "muted")
        self.choice_box = QWidget()
        self.choice = QVBoxLayout(self.choice_box)
        self.choice.setContentsMargins(0, 0, 0, 0)
        self.choice.setSpacing(6)
        self.login = w.button("Увійти в Google", "big", self._login)
        self.another = w.button("Увійти ще в один акаунт", "", self._login)
        self.done = w.button("Готово — закрити вхід", "", self._close_login)
        self.message = w.label("", "hint")
        layout.addWidget(self.status)
        layout.addWidget(self.explain)
        layout.addWidget(self.choice_box)
        layout.addLayout(w.hbox(self.login, self.another, self.done, "stretch"))
        layout.addWidget(self.message)
        if extended:
            more = w.More()
            more.add(w.text_field(model, "join.meet_account", "Основний акаунт вручну",
                                  "Якщо програма не побачила твій акаунт сама.", "st0000000@stud.example.edu.ua"))
            more.add(w.field("Інші акаунти", w.ChipsField(model, "join.accounts", "пошта й Enter"),
                             "Для зустрічей, які не пускають з основного. Акаунт для пари обирається в «Пари» → «Задати»."))
            more.add(w.label("Zoom", "h2"))
            more.add(w.choice_field(model, "join.zoom_mode", [
                ("auto", "Автоматично (рекомендовано)", "Zoom-клієнт, якщо помічник може його чути; інакше Zoom у браузері програми."),
                ("app", "Завжди Zoom-клієнт", "Мікрофон і камеру вимикають налаштування самого Zoom."),
                ("browser", "Завжди Zoom у браузері", "Працює розумний вихід; деякі викладачі вхід через браузер вимикають."),
            ], default="auto"))
            more.add(w.text_field(model, "join.display_name", "Ім'я для входу гостем",
                                  "Якщо Meet чи Zoom пустять лише як гостя.", "Прізвище Ім'я Група"))
            layout.addWidget(more)
        self.update_state({})

    def _login(self) -> None:
        self.message.setText("Відкриваю браузер програми…")
        self.bridge.command("login", ok=lambda _: self._opened(), fail=lambda m: self.message.setText(f"⚠ {m}"),
                            timeout=900)

    def _opened(self) -> None:
        self.message.setText("Увійди на сторінці «Браузер». Програма побачить акаунт сама — тут з'явиться «✓ Увійдено».")
        self.bridge.browser_wanted.emit()

    def _close_login(self) -> None:
        self.bridge.command("close_login", ok=lambda _: self.message.setText(""))

    def update_state(self, state: dict) -> None:
        accounts = state.get("google_accounts")
        if accounts is None:
            accounts = self.model.meta.get("google_accounts") or []
        browser = state.get("browser") or {}
        login_open = bool(state.get("login_open"))
        self.done.setVisible(login_open and bool(accounts))
        self.another.setVisible(bool(accounts))
        self.login.setVisible(not accounts)
        if browser.get("status") == "installing":
            self.login.setEnabled(False)
            self.login.setText("Готую браузер програми…")
        else:
            self.login.setEnabled(True)
            self.login.setText("Увійти в Google")
        if accounts != self._accounts:
            self._accounts = list(accounts)
            self._draw_accounts()
        if accounts:
            self.status.setText("✓ Увійдено: " + ", ".join(accounts))
            self.explain.setText("З якого акаунта заходити на пари:" if len(accounts) > 1 else
                                 "З цього акаунта програма заходитиме на пари.")
        else:
            self.status.setText("Ще не увійшов у Google")
            self.explain.setText("Браузер програми відкриється прямо тут, у вікні, — увійди в університетську пошту. "
                                 "Вхід збережеться: робити це треба лише раз.")

    def _draw_accounts(self) -> None:
        w.clear(self.choice)
        accounts = self._accounts
        main = self.model.get("join.meet_account") or ""
        if accounts and main not in accounts:
            main = preferred_account(accounts)
            self.model.set("join.meet_account", main)
        if accounts:
            others = [a for a in accounts if a != main]
            known = [a for a in (self.model.get("join.accounts") or []) if a != main]
            self.model.set("join.accounts", list(dict.fromkeys(known + others)) or None)
        if len(accounts) < 2:
            self.choice_box.hide()
            return
        self.choice_box.show()
        group = QButtonGroup(self.choice_box)
        for account in accounts:
            radio = QRadioButton(account)
            radio.setChecked(account == main)
            radio.toggled.connect(lambda on, account=account: on and self._choose(account))
            group.addButton(radio)
            self.choice.addWidget(radio)

    def _choose(self, account: str) -> None:
        self.model.set("join.meet_account", account)
        others = [a for a in (self.model.get("join.accounts") or []) if a != account]
        old = [a for a in self._accounts if a != account]
        self.model.set("join.accounts", list(dict.fromkeys(others + old)) or None)


# --- Telegram -----------------------------------------------------------------------------------


class TelegramPanel(QWidget):
    """Свій бот за хвилину: створити в @BotFather, вставити токен, натиснути Start."""

    connected = Signal()

    def __init__(self, model, bridge, extended: bool = False):
        super().__init__()
        self.model, self.bridge = model, bridge
        self.username = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        steps_box = QWidget()
        steps = QVBoxLayout(steps_box)
        steps.setContentsMargins(0, 0, 0, 0)
        steps.setSpacing(14)
        if model.get("telegram.chat_id") and model.get("telegram.token"):
            # Уже підключено — кроки сховані, на виду лише головне.
            self.summary = w.label("✓ Telegram підключено", "big")
            layout.addWidget(self.summary)
            layout.addLayout(w.hbox(w.button("Надіслати тест", "", self._test), "stretch"))
            self.summary_note = w.label("", "muted")
            layout.addWidget(self.summary_note)
            change = w.More("Підключити іншого бота")
            change.add(steps_box)
            layout.addWidget(change)
        else:
            self.summary = self.summary_note = None
            layout.addWidget(steps_box)

        steps.addWidget(self._step(1, "Створи свого бота", w.label(
            'Відкрий <a href="https://t.me/BotFather">@BotFather</a> у Telegram → «Start» → надішли <b>/newbot</b>. '
            "Назва — будь-яка, ім'я — латиницею й закінчується на «bot» (наприклад, ivan_autopara_bot). "
            "У відповідь прийде токен — довгий рядок на кшталт 123456789:AAH…", "muted", rich=True)))

        self.token = QLineEdit(model.get("telegram.token") or "")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("123456789:AAH…")
        self.token.textEdited.connect(self._token_edited)
        self.check_button = w.button("Перевірити", "", self._check)
        self.bot_state = w.label("", "muted")
        token_box = QWidget()
        token_layout = QVBoxLayout(token_box)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.addLayout(w.hbox(self.token, self.check_button))
        token_layout.addWidget(self.bot_state)
        steps.addWidget(self._step(2, "Встав токен", token_box))

        self.connect_button = w.button("Підключити", "primary", self._pair)
        self.test_button = w.button("Надіслати тест", "", self._test)
        self.pair_state = w.label("", "muted")
        self.code = w.label("", "code")
        self.code.hide()
        pair_box = QWidget()
        pair_layout = QVBoxLayout(pair_box)
        pair_layout.setContentsMargins(0, 0, 0, 0)
        pair_layout.addWidget(w.label("Відкриється твій бот — натисни в ньому «Start». Сповіщення отримуватиме лише той, "
                                      "хто це зробить.", "muted"))
        pair_layout.addLayout(w.hbox(self.connect_button, self.test_button, "stretch"))
        pair_layout.addWidget(self.pair_state)
        pair_layout.addWidget(self.code)
        steps.addWidget(self._step(3, "Підключи свій Telegram", pair_box))

        if extended:
            more = w.More("Що бот робить")
            for path, title, hint in (
                ("telegram.commands", "Відповідати на команди й кнопки", "«статус», «чат», «екран», «напиши …», «вийди», кнопки «Написати «+»?»."),
                ("telegram.mentions", "Сповіщати, коли тебе кличуть", "З цитатою й кнопками-відповідями."),
                ("telegram.clips", "Відеовирізка моменту", "Хвилина відео з чатом (і звуком), коли тебе згадали."),
                ("telegram.reports", "Звіт після пари", "Про що була пара, завдання й дедлайни, чи тебе кликали."),
                ("telegram.weekly", "Підсумок тижня", "У неділю ввечері: що пройшли з кожного предмета, завдання й дедлайни, що далі."),
                ("telegram.joins", "Повідомляти, коли зайшов чи вийшов з пари", "Якщо зайти не вдалося — повідомить завжди."),
                ("telegram.desktop", "Дублювати сповіщення на робочий стіл", ""),
            ):
                more.add(w.switch_row(model, path, title, hint))
            layout.addWidget(more)
        self._draw()
        if self.token.text():
            self._check(quiet=True)

    @staticmethod
    def _step(number: int, title: str, content: QWidget) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        circle = QLabel(str(number))
        circle.setFixedSize(28, 28)
        circle.setProperty("badge", "accent")
        circle.setStyleSheet("border-radius: 14px; font-weight: 700; padding: 0; qproperty-alignment: AlignCenter;")
        layout.addWidget(circle, 0)
        column = QVBoxLayout()
        column.setSpacing(6)
        head = w.label(title)
        head.setStyleSheet("font-weight: 700;")
        column.addWidget(head)
        column.addWidget(content)
        layout.addLayout(column, 1)
        return box

    def _token_edited(self, text: str) -> None:
        self.model.set("telegram.token", text.strip() or None)
        self.model.set("telegram.chat_id", None)
        self.username = ""
        self.bot_state.setText("")
        self._draw()

    def _check(self, quiet: bool = False) -> None:
        token = self.token.text().strip()
        if not token:
            return
        if not quiet:
            self.bot_state.setText("Перевіряю…")

        def ok(data: dict) -> None:
            self.username = data.get("username", "")
            self.bot_state.setText(f"✓ Бот знайдено: @{self.username}")
            if self.summary is not None and self.model.get("telegram.chat_id"):
                self.summary.setText(f"✓ Telegram підключено: @{self.username}")
            self._draw()

        self.bridge.call_api(app_module.api_telegram_check, body={"token": token}, ok=ok,
                             fail=lambda m: self.bot_state.setText(f"⚠ {m}"))

    def _draw(self) -> None:
        connected = bool(self.model.get("telegram.chat_id"))
        self.connect_button.setEnabled(bool(self.username))
        self.connect_button.setText("Підключити заново" if connected else "Підключити")
        self.test_button.setVisible(connected)
        if connected and not self.code.isVisible():
            self.pair_state.setText("✓ Підключено — сповіщення приходитимуть у Telegram")
        elif not self.code.isVisible():
            self.pair_state.setText("" if self.username else "Спершу встав токен і натисни «Перевірити».")

    def _pair(self) -> None:
        token = self.token.text().strip()
        code = f"{secrets.randbelow(10**6):06d}"
        self.code.setText(code)
        self.code.show()
        self.connect_button.setEnabled(False)
        self.pair_state.setText(f"Чекаю, поки ти натиснеш «Start» у @{self.username} (до 3 хвилин). "
                                "Telegram на телефоні? Знайди там бота й надішли йому цей код:")
        open_url(f"https://t.me/{self.username}?start={code}")

        def ok(data: dict) -> None:
            self.code.hide()
            if not data.get("chat_id"):
                self.pair_state.setText("⚠ Не дочекався — натисни «Підключити» ще раз")
                self._draw()
                return
            self.model.set("telegram.chat_id", int(data["chat_id"]))
            self.model.save_now()
            self._draw()
            self.pair_state.setText("✓ Підключено! Бот уже привітався в Telegram")
            self.connected.emit()

        def fail(message: str) -> None:
            self.code.hide()
            self.pair_state.setText(f"⚠ {message}")
            self._draw()

        self.bridge.call_api(app_module.api_telegram_pair, body={"token": token, "code": code}, ok=ok, fail=fail)

    def _test(self) -> None:
        body = {"token": self.token.text().strip(), "chat_id": self.model.get("telegram.chat_id")}
        note = self.summary_note if self.summary_note is not None and self.summary_note.isVisible() else self.pair_state
        note.setText("Надсилаю…")
        self.bridge.call_api(app_module.api_telegram_test, body=body, ok=lambda _: note.setText("✓ Надіслано — перевір Telegram"),
                             fail=lambda m: note.setText(f"⚠ {m}"))
