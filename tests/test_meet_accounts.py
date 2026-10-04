"""Нестандартні ситуації входу в Meet (окремо від test_meet_joiner: кожен тест зі своїм браузером)."""

import pytest

pytest.importorskip("playwright")

from autopara.config import JoinSettings  # noqa: E402
from autopara.joiner import Joiner  # noqa: E402

from .test_meet_joiner import FAKE_MEET, visit  # noqa: E402


def test_other_signed_in_account_is_tried_when_meet_refuses(tmp_path, caplog):
    """Університетський акаунт не пускають на зовнішню зустріч — програма сама заходить з gmail."""
    settings = JoinSettings(meet_account="st1234567@stud.duikt.edu.ua", accounts=["denys.personal@gmail.com"])
    joiner = Joiner(settings, tmp_path / "profile", headless=True)
    try:
        joiner.browser.context().route(
            "https://meet.google.com/**", lambda route: route.fulfill(body=FAKE_MEET, content_type="text/html; charset=utf-8"))
        session = joiner.join(visit("only-gmail"))
        assert "authuser=denys.personal%40gmail.com" in session.page.url and session.page.evaluate("window.joinedWith")["mic"] == "true"
        assert "пробую з denys.personal@gmail.com" in caplog.text
    finally:
        joiner.close()


def test_brain_picks_an_unknown_join_button_and_it_is_remembered(tmp_path, caplog, monkeypatch):
    caplog.set_level("INFO")
    """Google перейменував кнопку — мозок обирає її зі списку; мікрофона й камери він у списку не бачить."""
    from autopara import joiner as joiner_module

    monkeypatch.setattr(joiner_module, "monotonic", lambda clock=iter(range(0, 10**6, 30)): next(clock))  # «застрягли»
    asked = []

    def choose(question, options):
        asked.append(options)
        return options.index("Увійти до зустрічі")

    settings = JoinSettings()
    joiner = Joiner(settings, tmp_path / "profile", headless=True, chooser=choose)
    joiner.browser.close_if_idle = lambda: None  # імітація Meet живе в цьому вікні
    try:
        joiner.browser.context().route(
            "https://meet.google.com/**", lambda route: route.fulfill(body=FAKE_MEET, content_type="text/html; charset=utf-8"))
        session = joiner.join(visit("renamed"))
        assert session.page.evaluate("window.joinedWith")["mic"] == "true"  # перед входом мікрофон таки вимкнено
        assert asked and not any("microphone" in o.lower() or "camera" in o.lower() for o in asked[0])
        assert "Запам'ятав кнопку входу «Увійти до зустрічі»" in caplog.text
        joiner.leave(session)

        asked.clear()
        session = joiner.join(visit("renamed"))  # удруге — без мозку
        assert asked == [] and session.page.evaluate("window.joinedWith")["cam"] == "true"
    finally:
        joiner.close()
