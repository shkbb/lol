"""Лише українська: наживо мозок раз відповів «Тебе кличут, чтобы подтвердить, что ты на паре»."""

from autopara.lang import looks_russian, ukrainian


def test_russian_is_recognised_but_ukrainian_is_not():
    assert looks_russian("Тебе кличут, чтобы подтвердить, что ты на паре. Учитель называет имена.")
    assert not looks_russian("Тебе кличуть, щоб перевірити значення фоторезистора. Завтра контрольна.")
    assert not looks_russian("Все ок, як ти? Що плануєш на завтра?")


def test_russian_reply_is_rewritten_once_or_dropped():
    replies = iter(["Это просто: нужно сделать вот так.", "Це просто: треба зробити отак."])
    asked = []
    assert ukrainian(lambda system, text: asked.append(text) or next(replies), "с", "питання") == "Це просто: треба зробити отак."
    assert "Перепиши її українською" in asked[1]
    assert ukrainian(lambda system, text: "Что это такое?", "с", "питання") is None  # краще промовчати
