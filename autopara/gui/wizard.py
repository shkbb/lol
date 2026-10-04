"""Майстер першого запуску: група → посилання → Google → Telegram → готово. Кожен крок зберігається одразу."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout, QWidget

from .. import app as app_module
from . import widgets as w
from .panels import AccountsPanel, GroupPanel, LinksPanel, TelegramPanel

STEPS = ["Група", "Посилання", "Google", "Telegram", "Готово"]


class Wizard(QWidget):
    finished = Signal()

    def __init__(self, model, bridge):
        super().__init__()
        self.model, self.bridge = model, bridge
        self.setObjectName("root")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("sidebar")
        head = QHBoxLayout(header)
        head.setContentsMargins(28, 14, 28, 14)
        head.addWidget(w.label("autopara", "brand", wrap=False))
        head.addSpacing(24)
        self.dots: list[QLabel] = []
        for index, title in enumerate(STEPS, 1):
            dot = QLabel(f"{index}  {title}")
            dot.setProperty("badge", "plain")
            self.dots.append(dot)
            head.addWidget(dot)
        head.addStretch(1)
        layout.addWidget(header)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)

        footer = QFrame()
        footer.setObjectName("sidebar")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(28, 12, 28, 12)
        self.back = w.button("← Назад", "", self._back)
        self.skip = w.button("Пропустити", "", self._next)
        self.next = w.button("Далі →", "big", self._next)
        foot.addWidget(self.back)
        foot.addStretch(1)
        foot.addWidget(self.skip)
        foot.addWidget(self.next)
        layout.addWidget(footer)

        self.accounts: AccountsPanel | None = None
        self._build()
        model.replaced.connect(self._imported)

    def _build(self) -> None:
        while self.stack.count():
            self.stack.removeWidget(self.stack.widget(0))
        self.stack.addWidget(self._group_step())
        self.stack.addWidget(self._links_step())
        self.stack.addWidget(self._google_step())
        self.stack.addWidget(self._telegram_step())
        self.stack.addWidget(self._done_step())
        self.go(0)

    def go(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        for number, dot in enumerate(self.dots):
            dot.setProperty("badge", "accent" if number == index else "ok" if number < index else "plain")
            w.restyle(dot)
        self.back.setVisible(0 < index < len(STEPS) - 1)
        self.skip.setVisible(index == 3)
        self.next.setText({0: "Далі →", 1: "Далі →", 2: "Далі →", 3: "Далі →", 4: "Почати"}[index])
        self._update_next()
        if index == 1:
            self.links.reload()
        if index == len(STEPS) - 1:
            self._turn_on()

    def _update_next(self) -> None:
        index = self.stack.currentIndex()
        if index == 0:
            self.next.setEnabled(bool(self.model.get("schedule.group") or self.model.get("schedule.group_uid")))
        else:
            self.next.setEnabled(True)

    def _next(self) -> None:
        index = self.stack.currentIndex()
        self.model.save_now(wait=True)  # наступний крок спирається на збережене (розклад обраної групи)
        if index == len(STEPS) - 1:
            self.finished.emit()
        else:
            self.go(index + 1)

    def _back(self) -> None:
        self.go(max(self.stack.currentIndex() - 1, 0))

    def update_state(self, state: dict) -> None:
        if self.accounts is not None:
            self.accounts.update_state(state)

    # --- Кроки ----------------------------------------------------------------------------

    def _group_step(self) -> QWidget:
        page = w.Page("Привіт! Налаштуємо за хвилину", "Обери свій університет і групу — програма сама візьме розклад "
                                                         "і заходитиме на пари замість тебе.")
        card = w.Card()
        group = GroupPanel(self.model, self.bridge)
        group.chosen.connect(self._update_next)
        card.add(group)
        page.add(card)
        ready = w.Card("Маю готові налаштування", "Профіль чи файл налаштувань від одногрупника — і нічого не заповнювати.")
        buttons = [w.button(f"Профіль «{name}»", "", lambda name=name: self._import({"profile": name}))
                   for name in self.model.meta.get("profiles") or []]
        buttons.append(w.button("Відкрити файл…", "", self._import_file))
        ready.add(w.hbox(*buttons, "stretch"))
        self.import_note = w.label("", "hint")
        ready.add(self.import_note)
        page.add(ready)
        return page

    def _links_step(self) -> QWidget:
        page = w.Page("Посилання на пари", "Це предмети з твого розкладу. Встав посилання з Meet чи Zoom, яке дав викладач. "
                                            "Чого ще немає — можна додати пізніше в «Налаштуваннях».")
        card = w.Card()
        self.links = LinksPanel(self.model, self.bridge)
        card.add(self.links)
        page.add(card)
        return page

    def _google_step(self) -> QWidget:
        page = w.Page("Вхід у Google", "Програма заходить на пари у своєму браузері, тож туди треба один раз увійти. "
                                        "Найкраще — в університетську пошту.")
        card = w.Card()
        self.accounts = AccountsPanel(self.model, self.bridge)
        card.add(self.accounts)
        page.add(card)
        return page

    def _telegram_step(self) -> QWidget:
        page = w.Page("Telegram — за бажанням", "Свій бот напише, коли тебе кличуть на парі, і пришле звіт після неї. "
                                                 "Можна пропустити й налаштувати пізніше.")
        card = w.Card()
        telegram = TelegramPanel(self.model, self.bridge)
        card.add(telegram)
        page.add(card)
        return page

    def _done_step(self) -> QWidget:
        page = w.Page("Готово 🎉", "")
        card = w.Card()
        self.done_text = w.label("", "big")
        self.done_note = w.label("", "muted")
        card.add(self.done_text)
        card.add(self.done_note)
        page.add(card)
        return page

    def _turn_on(self) -> None:
        self.model.save_now(wait=True)
        try:
            auto = self.bridge.api(app_module.api_autostart, body={"enabled": True})
            self.model.meta["autostart"] = auto
        except Exception:  # noqa: BLE001 — автозапуск можна ввімкнути й пізніше в «Загальному»
            auto = {"enabled": False}
        self.bridge.command("start", timeout=300)
        self.done_text.setText("Програма працює й заходитиме на пари сама.")
        lines = ["Мікрофон і камера на парах завжди вимкнені."]
        if auto.get("enabled"):
            lines.append("Вона запускатиметься разом із комп'ютером — нічого відкривати не треба.")
        lines.append("Закриєш це вікно — програма працюватиме далі у фоні. Знову відкрити: ярлик autopara.")
        self.done_note.setText("\n".join(lines))

    # --- Імпорт ---------------------------------------------------------------------------

    def _import(self, body: dict) -> None:
        try:
            result = self.bridge.api(app_module.api_import, body=body)
        except Exception as e:  # noqa: BLE001
            self.import_note.setText(f"⚠ {e}")
            return
        self.model.replace(result["config"])

    def _imported(self) -> None:
        self._build()
        self.go(2)  # група й посилання вже є — далі вхід у Google
        self.import_note.setText("✓ Налаштування імпортовано")

    def _import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Файл налаштувань autopara", str(Path.home()), "Налаштування (*.toml)")
        if path:
            self._import({"toml": Path(path).read_text(encoding="utf-8")})
