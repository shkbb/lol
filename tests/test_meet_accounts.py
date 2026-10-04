"""Meet не пускає з основного акаунта — програма пробує інші, в які увійдено (окремо: свій браузер)."""

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
