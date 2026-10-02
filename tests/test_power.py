import io
import subprocess

from autopara import power


class FakeProcess:
    def __init__(self, args, fails=False):
        self.args = args
        self.running = not fails
        self.stderr = io.StringIO("Failed to connect to bus" if fails else "")

    def poll(self):
        return None if self.running else 1

    def terminate(self):
        self.running = False

    def wait(self, timeout=None):
        if self.running and timeout is not None:
            raise subprocess.TimeoutExpired("systemd-inhibit", timeout)
        return 1


def test_sleep_guard_holds_and_releases(monkeypatch):
    started = []
    monkeypatch.setattr(power.sys, "platform", "linux")
    monkeypatch.setattr(power.shutil, "which", lambda name: "/usr/bin/systemd-inhibit")
    monkeypatch.setattr(power.subprocess, "Popen", lambda args, **kw: started.append(FakeProcess(args)) or started[-1])
    guard = power.SleepGuard()
    guard.hold(True)
    guard.hold(True)  # уже тримаємо — другий процес не потрібен
    assert len(started) == 1 and "--what=sleep:idle" in started[0].args
    assert f"--pid={power.os.getpid()}" in started[0].args  # помре разом з autopara
    guard.hold(False)
    assert not started[0].running and not guard.holding


def test_sleep_guard_is_a_no_op_without_systemd(monkeypatch):
    monkeypatch.setattr(power.shutil, "which", lambda name: None)
    guard = power.SleepGuard()
    guard.hold(True)
    assert not guard.holding


def test_sleep_guard_gives_up_quietly_when_inhibit_fails(monkeypatch, caplog):
    started = []
    monkeypatch.setattr(power.sys, "platform", "linux")
    monkeypatch.setattr(power.shutil, "which", lambda name: "/usr/bin/systemd-inhibit")
    monkeypatch.setattr(
        power.subprocess, "Popen", lambda args, **kw: started.append(FakeProcess(args, fails=True)) or started[-1]
    )
    guard = power.SleepGuard()
    for _ in range(3):
        guard.hold(True)
    assert len(started) == 1 and not guard.holding
    assert "Failed to connect to bus" in caplog.text
