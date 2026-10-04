import json

import pytest

pytest.importorskip("playwright")

from autopara.joiner import Browser  # noqa: E402


def test_one_window_one_tab_and_closed_when_lesson_ends(tmp_path):
    browser = Browser(tmp_path, headless=True)
    try:
        page = browser.new_page()  # порожня вкладка нового вікна — без зайвої другої
        assert browser.context().pages == [page]
        page.set_content("<p>ok</p>")
        assert page.inner_text("p") == "ok"
        page.goto("data:text/html,<p>пара</p>")
        browser.close_if_idle()  # у вікні ще пара — не чіпаємо
        assert browser.any_page() is page
        page.close()
        browser.close_if_idle()  # пара скінчилась — вікно закриваємо
        assert browser.any_page() is None
        assert browser.new_page().evaluate("1 + 1") == 2  # наступна пара — вікно відкривається знову
    finally:
        browser.close()


def test_ensure_downloads_browser_only_when_missing(monkeypatch):
    from autopara import browser_setup

    present = {"path": ""}
    installs = []
    monkeypatch.setattr(browser_setup, "installed", lambda: present["path"] or None)

    def install():
        installs.append(1)
        present["path"] = "/tmp/chrome"
        return True, ""

    monkeypatch.setattr(browser_setup, "_install", install)
    assert browser_setup.ensure() and installs == [1] and browser_setup.status()["status"] == "ready"
    assert browser_setup.ensure() and installs == [1]  # уже є — нічого не качаємо

    present["path"] = ""
    monkeypatch.setattr(browser_setup, "_install", lambda: (False, "немає мережі"))
    assert not browser_setup.ensure()
    state = browser_setup.status()
    assert state["status"] == "error" and "інтернет" in state["error"] and "немає мережі" in state["error"]


def test_browser_signin_is_disallowed_so_workspace_accounts_stay_in_profile(tmp_path):
    from autopara.joiner import disallow_browser_signin

    prefs_file = tmp_path / "Default" / "Preferences"
    disallow_browser_signin(tmp_path)  # новий профіль: файла ще немає
    assert json.loads(prefs_file.read_text())["signin"] == {"allowed": False, "allowed_on_next_startup": False}

    signin_on = {"allowed": True, "allowed_on_next_startup": True}
    prefs_file.write_text(json.dumps({"intl": {"accept_languages": "uk"}, "signin": signin_on}))
    disallow_browser_signin(tmp_path)  # Chrome чи людина ввімкнули знову — вимикаємо, решту не чіпаємо
    prefs = json.loads(prefs_file.read_text())
    assert prefs["signin"] == {"allowed": False, "allowed_on_next_startup": False}
    assert prefs["intl"] == {"accept_languages": "uk"}

    prefs_file.write_text("{зіпсований")
    disallow_browser_signin(tmp_path)  # чужий зіпсований файл не перезаписуємо
    assert prefs_file.read_text() == "{зіпсований"
