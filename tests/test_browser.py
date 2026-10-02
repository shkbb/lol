import json

import pytest

pytest.importorskip("playwright")

from autopara.joiner import Browser  # noqa: E402


def test_bad_browser_path_falls_back_to_bundled_chromium(tmp_path, caplog):
    browser = Browser(tmp_path, channel="", headless=True, executable_path="/nonexistent/chromium")
    try:
        page = browser.new_page()
        page.set_content("<p>ok</p>")
        assert page.inner_text("p") == "ok"
        assert "/nonexistent/chromium" in caplog.text
    finally:
        browser.close()


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
