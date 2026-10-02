import json
import sys
import textwrap

from autopara.agent import Agent, AgentError, parse_output
from autopara.config import AgentSettings


def test_parse_output_variants():
    assert parse_output('{"result": "Готово", "is_error": false}') == ("Готово", False)
    assert parse_output('{"response": "ok"}') == ("ok", False)
    assert parse_output('{"message": {"content": "привіт"}}') == ("привіт", False)
    stream = "\n".join([json.dumps({"type": "start"}), json.dumps({"type": "result", "result": "фініш"})])
    assert parse_output(stream) == ("фініш", False)
    assert parse_output('{"is_error": true, "error": "quota"}') == ("quota", True)
    assert parse_output("просто текст") == ("просто текст", False)


def fake_agy(tmp_path, body):
    """Маленька програма, що прикидається agy: пише файл і відповідає JSON."""
    script = tmp_path / "fake_agy.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    launcher = tmp_path / "agy"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)
    return str(launcher)


def test_agent_runs_in_task_folder_and_counts_usage(tmp_path):
    command = fake_agy(tmp_path, """
        import json, pathlib, sys
        task = pathlib.Path("TASK.md").read_text(encoding="utf-8")
        pathlib.Path("out").mkdir(exist_ok=True)
        pathlib.Path("out/report.txt").write_text("звіт: " + task, encoding="utf-8")
        print(json.dumps({"result": "Зроблено: " + sys.argv[-1]}))
    """)
    work = tmp_path / "task"
    work.mkdir()
    (work / "TASK.md").write_text("Лабораторна №3", encoding="utf-8")
    agent = Agent(AgentSettings(command=command, args=["--output-format", "json"], daily_limit=2), tmp_path / "usage.json")
    result = agent.run("Прочитай TASK.md і виконай", work)
    assert result.ok and result.text == "Зроблено: Прочитай TASK.md і виконай"
    assert (work / "out" / "report.txt").read_text(encoding="utf-8") == "звіт: Лабораторна №3"
    assert agent.used_today() == 1
    agent.run("ще", work)
    try:
        agent.run("і ще", work)
    except AgentError as e:
        assert "ліміт" in str(e)
    else:
        raise AssertionError("ліміт не спрацював")


def test_agent_failure_is_reported(tmp_path):
    command = fake_agy(tmp_path, """
        import sys
        print("compilation failed", file=sys.stderr)
        sys.exit(3)
    """)
    agent = Agent(AgentSettings(command=command, args=[]), tmp_path / "usage.json")
    result = agent.run("x", tmp_path)
    assert not result.ok and "compilation failed" in result.error
    assert agent.problem() is None  # звичайний провал — не пауза


def test_quota_and_login_pause_instead_of_failing(tmp_path):
    command = fake_agy(tmp_path, """
        import sys
        print(sys.argv[-1], file=sys.stderr)
        sys.exit(1)
    """)
    agent = Agent(AgentSettings(command=command, args=[]), tmp_path / "usage.json")
    for message, expected in (("429 RESOURCE_EXHAUSTED: quota", "ліміт"), ("Error: not logged in", "увійти")):
        agent.paused_until = None
        try:
            agent.run(message, tmp_path)
        except AgentError as e:
            assert expected in str(e)
        else:
            raise AssertionError("мала бути пауза")
        assert expected in agent.problem()


def test_cancel_stops_the_agent_and_its_children(tmp_path):
    import threading
    import time

    command = fake_agy(tmp_path, """
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        time.sleep(60)
    """)
    agent = Agent(AgentSettings(command=command, args=[]), tmp_path / "usage.json")
    results = []
    worker = threading.Thread(target=lambda: results.append(agent.run("x", tmp_path)))
    started = time.monotonic()
    worker.start()
    time.sleep(1)
    agent.cancel()
    worker.join(10)
    assert results and not results[0].ok and "зупинено" in results[0].error
    assert time.monotonic() - started < 10


def test_missing_agent_is_explained(tmp_path):
    agent = Agent(AgentSettings(command="agy-not-installed-here"), tmp_path / "usage.json")
    assert "Antigravity" in agent.problem()
