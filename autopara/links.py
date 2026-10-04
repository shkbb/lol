"""Які посилання відкривати для яких пар і як їх відкривати."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .rozklad import Lesson


@dataclass(frozen=True)
class LinkRule:
    """Правило з конфіга: пари, що підходять під усі задані поля, відкриваються за ``url``.

    У кожному полі можна дати кілька варіантів через «|»: ``subject = "ЯТПЗ|Якість"``.
    ``subject`` порівнюється і з повною, і зі скороченою назвою предмета з розкладу.
    """

    subject: str = ""
    type: str = ""
    teacher: str = ""
    url: str = ""
    skip: bool = False
    account: str = ""  # Google-акаунт для цього посилання (email або номер), якщо не основний

    def matches(self, lesson: Lesson) -> bool:
        return (
            (_contains(lesson.subject, self.subject) or _contains(lesson.short_subject, self.subject))
            and _contains(lesson.type, self.type)
            and _contains(lesson.teacher, self.teacher)
        )


@dataclass(frozen=True)
class Resolution:
    url: str | None
    source: str  # "config" | "schedule" | "skip" | "none"


def _fold(text: str) -> str:
    return " ".join(text.casefold().replace("’", "'").replace("ʼ", "'").split())


def _contains(text: str, needle: str) -> bool:
    if not needle:
        return True
    return any(alternative and _fold(alternative) in _fold(text) for alternative in needle.split("|"))


def resolve(lesson: Lesson, rules: list[LinkRule], use_schedule_links: bool = True) -> Resolution:
    for rule in rules:
        if rule.matches(lesson):
            if rule.skip:
                return Resolution(None, "skip")
            url = normalize_url(rule.url)
            if rule.account and platform_of(url) == "meet":
                url = meet_url(url, rule.account)  # цю зустріч відкриваємо з іншого акаунта
            return Resolution(url, "config")
    # Посилання з самого розкладу беремо лише якщо це справді Meet чи Zoom, а не Moodle тощо.
    if use_schedule_links and lesson.link and platform_of(lesson.link) != "other":
        return Resolution(normalize_url(lesson.link), "schedule")
    return Resolution(None, "none")


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    return url


def platform_of(url: str) -> str:
    host = (urlsplit(normalize_url(url)).hostname or "").lower()
    if host == "meet.google.com":
        return "meet"
    if host == "zoom.us" or host.endswith(".zoom.us"):
        return "zoom"
    return "other"


def meet_url(url: str, account: str = "") -> str:
    """Посилання на Meet; ``account`` обирає Google-акаунт, якщо їх у браузері кілька."""
    url = normalize_url(url)
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    if not account or "authuser" in query:  # акаунт, заданий для конкретного посилання, важливіший
        return url
    query["authuser"] = [account]
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def with_account(url: str, account: str) -> str:
    """Те саме посилання на Meet, але з іншим Google-акаунтом."""
    parts = urlsplit(normalize_url(url))
    query = {key: values for key, values in parse_qs(parts.query).items() if key != "authuser"}
    return meet_url(urlunsplit(parts._replace(query=urlencode(query, doseq=True))), account)


def zoom_meeting(url: str) -> tuple[str, str | None, str | None]:
    """(хост, номер конференції, пароль) з будь-якого посилання на Zoom."""
    parts = urlsplit(normalize_url(url))
    match = re.search(r"(?<!\d)(\d{9,12})(?!\d)", parts.path)
    password = parse_qs(parts.query).get("pwd", [None])[0]
    return (parts.hostname or "zoom.us"), (match.group(1) if match else None), password


def zoom_app_url(url: str) -> str | None:
    """``zoommtg://`` відкриває Zoom-клієнт одразу, без сторінки «Відкрити Zoom?» у браузері."""
    host, meeting, password = zoom_meeting(url)
    if not meeting:
        return None  # персональні посилання на кшталт /my/name — лише через https
    query = {"action": "join", "confno": meeting}
    if password:
        query["pwd"] = password
    return f"zoommtg://{host}/join?{urlencode(query)}"


def zoom_web_url(url: str) -> str:
    """Посилання на веб-клієнт Zoom (заходить у браузері, без встановленого Zoom)."""
    _, meeting, password = zoom_meeting(url)
    if not meeting:
        return normalize_url(url)
    query = f"?{urlencode({'pwd': password})}" if password else ""
    return f"https://app.zoom.us/wc/join/{meeting}{query}"
