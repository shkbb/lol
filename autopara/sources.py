"""Звідки брати розклад: ДУІКТ чи КПІ — однаковий інтерфейс для решти програми."""

from __future__ import annotations

import logging

from .config import Config
from .kpi import API as KPI_API
from .kpi import KpiClient, group_from_link
from .rozklad import BASE_URL as DUIKT_URL
from .rozklad import RozkladClient, RozkladError, Target

log = logging.getLogger(__name__)


def make_source(config: Config):
    """Клієнт розкладу для університету з налаштувань."""
    schedule = config.schedule
    if schedule.university == "kpi":
        return KpiClient(schedule.base_url or KPI_API)
    return RozkladClient(schedule.base_url or DUIKT_URL)


def resolve_target(client, config: Config) -> Target:
    """Чий розклад тягнемо — з налаштувань (за збереженими ID або пошуком за назвою)."""
    s = config.schedule
    if s.university == "kpi":
        uid = s.group_uid or group_from_link(s.group)
        if uid:
            target = client.group_by_uid(uid)
            return target if target.group_name else Target(0, 0, 0, group_name=s.group, uid=uid)
        if not s.group:
            raise RozkladError("група не вказана — вибери її у вікні програми")
        return client.find_group(s.group)
    if s.faculty_id and s.course and s.group_id:
        target = Target(s.faculty_id, s.course, s.group_id, group_name=s.group)
    elif s.group:
        target = client.find_group(s.group, s.faculty_id, s.course)
    else:
        raise RozkladError("група не вказана — вибери її у вікні програми")
    if s.student:
        try:
            target = client.resolve_student(target, s.student)
        except RozkladError as e:
            # Розклад групи кращий, ніж жодного: підгрупи не врахуємо, але пари не пропустимо.
            log.warning("%s — беру розклад усієї групи", e)
    return target
