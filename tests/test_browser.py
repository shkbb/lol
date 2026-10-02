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
