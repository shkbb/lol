"""Перетворює розклад на список «візитів»: куди, коли зайти і коли вийти."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from .config import JoinSettings
from .links import LinkRule, platform_of, resolve
from .rozklad import Lesson

# Пари з одним посиланням, між якими перерва не довша за це, проводимо в одному дзвінку.
# Лише коли виходимо за розкладом: з розумним виходом кожна пара — окремий візит,
# а фоновий цикл сам продовжить відкриту зустріч, якщо на перерві з неї не вийшли.
MERGE_GAP = timedelta(minutes=30)


@dataclass
class Visit:
    url: str
    platform: str  # "meet" | "zoom" | "other"
    lessons: list[Lesson]
    join_at: datetime
    leave_at: datetime

    @property
    def key(self) -> str:
        first = self.lessons[0]
        return f"{first.day}|{first.start}|{self.url}"

    @property
    def title(self) -> str:
        return " + ".join(dict.fromkeys(lesson.title for lesson in self.lessons))


@dataclass
class Plan:
    visits: list[Visit] = field(default_factory=list)
    unlinked: list[Lesson] = field(default_factory=list)  # пари, для яких немає посилання
    conflicts: list[str] = field(default_factory=list)


def build_plan(
    lessons: list[Lesson],
    rules: list[LinkRule],
    settings: JoinSettings,
    tz: tzinfo,
    now: datetime,
    use_schedule_links: bool = True,
) -> Plan:
    plan = Plan()
    before = timedelta(minutes=settings.minutes_before)
    after = timedelta(minutes=settings.leave_after_minutes)

    for lesson in sorted(lessons, key=lambda lesson: (lesson.day, lesson.start)):
        resolution = resolve(lesson, rules, use_schedule_links)
        if resolution.source == "skip":
            continue
        if resolution.url is None:
            if lesson.ends_at(tz) > now:
                plan.unlinked.append(lesson)
            continue
        visit = Visit(
            url=resolution.url,
            platform=platform_of(resolution.url),
            lessons=[lesson],
            join_at=lesson.starts_at(tz) - before,
            leave_at=lesson.ends_at(tz) + after,
        )
        previous = plan.visits[-1] if plan.visits else None
        if previous is None:
            plan.visits.append(visit)
            continue
        last = previous.lessons[-1]
        gap = lesson.starts_at(tz) - last.ends_at(tz)
        if visit.url == previous.url and (gap < timedelta(0) or (not settings.smart_leave and gap <= MERGE_GAP)):
            previous.lessons.append(lesson)
            previous.leave_at = max(previous.leave_at, visit.leave_at)
        elif lesson.starts_at(tz) < last.ends_at(tz):
            # Дві різні пари одночасно (зазвичай — дві підгрупи). Лишаємо першу.
            plan.conflicts.append(
                f"{lesson.day:%d.%m} {lesson.start:%H:%M}: «{lesson.title}» накладається на "
                f"«{last.title}» — заходжу на «{last.title}». Додай skip-правило або вкажи student."
            )
        else:
            # Пари не перетинаються, перетинаються лише відступи — виходимо з попередньої раніше.
            previous.leave_at = min(previous.leave_at, visit.join_at)
            plan.visits.append(visit)

    plan.visits = [visit for visit in plan.visits if visit.leave_at > now]
    return plan
