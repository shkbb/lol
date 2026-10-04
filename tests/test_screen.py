"""Браузер програми у її вікні: невидимий Chromium віддає картинку й вкладки, а мишка й клавіатура з вікна
доходять до сторінки."""

import time

import pytest

pytest.importorskip("playwright")

from autopara.joiner import Browser, chrome_user_agent  # noqa: E402
from autopara.screen import Screen, coalesce_moves  # noqa: E402

PAGE = """<!doctype html><meta charset=utf-8><title>Пара</title>
<body style="margin:0"><button id=b style="position:absolute;left:100px;top:100px;width:120px;height:40px"
onclick="document.title='клікнули'">Кнопка</button><input id=i style="position:absolute;left:100px;top:200px">
<p id=t style="position:absolute;left:100px;top:300px">виділи мене</p>
<a id=pop href="https://meet.google.com/popup" target=_blank style="position:absolute;left:100px;top:400px">нова</a>"""


def pump_until(browser, condition, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        browser.pump(0.1)
        if condition():
            return True
    raise AssertionError("не дочекався")


def test_browser_shows_in_window_and_takes_mouse_and_keys(tmp_path):
    screen = Screen(size=(900, 600))
    frames, tabs, copied = [], [], []
    screen.on_frame = lambda data, meta: frames.append((data, meta))
    screen.on_tabs = lambda items, current: tabs.append((items, current))
    screen.on_copy = copied.append
    browser = Browser(tmp_path, screen=screen)
    try:
        page = browser.new_page()
        # На весь браузер, а не лише на вкладку: нова вкладка теж не повинна йти в справжній інтернет.
        browser.context().route("https://meet.google.com/**", lambda route: route.fulfill(body=PAGE, content_type="text/html"))
        page.goto("https://meet.google.com/abc-defg-hij")
        assert "HeadlessChrome" not in page.evaluate("navigator.userAgent")  # Google бачить звичайний Chrome
        assert page.evaluate("[innerWidth, innerHeight]") == [900, 600]

        screen.watch(True)  # сторінку «Браузер» відкрили — пішла картинка
        pump_until(browser, lambda: frames and tabs and tabs[-1][0] and tabs[-1][0][0]["title"] == "Пара")
        assert frames[-1][0][:2] == b"\xff\xd8"  # JPEG
        assert frames[-1][1]["deviceWidth"] == 900
        assert tabs[-1] == ([{"title": "Пара", "url": "https://meet.google.com/abc-defg-hij", "closable": False}], 0)

        for kind in ("mousePressed", "mouseReleased"):
            screen.send("Input.dispatchMouseEvent", {"type": kind, "x": 150, "y": 120, "button": "left", "clickCount": 1})
        pump_until(browser, lambda: page.title() == "клікнули")

        page.focus("#i")
        screen.send("Input.insertText", {"text": "привіт "})
        screen.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "!", "text": "!"})
        pump_until(browser, lambda: page.input_value("#i") == "привіт !")

        page.evaluate("() => { const r = document.createRange(); r.selectNodeContents(document.getElementById('t'));"
                      " getSelection().removeAllRanges(); getSelection().addRange(r); document.activeElement.blur(); }")
        screen.copy()
        pump_until(browser, lambda: copied == ["виділи мене"])

        screen.resize(700, 500)  # вікно програми змінило розмір — сторінка теж
        pump_until(browser, lambda: page.evaluate("[innerWidth, innerHeight]") == [700, 500])

        page.click("#pop")  # сайт відкрив нову вкладку — як і звичайний браузер, переходимо на неї
        pump_until(browser, lambda: len(tabs[-1][0]) == 2 and tabs[-1][1] == 1)
        assert tabs[-1][0][1]["closable"] and not tabs[-1][0][0]["closable"]  # вкладку пари людина не закриє
        popup = browser.context().pages[1]
        popup.wait_for_load_state()
        assert popup.evaluate("[innerWidth, innerHeight]") == [700, 500]
        screen.command("close", 0)  # спроба закрити пару — нічого
        screen.command("close", 1)
        pump_until(browser, lambda: len(tabs[-1][0]) == 1 and tabs[-1][1] == 0)
        assert not page.is_closed()

        screen.watch(False)  # сторінку «Браузер» сховали — картинка не йде
        browser.pump(0.3)
        count = len(frames)
        page.evaluate("document.body.style.background = 'red'")
        browser.pump(0.5)
        assert len(frames) == count
    finally:
        browser.close()
    assert tabs[-1] == ([], -1)  # браузер закрився — у вікні порожньо


def test_mouse_moves_are_merged_and_user_agent_is_plain_chrome():
    move = ("input", "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 1})
    press = ("input", "Input.dispatchMouseEvent", {"type": "mousePressed", "x": 2})
    last = ("input", "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 3})
    assert coalesce_moves([move, move, press, move, last]) == [move, press, last]
    agent = chrome_user_agent()
    assert agent.startswith("Mozilla/5.0 (") and " Chrome/" in agent and "Headless" not in agent
