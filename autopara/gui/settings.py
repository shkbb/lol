"""«Налаштування»: вкладки Пари / Акаунти / Помічник / Telegram / Загальне. Усе зберігається саме."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QFileDialog, QHBoxLayout, QLineEdit, QTabWidget, QVBoxLayout, QWidget

from .. import app as app_module
from . import widgets as w
from .panels import AccountsPanel, GroupPanel, LinksPanel, TelegramPanel

TAB_MARGINS = (2, 14, 2, 24)
TABS = [("pairs", "Пари"), ("accounts", "Акаунти"), ("assistant", "Помічник"), ("telegram", "Telegram"), ("general", "Загальне")]


class SettingsPage(QWidget):
    def __init__(self, model, bridge, window):
        super().__init__()
        self.model, self.bridge, self.window = model, bridge, window
        self.setObjectName("root")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 0)
        layout.setSpacing(12)
        layout.addWidget(w.label("Налаштування", "h1"))
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.tabBar().setExpanding(False)
        layout.addWidget(self.tabs)
        self.accounts: AccountsPanel | None = None
        self.build()
        model.replaced.connect(self.build)

    def build(self) -> None:
        current = self.tabs.currentIndex()
        self.tabs.clear()
        for key, title in TABS:
            self.tabs.addTab(getattr(self, f"_{key}")(), title)
        self.tabs.setCurrentIndex(max(current, 0))

    def open_tab(self, key: str) -> None:
        keys = [k for k, _ in TABS]
        if key in keys:
            self.tabs.setCurrentIndex(keys.index(key))

    def update_state(self, state: dict) -> None:
        if self.accounts is not None:
            self.accounts.update_state(state)

    # --- Вкладки ----------------------------------------------------------------------

    def _pairs(self) -> QWidget:
        page = w.Page("", "", margins=TAB_MARGINS)
        group_card = w.Card("Університет і група")
        group = GroupPanel(self.model, self.bridge, extended=True)
        group_card.add(group)
        page.add(group_card)
        links_card = w.Card()
        head = QHBoxLayout()
        head.addWidget(w.label("Посилання на пари", "h2"), 1)
        links = LinksPanel(self.model, self.bridge, extended=True)
        head.addWidget(w.button("Оновити", "", links.reload))
        links_card.add(head)
        links_card.add(links)
        page.add(links_card)
        group.chosen.connect(lambda: setattr(links, "_pending", True))
        return page

    def _accounts(self) -> QWidget:
        page = w.Page("", "", margins=TAB_MARGINS)
        card = w.Card("Google", "З якого акаунта програма заходить на пари в Meet.")
        self.accounts = AccountsPanel(self.model, self.bridge, extended=True)
        card.add(self.accounts)
        page.add(card)
        return page

    def _assistant(self) -> QWidget:
        model = self.model
        page = w.Page("", "Стежить за парою, поки тебе немає: відмічається на перекличці, кличе тебе, коли звертаються, "
                          "шле звіт після пари.", margins=TAB_MARGINS)
        card = w.Card()
        card.add(w.text_field(model, "me.name", "Твоє прізвище та ім'я", "Як тебе називають викладачі — щоб помічати, коли кличуть.",
                              "Прізвище Ім'я"))
        card.add(w.label("Писати в чат пари самому", "h2"))
        card.add(w.choice_field(model, "me.chat_reply", [
            ("auto", "Так, де все зрозуміло", "Перекличка, «… ви тут?». Коли не впевнений — спитає в Telegram."),
            ("ask", "Лише після мого «так» у Telegram", "Кожен раз питає кнопками."),
            ("off", "Ніколи", "Лише сповіщає."),
        ], default="auto"))
        page.add(card)

        ears = w.Card()
        ears.add(w.switch_row(model, "ears.enabled", "Слухати пару",
                              "Розпізнає мову на цьому комп'ютері (нікуди не надсилає): чує, коли тебе кличуть, і складає звіт."))
        if not model.meta.get("ears_installed"):
            install = w.button("Встановити розпізнавання мови (~200 МБ)", "primary")
            note = w.label("", "hint")

            def do_install() -> None:
                install.setEnabled(False)
                note.setText("Встановлюю… 1–3 хвилини")
                self.bridge.call_api(app_module.api_install_ears, ok=lambda _: note.setText("✓ Готово — модель завантажиться сама"),
                                     fail=lambda m: (install.setEnabled(True), note.setText(f"⚠ {m}")))

            install.clicked.connect(do_install)
            ears.add(w.hbox(install, "stretch"))
            ears.add(note)
        ears.add(w.switch_row(model, "brain.enabled", "Мозок (локальна мовна модель)",
                              "Через Ollama: розбирається в незвичних ситуаціях і пише підсумок пари у звіті."))
        more = w.More()
        more.add(w.text_field(model, "me.present", "Що писати на перекличці", placeholder="Присутній"))
        more.add(w.text_field(model, "me.here", "Відповідь на «…, ви тут?»", placeholder="Так, я тут"))
        more.add(w.field("Кнопки-відповіді в Telegram", w.ChipsField(model, "me.quick_replies", "текст і Enter"),
                         "Коли тебе кличуть, під сповіщенням будуть ці кнопки: натиснув — і текст пішов у чат пари."))
        more.add(w.switch_row(model, "me.roll_call", "Відмічатися на перекличці", "«+», «Присутній» чи прізвище, коли відмічається група."))
        more.add(w.switch_row(model, "me.answer_here", "Відповідати, коли викладач питає саме тебе", "«Ляшко, ви тут?» → «Так, я тут»."))
        more.add(w.switch_row(model, "me.polite", "Дублювати за групою «Дякую», «Добрий день»"))
        more.add(w.label("На запис, голосування, «хто здав» чи «зрозуміло?» помічник сам не відповідає ніколи — це вирішуєш ти.", "hint"))
        more.add(w.select_field(model, "ears.model", "Розпізнавання наживо", [
            ("auto", "автоматично за потужністю комп'ютера"), ("small", "small — швидка"), ("medium", "medium"),
            ("large-v3-turbo", "large-v3-turbo — точна"),
        ], "Наживо головне — встигати за парою, щоб помітити, що тебе кличуть.", default="auto"))
        more.add(w.select_field(model, "ears.refine_model", "Уточнення після пари (для звіту)", [
            ("auto", "автоматично (large-v3 на потужному комп'ютері)"), ("large-v3", "large-v3 — найточніша"),
            ("large-v3-turbo", "large-v3-turbo"), ("off", "не уточнювати — звіт одразу"),
        ], "Після пар програма ще раз уважно розшифровує весь запис — звіт приходить пізніше, зате точніший.",
            default="auto"))
        if platform_is_linux():
            more.add(w.select_field(model, "ears.source", "Пари в браузері: звідки брати звук", [
                ("auto", "звук браузера програми (рекомендовано)"), ("browser", "зі сторінки пари (запасний спосіб)"),
            ], "У браузері програми відкрита лише пара, тож вуха чують саме її.", default="auto"))
        else:
            more.add(self._zoom_cable())
        more.add(w.text_field(model, "brain.model", "Модель мозку", placeholder="gemma3:4b"))
        check = w.button("Перевірити мозок і вуха")
        result = w.label("", "muted")

        def run_check() -> None:
            result.setText("Перевіряю…")

            def show(data: dict) -> None:
                rows = [c for c in data["checks"] if c["area"] in ("мозок", "вуха", "вуха в Zoom", "відеовирізки")]
                result.setText("\n".join(f"{'✓' if c['ok'] else '·' if c['ok'] is None else '✗'} {c['area']}: {c['text']}" for c in rows))

            self.bridge.call_api(app_module.api_check, ok=show, fail=lambda m: result.setText(f"⚠ {m}"))

        check.clicked.connect(run_check)
        more.add(w.hbox(check, "stretch"))
        more.add(result)
        ears.add(more)
        page.add(ears)
        return page

    def _zoom_cable(self) -> QWidget:
        """Windows і macOS: щоб вуха чули Zoom-клієнт, Zoom виводить звук у віртуальний кабель."""
        model = self.model
        combo = QComboBox()
        current = model.get("ears.app_device") or ""
        combo.addItem("автоматично (знайду встановлений кабель сам)", "")
        if current:
            combo.addItem(current, current)
            combo.setCurrentIndex(1)

        def loaded(data: dict) -> None:
            combo.blockSignals(True)
            for name in data["devices"]:
                if combo.findData(name) < 0:
                    combo.addItem(name, name)
            combo.blockSignals(False)

        self.bridge.call_api(app_module.api_audio_devices, ok=loaded)
        combo.currentIndexChanged.connect(lambda _: model.set("ears.app_device", combo.currentData() or None))
        mac = sys.platform == "darwin"
        cable = ('<a href="https://existential.audio/blackhole/">BlackHole</a>' if mac
                 else '<a href="https://vb-audio.com/Cable/">VB-Cable</a>')
        where = "BlackHole (або Multi-Output з ним)" if mac else "«CABLE Input»"
        hint = w.label(f"Щоб помічник чув пари в Zoom-клієнті: встанови {cable}, у Zoom → Налаштування → Аудіо → Динамік "
                       f"обери {where}. Без кабеля Zoom відкриватиметься в браузері програми — там помічник його чує.", "hint", rich=True)
        box = w.field("Zoom-клієнт: віртуальний кабель", combo)
        box.layout().addWidget(hint)
        return box

    def _telegram(self) -> QWidget:
        page = w.Page("", "Твій особистий бот: пише, коли тебе кличуть, питає «написати «+»?» кнопками, шле звіт після пари. "
                          "Токен лишається лише на цьому комп'ютері.", margins=TAB_MARGINS)
        card = w.Card()
        card.add(TelegramPanel(self.model, self.bridge, extended=True))
        page.add(card)
        return page

    def _general(self) -> QWidget:
        model = self.model
        page = w.Page("", "", margins=TAB_MARGINS)
        start = w.Card("Запуск")
        auto = model.meta.get("autostart") or {}
        row = w.SwitchRow("Запускати разом із комп'ютером", auto.get("note") or "У фоні, без вікна — заходитиме на пари сама.",
                          bool(auto.get("enabled")))
        row.switch.setEnabled(bool(auto.get("supported", True)))

        def autostart(on: bool) -> None:
            try:
                model.meta["autostart"] = self.bridge.api(app_module.api_autostart, body={"enabled": on})
            except Exception as e:  # noqa: BLE001
                self.window.show_status("error", f"⚠ {e}")

        row.toggled.connect(autostart)
        start.add(row)
        page.add(start)

        lesson = w.Card("На парі")
        lesson.add(w.switch_row(model, "join.mute_microphone", "Вимикати мікрофон",
                                "Якщо не вдасться переконатися, що він вимкнений, браузер не дасть сторінці доступу до нього."))
        lesson.add(w.switch_row(model, "join.camera_off", "Вимикати камеру"))
        lesson.add(w.switch_row(model, "join.mute_sound", "Без звуку", "Вимкни, щоб чути пару з колонок (помічник чутиме однаково)."))
        lesson.add(w.switch_row(model, "join.smart_leave", "Виходити разом з групою, а не рівно за розкладом",
                                "Відпустили раніше — вийде, коли вийде більшість; затягнулась — не вийде першим."))
        more = w.More()
        more.add(w.number_field(model, "join.minutes_before", "За скільки хвилин до пари заходити", minimum=0, maximum=30, suffix=" хв"))
        more.add(w.switch_row(model, "join.auto_leave", "Виходити з пари самому", "Вимкнено — програма лише заходить, а виходиш ти."))
        more.add(w.number_field(model, "join.max_overtime_minutes", "Не довше ніж після кінця пари", maximum=180, suffix=" хв"))
        lesson.add(more)
        page.add(lesson)

        test = w.Card("Перевірка", "Створи свою зустріч на meet.new і встав посилання — програма зайде так само, як на пару.")
        url = QLineEdit()
        url.setPlaceholderText("https://meet.google.com/xxx-xxxx-xxx")
        note = w.label("", "muted")

        def join() -> None:
            note.setText("Заходжу — стежити можна на сторінці «Сьогодні»")
            self.bridge.call_api(app_module.api_join, body={"url": url.text(), "minutes": 60}, fail=lambda m: note.setText(f"⚠ {m}"))

        def check_all() -> None:
            note.setText("Перевіряю…")

            def show(data: dict) -> None:
                note.setText("\n".join(f"{'✓' if c['ok'] else '·' if c['ok'] is None else '✗'} {c['area']}: {c['text']}"
                                       for c in data["checks"]))

            self.bridge.call_api(app_module.api_check, ok=show, fail=lambda m: note.setText(f"⚠ {m}"))

        test.add(w.hbox(url, w.button("Зайти", "primary", join)))
        test.add(w.hbox(w.button("Перевірити все", "", check_all), "stretch"))
        test.add(note)
        page.add(test)

        move = w.Card("Перенести налаштування", "Готовий профіль чи файл налаштувань від одногрупника.")
        buttons = [w.button(f"Профіль «{name}»", "", lambda name=name: self._import({"profile": name}))
                   for name in model.meta.get("profiles") or []]
        buttons.append(w.button("Імпорт з файла…", "", self._import_file))
        move.add(w.hbox(*buttons, "stretch"))
        move.add(w.label(f"Налаштування лежать у {model.meta.get('path', '')}", "hint"))
        page.add(move)

        records = w.Card("Записи пар", "Кожна пара зберігається тут, по теках предметів: підсумок, розшифровка й чат. "
                                        "З них і мозок відповідає на питання в Telegram.")
        records.add(w.hbox(w.button("📂 Відкрити записи пар", "", self._open_records), "stretch"))
        page.add(records)

        program = w.Card("Програма")
        program.add(w.hbox(w.button("Пройти майстер ще раз", "", self.window.show_wizard),
                           w.button("Вийти з програми", "", self.window.quit_program), "stretch"))
        program.add(w.label("«Вийти» — програма перестане заходити на пари до наступного запуску.", "hint"))
        page.add(program)
        return page

    def _open_records(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from .. import notes

        folder = self.bridge.controller.config.data_dir / notes.FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # --- Імпорт ---------------------------------------------------------------------------

    def _import(self, body: dict) -> None:
        try:
            result = self.bridge.api(app_module.api_import, body=body)
        except Exception as e:  # noqa: BLE001
            self.window.show_status("error", f"⚠ {e}")
            return
        self.model.replace(result["config"])
        self.window.show_status("saved", "✓ Налаштування імпортовано")

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Файл налаштувань autopara", str(Path.home()), "Налаштування (*.toml)")
        if path:
            self._import({"toml": Path(path).read_text(encoding="utf-8")})


def platform_is_linux() -> bool:
    return sys.platform.startswith("linux")
