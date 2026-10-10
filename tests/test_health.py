"""Сторож: мовчазні проблеми — у Telegram з порадою, що робити; нагадування не частіше ніж раз на 12 год."""

import os

from autopara import health
from autopara.health import Health, Problem


def test_problem_is_told_once_with_a_fix_reminded_later_and_resolved():
    sent, now, broken = [], [0.0], [True]
    problem = Problem("disk", "Мало місця на диску: вільно 1.2 ГБ.", "звільни місце.", "Місця знову достатньо.")
    guard = Health(sent.append, [lambda: problem if broken[0] else None], repeat=3600, clock=lambda: now[0])
    guard.step()
    assert sent == ["⚠️ Мало місця на диску: вільно 1.2 ГБ.\n🛠 Що зробити: звільни місце."]
    now[0] = 600
    guard.step()
    assert len(sent) == 1  # не спамимо кожні 10 хв
    now[0] = 3700
    guard.step()
    assert len(sent) == 2  # але й не забуваємо
    broken[0] = False
    guard.step()
    assert sent[-1] == "✅ Місця знову достатньо."


def test_ollama_is_reported_only_when_it_went_down(tmp_path):
    up = [False]
    check = health.ollama_check(lambda: up[0])
    assert check() is None and check() is None  # не встановлена взагалі — мовчимо (про це каже вікно програми)
    up[0] = True
    assert check() is None
    up[0] = False
    assert check() is None  # один збій — ще не проблема
    problem = check()
    assert problem.key == "ollama" and "Ollama" in problem.fix


def test_stuck_refine_queue_and_stale_schedule(tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir()
    job = audio / "lesson.json"
    job.write_text("{}")
    (audio / "old.result.json").write_text("[]")
    os.utime(job, (1000, 1000))
    assert health.refine_check(audio, clock=lambda: 1000 + 23 * 3600)() is None
    stuck = health.refine_check(audio, clock=lambda: 1000 + 25 * 3600)()
    assert "25 год" in stuck.text and "не уточнювати" in stuck.fix
    cache = tmp_path / "schedule-cache.json"
    cache.write_text("{}")
    os.utime(cache, (1000, 1000))
    assert health.schedule_check(cache, clock=lambda: 1000 + 47 * 3600)() is None
    assert "збереженою копією" in health.schedule_check(cache, clock=lambda: 1000 + 49 * 3600)().text
    assert health.disk_check(tmp_path, min_gb=10**9)().key == "disk" and health.disk_check(tmp_path, min_gb=0)() is None
