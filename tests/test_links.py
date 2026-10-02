from autopara.links import (
    LinkRule,
    meet_url,
    normalize_url,
    platform_of,
    resolve,
    zoom_app_url,
    zoom_web_url,
)

MEET = "https://meet.google.com/aaa-bbbb-ccc"
ZOOM = "https://us05web.zoom.us/j/12345678901?pwd=AbC"


def test_first_matching_rule_wins(lesson):
    rules = [
        LinkRule(subject="бази даних", type="лк", url=MEET),
        LinkRule(subject="Бази даних", url=ZOOM),
    ]
    assert resolve(lesson(type="Лк"), rules).url == MEET
    assert resolve(lesson(type="Лб т.2"), rules).url == ZOOM
    assert resolve(lesson(subject="Філософія"), rules).source == "none"


def test_teacher_filter_and_apostrophes(lesson):
    rules = [LinkRule(subject="Об'єктно", teacher="петренко", url=MEET)]
    assert resolve(lesson(subject="Обʼєктно-орієнтоване", teacher="Петренко П.П."), rules).url == MEET
    assert resolve(lesson(subject="Обʼєктно-орієнтоване", teacher="Іваненко І.І."), rules).url is None


def test_skip_rule(lesson):
    result = resolve(lesson(subject="Фізичне виховання", link=MEET), [LinkRule(subject="фізичне", skip=True)])
    assert (result.url, result.source) == (None, "skip")


def test_schedule_link_is_fallback_only_for_meet_and_zoom(lesson):
    assert resolve(lesson(link=ZOOM), []).source == "schedule"
    assert resolve(lesson(link=ZOOM), [], use_schedule_links=False).source == "none"
    assert resolve(lesson(link="https://moodle.duikt.edu.ua/course/1"), []).source == "none"
    assert resolve(lesson(link=ZOOM), [LinkRule(subject="бази", url=MEET)]).url == MEET


def test_platform_detection_and_normalization():
    assert normalize_url("meet.google.com/aaa-bbbb-ccc") == MEET
    assert platform_of("meet.google.com/aaa-bbbb-ccc") == "meet"
    assert platform_of(ZOOM) == "zoom"
    assert platform_of("https://zoom.us/my/teacher") == "zoom"
    assert platform_of("https://notzoom.us/j/1") == "other"


def test_meet_account():
    assert meet_url(MEET) == MEET
    assert meet_url(MEET, "me@duikt.edu.ua") == MEET + "?authuser=me%40duikt.edu.ua"


def test_zoom_urls():
    assert zoom_app_url(ZOOM) == "zoommtg://us05web.zoom.us/join?action=join&confno=12345678901&pwd=AbC"
    assert zoom_app_url("https://zoom.us/j/987654321") == "zoommtg://zoom.us/join?action=join&confno=987654321"
    assert zoom_app_url("https://zoom.us/my/teacher") is None
    assert zoom_web_url(ZOOM) == "https://app.zoom.us/wc/join/12345678901?pwd=AbC"
    assert zoom_web_url("https://zoom.us/my/teacher") == "https://zoom.us/my/teacher"


def test_alternatives_and_short_subject(lesson):
    rules = [LinkRule(subject="ЯТПЗ|Якість", type="Лк", url=MEET), LinkRule(subject="ЯТПЗ|Якість", url=ZOOM)]
    assert resolve(lesson(subject="Якість та тестування ПЗ", type="Лк"), rules).url == MEET
    assert resolve(lesson(subject="Невідома повна назва", short_subject="ЯТПЗ", type="Пз"), rules).url == ZOOM
    assert resolve(lesson(subject="Бази даних"), rules).source == "none"


def test_link_account_wins_over_default_account():
    personal = meet_url(MEET, "personal@gmail.com")
    assert meet_url(personal, "st@stud.duikt.edu.ua") == personal
