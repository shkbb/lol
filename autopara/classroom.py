"""Google Classroom через браузер програми — там уже виконано вхід в університетський акаунт.

API Classroom дозволяє здавати лише ті завдання, які створила та сама програма через API, а викладачі
створюють їх вручну. Тож читаємо й здаємо так само, як людина, — у браузері. Від верстки залежимо
мінімально: завдання шукаємо за стабільними адресами (``/c/<курс>/a/<завдання>``), текст умови
розбирає агент, а кнопки — за назвами українською й англійською.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

log = logging.getLogger(__name__)

CLASSROOM = "https://classroom.google.com"
ITEM_RE = re.compile(r"/c/([\w-]+)/a/([\w-]+)")
DRIVE_FILE_RE = re.compile(r"drive\.google\.com/(?:file/d/|open\?id=)([\w-]+)")
DOCS_RE = re.compile(r"docs\.google\.com/(document|spreadsheets|presentation|forms)/d/(?:e/)?([\w-]+)")
FORMS_RE = re.compile(r"forms\.gle/|docs\.google\.com/forms/")

ADD_RE = re.compile(r"Додати або створити|Add or create", re.I)
FILE_RE = re.compile(r"^\s*(Файл|File)\s*$", re.I)
BROWSE_RE = re.compile(r"Огляд|Browse|Вибрати файли|Select files|Вибрати файл", re.I)
TURN_IN_RE = re.compile(r"^\s*(Здати|Turn in|Позначити як виконане|Mark as done)\s*$", re.I)
CANCEL_RE = re.compile(r"^\s*(Скасувати|Cancel)\s*$", re.I)
UNSUBMIT_RE = re.compile(r"Скасувати здачу|Unsubmit", re.I)
TURNED_IN_RE = re.compile(r"Здано|Turned in|Скасувати здачу|Unsubmit|Оцінено|Graded|Повернуто|Returned", re.I)
LOGIN_HOSTS = ("accounts.google.com",)

EXPORTS = {
    "document": ("https://docs.google.com/document/d/{id}/export?format=docx", ".docx"),
    "spreadsheets": ("https://docs.google.com/spreadsheets/d/{id}/export?format=xlsx", ".xlsx"),
    "presentation": ("https://docs.google.com/presentation/d/{id}/export/pptx", ".pptx"),
}


class ClassroomError(Exception):
    pass


@dataclass
class Attachment:
    url: str
    title: str = ""
    kind: str = "link"  # drive | document | spreadsheets | presentation | forms | link
    file_id: str = ""

    def download_url(self, account: str) -> tuple[str, str] | None:
        """Звідки завантажити й яке розширення дати, якщо його немає в назві."""
        if self.kind == "drive":
            url = f"https://drive.usercontent.google.com/download?id={self.file_id}&export=download&confirm=t"
            return f"{url}&authuser={quote(account)}", ""
        if self.kind in EXPORTS:
            template, extension = EXPORTS[self.kind]
            return template.format(id=self.file_id) + f"&authuser={quote(account)}", extension
        return None


@dataclass
class Assignment:
    course_id: str
    item_id: str
    title: str = ""
    course: str = ""
    listing: str = ""  # як завдання виглядає у списку «Завдання» (назва, курс, термін)
    text: str = ""  # текст сторінки завдання — умову з нього розбере агент
    attachments: list[Attachment] = field(default_factory=list)
    status: str = ""

    @property
    def key(self) -> str:
        return f"{self.course_id}/{self.item_id}"

    @property
    def path(self) -> str:
        return f"/c/{self.course_id}/a/{self.item_id}/details"

    @property
    def is_quiz(self) -> bool:
        """Тести й опитування (Google Forms) — це не робота для агента."""
        return any(a.kind == "forms" for a in self.attachments)


@dataclass
class SubmitResult:
    ok: bool
    note: str
    screenshot: bytes | None = None


def classify_link(url: str, title: str = "") -> Attachment | None:
    if FORMS_RE.search(url):
        match = DOCS_RE.search(url)
        return Attachment(url, title, "forms", match.group(2) if match else "")
    if match := DRIVE_FILE_RE.search(url):
        return Attachment(url, title, "drive", match.group(1))
    if match := DOCS_RE.search(url):
        return Attachment(url, title, match.group(1), match.group(2))
    if url.startswith("http") and "google.com" not in url.split("/")[2]:
        return Attachment(url, title, "link")
    return None


class Classroom:
    def __init__(self, new_page, account: str, context=None):
        self.new_page = new_page  # () -> Page у браузері програми
        self.account = account
        self.context = context  # () -> BrowserContext, щоб брати кукі без нової вкладки

    # --- Читання ------------------------------------------------------------------

    def _open(self, path: str) -> Page:
        page = self.new_page()
        separator = "&" if "?" in path else "?"
        page.goto(f"{CLASSROOM}{path}{separator}authuser={quote(self.account)}", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        if any(host in page.url for host in LOGIN_HOSTS):
            page.close()
            raise ClassroomError(f"увійди в {self.account} у браузері програми («Акаунти» → «Увійти в Google»)")
        return page

    def todo(self) -> list[Assignment]:
        """Усі призначені й ще не здані завдання з усіх курсів."""
        page = self._open("/a/not-turned-in/all")
        try:
            for _ in range(6):  # список дорендерюється під час прокручування
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(500)
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.getAttribute('href'), e.innerText || ''])"
            )
        finally:
            page.close()
        found: dict[str, Assignment] = {}
        for href, text in links:
            match = ITEM_RE.search(href or "")
            if not match:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            item = found.setdefault(f"{match.group(1)}/{match.group(2)}", Assignment(match.group(1), match.group(2)))
            if lines and not item.listing:
                item.listing = " · ".join(lines)
                item.title = lines[0]
                item.course = lines[1] if len(lines) > 1 else ""
        return list(found.values())

    def details(self, item: Assignment) -> Assignment:
        """Сторінка завдання: текст, вкладення, стан здачі."""
        page = self._open(item.path)
        try:
            main = page.locator("main, [role=main]").first
            item.text = (main.inner_text() if main.count() else page.locator("body").inner_text()).strip()
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.href, (e.innerText || e.getAttribute('aria-label') || '').trim()])"
            )
            title = page.title().split(" - ")[0].strip()
            if title and not item.title:
                item.title = title
        finally:
            page.close()
        seen = set()
        item.attachments = []
        for url, text in links:
            attachment = classify_link(url, text.splitlines()[0] if text else "")
            if attachment is None or (attachment.file_id or attachment.url) in seen:
                continue
            seen.add(attachment.file_id or attachment.url)
            item.attachments.append(attachment)
        item.status = "turned_in" if TURNED_IN_RE.search(item.text) else "assigned"
        return item

    def cookies(self) -> list[dict]:
        """Кукі Google з браузера програми — щоб завантажувати файли у фоні, не займаючи браузер."""
        if self.context is not None:
            return [c for c in self.context().cookies() if "google" in c.get("domain", "")]
        page = self.new_page()
        try:
            return [c for c in page.context.cookies() if "google" in c.get("domain", "")]
        finally:
            page.close()

    # --- Здача --------------------------------------------------------------------

    def submit(self, item: Assignment, files: list[Path], dry_run: bool = False) -> SubmitResult:
        """Прикріпити файли й здати. ``dry_run`` — дійти до самого підтвердження й натиснути «Скасувати»."""
        page = self._open(item.path)
        try:
            text = page.locator("body").inner_text()
            if UNSUBMIT_RE.search(text):
                return SubmitResult(True, "уже здано", page.screenshot(full_page=True))
            missing = [path for path in files if path.name not in text]
            if missing:
                self._attach(page, missing)
            turn_in = page.get_by_role("button", name=TURN_IN_RE).first
            turn_in.wait_for(timeout=15_000)
            turn_in.click()
            dialog = page.get_by_role("dialog").last
            dialog.wait_for(timeout=10_000)
            if dry_run:
                shot = page.screenshot(full_page=True)
                dialog.get_by_role("button", name=CANCEL_RE).first.click()
                return SubmitResult(True, "пробна здача: усе готово, але «Здати» не натискав", shot)
            dialog.get_by_role("button", name=TURN_IN_RE).first.click()
            page.get_by_text(UNSUBMIT_RE).first.wait_for(timeout=30_000)
            return SubmitResult(True, "здано", page.screenshot(full_page=True))
        except PlaywrightError as e:
            shot = None
            try:
                shot = page.screenshot(full_page=True)
            except PlaywrightError:
                pass
            return SubmitResult(False, f"не вийшло здати: {str(e).splitlines()[0]}", shot)
        finally:
            page.close()

    def _attach(self, page: Page, files: list[Path]) -> None:
        page.get_by_role("button", name=ADD_RE).first.click()
        page.get_by_role("menuitem", name=FILE_RE).first.click()
        page.wait_for_timeout(2500)  # вікно вибору файлу (Google Picker) — окремий iframe
        paths = [str(path) for path in files]
        try:
            with page.expect_file_chooser(timeout=20_000) as chooser:
                self._click_browse(page)
            chooser.value.set_files(paths)
        except PlaywrightError:
            inputs = [frame.locator("input[type=file]") for frame in page.frames]
            target = next((locator for locator in inputs if locator.count()), None)
            if target is None:
                raise
            target.first.set_input_files(paths)
        for path in files:  # чекаємо, поки файли завантажаться й з'являться в «Ваша робота»
            page.get_by_text(path.name).first.wait_for(timeout=180_000)

    @staticmethod
    def _click_browse(page: Page) -> None:
        for frame in page.frames:
            button = frame.get_by_role("button", name=BROWSE_RE)
            if button.count():
                button.first.click()
                return
        raise PlaywrightError("не знайшов кнопку вибору файлу")

    def unsubmit(self, item: Assignment) -> SubmitResult:
        page = self._open(item.path)
        try:
            page.get_by_role("button", name=UNSUBMIT_RE).first.click()
            dialog = page.get_by_role("dialog").last
            dialog.wait_for(timeout=10_000)
            dialog.get_by_role("button", name=UNSUBMIT_RE).first.click()
            page.get_by_role("button", name=TURN_IN_RE).first.wait_for(timeout=30_000)
            return SubmitResult(True, "здачу скасовано", page.screenshot(full_page=True))
        except PlaywrightError as e:
            return SubmitResult(False, f"не вийшло скасувати здачу: {str(e).splitlines()[0]}")
        finally:
            page.close()


def download(url: str, folder: Path, cookies: list[dict], fallback_name: str, session=None) -> Path:
    """Завантажити файл з Google з кукі браузера (у фоновому потоці)."""
    import requests

    session = session or requests.Session()
    for cookie in cookies:
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
    response = session.get(url, timeout=120, stream=True)
    response.raise_for_status()
    if "text/html" in response.headers.get("Content-Type", "") and "export" not in url:
        raise ClassroomError("Google віддав сторінку замість файла — можливо, немає доступу")
    name = fallback_name
    disposition = response.headers.get("Content-Disposition", "")
    if match := re.search(r"filename\*=UTF-8''([^;]+)", disposition):
        from urllib.parse import unquote

        name = unquote(match.group(1))
    elif match := re.search(r'filename="([^"]+)"', disposition):
        name = match.group(1)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or fallback_name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    with path.open("wb") as f:
        for chunk in response.iter_content(1 << 16):
            f.write(chunk)
    return path
