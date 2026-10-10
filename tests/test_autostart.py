import plistlib
import types

from autopara import autostart


def test_linux_autostart_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(autostart.sys, "executable", "/home/me/my apps/autopara/.venv/bin/python")
    assert autostart.status() == {"supported": True, "enabled": False, "note": ""}
    assert autostart.set_enabled(True)["enabled"]
    text = (tmp_path / "autostart" / "autopara.desktop").read_text(encoding="utf-8")
    assert 'Exec="/home/me/my apps/autopara/.venv/bin/python" -P -m autopara app --hidden\n' in text
    assert not autostart.set_enabled(False)["enabled"]


def test_existing_systemd_service_is_left_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    unit = tmp_path / "systemd" / "user" / "autopara.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n")
    state = autostart.set_enabled(True)
    assert state["supported"] is False and state["enabled"] and "systemd" in state["note"]
    assert not (tmp_path / "autostart" / "autopara.desktop").exists()  # другої копії не додаємо


def test_windows_run_key(monkeypatch, tmp_path):
    values = {}

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def query(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1

    def delete(key, name):
        if name not in values:
            raise FileNotFoundError(name)
        del values[name]

    fake = types.SimpleNamespace(
        HKEY_CURRENT_USER="HKCU",
        REG_SZ=1,
        OpenKey=lambda root, path: Key(),
        CreateKey=lambda root, path: Key(),
        QueryValueEx=query,
        SetValueEx=lambda key, name, reserved, kind, value: values.__setitem__(name, value),
        DeleteValue=delete,
    )
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(autostart.sys, "platform", "win32")
    monkeypatch.setattr(autostart, "_winreg", lambda: fake)
    monkeypatch.setattr(autostart.sys, "executable", str(scripts / "python.exe"))
    assert autostart.status()["enabled"] is False
    assert autostart.set_enabled(True)["enabled"] is True
    assert values["autopara"].endswith('pythonw.exe -P -m autopara app --hidden')  # без вікна консолі
    assert autostart.set_enabled(False)["enabled"] is False
    assert autostart.set_enabled(False)["enabled"] is False  # повторне вимкнення не падає


def test_macos_launch_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(autostart.sys, "platform", "darwin")
    monkeypatch.setattr(autostart.Path, "home", lambda: tmp_path)
    assert autostart.set_enabled(True)["enabled"]
    agent = plistlib.loads((tmp_path / "Library" / "LaunchAgents" / "ua.autopara.plist").read_bytes())
    assert agent["RunAtLoad"] and agent["ProgramArguments"][-3:] == ["autopara", "app", "--hidden"]
    assert not autostart.set_enabled(False)["enabled"]


def test_old_autostart_entry_is_rewritten_with_safe_command(monkeypatch, tmp_path):
    """Старий рядок «python -m autopara» з домашньої теки брав теку ~/autopara за пакет — програма не стартувала."""
    monkeypatch.setattr(autostart.sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(autostart.sys, "executable", "/home/me/autopara/.venv/bin/python")
    entry = tmp_path / "autostart" / "autopara.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nExec=/home/me/autopara/.venv/bin/python -m autopara app --hidden\n", encoding="utf-8")
    assert autostart.refresh()
    assert "Exec=/home/me/autopara/.venv/bin/python -P -m autopara app --hidden" in entry.read_text(encoding="utf-8")
    assert not autostart.refresh()  # уже як треба
