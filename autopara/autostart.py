"""Запуск разом із комп'ютером — програма стартує прихованою (``autopara app --hidden``), а ярлик лише показує її вікно.

Windows — запис у реєстрі (HKCU\\...\\Run) через pythonw, тож чорного вікна консолі немає;
Linux із systemd і графічною сесією (GNOME, KDE…) — служба користувача autopara-app.service: стартує з сесією й сама
перезапускається, якщо програма впала; інші Linux — ~/.config/autostart; macOS — LaunchAgent.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

NAME = "autopara"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AGENT_LABEL = "ua.autopara"


def command() -> list[str]:
    python = Path(sys.executable)
    if sys.platform == "win32" and python.with_name("pythonw.exe").exists():
        python = python.with_name("pythonw.exe")  # без вікна консолі
    # -P: не брати поточну теку в sys.path. Автозапуск стартує з домашньої теки, а там лежить тека «autopara» з
    # програмою — і Python брав її за пакет замість коду (нова версія на старті падала: немає __version__).
    return [str(python), "-P", "-m", "autopara", "app", "--hidden"]


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def desktop_file() -> Path:
    return _config_home() / "autostart" / "autopara.desktop"


def systemd_unit() -> Path:
    """Служба, яку налаштували вручну (сам програма її не чіпає)."""
    return _config_home() / "systemd" / "user" / "autopara.service"


SERVICE = "autopara-app.service"
MARK = "# Цим файлом керує autopara («Налаштування» → «Запускати разом із комп'ютером») — не змінюй вручну."


def service_unit() -> Path:
    """Служба, якою керує сама програма."""
    return _config_home() / "systemd" / "user" / SERVICE


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)


def systemd_session() -> bool:
    """Чи є systemd користувача з графічною сесією (GNOME, KDE…): тоді автозапуск — службою з перезапуском."""
    if sys.platform in ("win32", "darwin") or shutil.which("systemctl") is None:
        return False
    try:
        return "ActiveState=active" in _systemctl("show", "graphical-session.target", "-p", "ActiveState").stdout
    except (OSError, subprocess.SubprocessError):
        return False


def managed_service() -> bool:
    """Програма стартує службою autopara-app.service (і перезапускатись теж має нею)."""
    path = service_unit()
    try:
        return path.exists() and MARK in path.read_text(encoding="utf-8")
    except OSError:
        return False


def _systemd_quote(arg: str) -> str:
    if not any(char in arg for char in ' \t"\'\\$%;'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$").replace("%", "%%") + '"'


def _unit_text() -> str:
    return (
        f"{MARK}\n"
        "[Unit]\n"
        "Description=autopara — автоматичний вхід на пари\n"
        "After=graphical-session.target\n"
        "PartOf=graphical-session.target\n\n"
        "[Service]\n"
        f"ExecStart={' '.join(_systemd_quote(arg) for arg in command())}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n\n"
        "[Install]\n"
        "WantedBy=graphical-session.target\n"
    )


def launch_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _winreg():
    import winreg

    return winreg


def _desktop_quote(arg: str) -> str:
    """Аргумент для рядка Exec= у .desktop-файлі."""
    if not any(char in arg for char in ' \t\n"\'\\><~|&;$*?#()`'):
        return arg
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + escaped.replace("\\", "\\\\") + '"'  # .desktop ще раз екранує зворотні скісні


def status() -> dict:
    """{"supported", "enabled", "note"} — для вікна програми."""
    if sys.platform == "win32":
        winreg = _winreg()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, NAME)
            enabled = True
        except OSError:
            enabled = False
        return {"supported": True, "enabled": enabled, "note": ""}
    if sys.platform == "darwin":
        return {"supported": True, "enabled": launch_agent().exists(), "note": ""}
    if managed_service():
        return {"supported": True, "enabled": True, "note": ""}
    if systemd_unit().exists():
        return {
            "supported": False,
            "enabled": True,
            "note": "Уже запускається як systemd-сервіс (autopara.service) — керуй ним через systemctl --user.",
        }
    return {"supported": True, "enabled": desktop_file().exists(), "note": ""}


def set_enabled(enabled: bool) -> dict:
    if sys.platform == "win32":
        winreg = _winreg()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(command()))
            else:
                try:
                    winreg.DeleteValue(key, NAME)
                except FileNotFoundError:
                    pass
    elif sys.platform == "darwin":
        path = launch_agent()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            agent = {"Label": AGENT_LABEL, "ProgramArguments": command(), "RunAtLoad": True}
            path.write_bytes(plistlib.dumps(agent))
        else:
            path.unlink(missing_ok=True)
    elif not systemd_unit().exists() and (managed_service() or (enabled and systemd_session())):
        unit = service_unit()
        if enabled:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(_unit_text(), encoding="utf-8")
            _systemctl("daemon-reload")
            _systemctl("enable", SERVICE)
            desktop_file().unlink(missing_ok=True)  # тепер стартує службою — двічі не треба
        else:
            _systemctl("disable", SERVICE)
            unit.unlink(missing_ok=True)
            _systemctl("daemon-reload")
            desktop_file().unlink(missing_ok=True)
    elif not systemd_unit().exists():
        path = desktop_file()
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=autopara\n"
                "Comment=Автоматичний вхід на пари (у фоні)\n"
                f"Exec={' '.join(_desktop_quote(arg) for arg in command())}\n"
                "Terminal=false\n"
                "X-GNOME-Autostart-enabled=true\n",
                encoding="utf-8",
            )
        else:
            path.unlink(missing_ok=True)
    return status()


def refresh() -> bool:
    """Автозапуск увімкнений, але записаний старою версією (інша команда чи ще не служба) — переписати.
    True — переписали."""
    try:
        if sys.platform not in ("win32", "darwin") and not systemd_unit().exists():
            if managed_service():
                if service_unit().read_text(encoding="utf-8") != _unit_text():
                    set_enabled(True)
                    return True
                return False
            path = desktop_file()
            if path.exists():
                wanted = f"Exec={' '.join(_desktop_quote(arg) for arg in command())}"
                if systemd_session() or wanted not in path.read_text(encoding="utf-8").splitlines():
                    set_enabled(True)  # зі звичайного автозапуску — на службу з перезапуском
                    return True
        elif status().get("enabled"):
            set_enabled(True)  # Windows і macOS: запис дешевий, а команда могла змінитись
    except (OSError, subprocess.SubprocessError):
        return False
    return False
