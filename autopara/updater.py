"""Автооновлення для тих, хто поставив програму з архіву (друзі): нічого не треба перекачувати.

Раз на кілька годин програма дивиться останній реліз на GitHub. Є новіша версія — завантажує архів,
перевіряє його, робить резервну копію поточної версії й замінює файли програми (теку .venv, профілі
й налаштування не чіпає; налаштування й записи пар узагалі лежать деінде). Змінились залежності —
доставляє їх. Перезапускається лише тоді, коли найближчим часом немає пари. Окремий процес
(``python -m autopara.updater finish``) дочекається, поки стара копія вийде, запустить нову й переконається,
що вона стартувала; не стартувала — поверне попередню версію.

Копію з git (розробницьку) автооновлення не чіпає — її оновлюють через git.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from . import __version__

log = logging.getLogger(__name__)

REPO = "shkbb/lol"
LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = "autopara.zip"
INSTALL_DIR = Path(__file__).resolve().parent.parent
KEEP = {".venv", ".git", ".update", "profiles", "build", "dist"}  # це не частина архіву програми
HEALTH = "update-health.json"  # нова копія пише, що стартувала
DONE = "update-done.json"  # що змінилось — нова копія скаже в Telegram


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def from_git(install: Path = INSTALL_DIR) -> bool:
    return (install / ".git").exists()


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    notes: str = ""


def latest(session: requests.Session | None = None) -> Release | None:
    """Останній реліз з архівом програми, або None (немає мережі, немає архіву)."""
    session = session or requests.Session()
    try:
        data = session.get(LATEST, timeout=20, headers={"Accept": "application/vnd.github+json"}).json()
    except (requests.RequestException, ValueError) as e:
        log.debug("Не вдалося перевірити оновлення: %s", e)
        return None
    url = next((asset.get("browser_download_url") for asset in data.get("assets") or []
                if asset.get("name") == ASSET), None)
    tag = str(data.get("tag_name") or "")
    if not url or not tag:
        return None
    return Release(tag.lstrip("vV"), url, str(data.get("body") or "")[:1500])


def is_newer(release: Release, current: str = __version__) -> bool:
    return parse_version(release.version) > parse_version(current)


def download(release: Release, folder: Path, session: requests.Session | None = None) -> Path:
    """Завантажити й розпакувати архів у ``folder`` / "new". Перевіряє, що це справді наша програма тієї версії."""
    session = session or requests.Session()
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / ASSET
    with session.get(release.url, timeout=120, stream=True) as response:
        response.raise_for_status()
        with open(archive, "wb") as out:
            for block in response.iter_content(1 << 16):
                out.write(block)
    target = folder / "new"
    shutil.rmtree(target, ignore_errors=True)
    with zipfile.ZipFile(archive) as zf:
        if zf.testzip() is not None:
            raise ValueError("архів пошкоджений")
        names = zf.namelist()
        prefix = "autopara/"
        if not all(name.startswith(prefix) for name in names) or f"{prefix}pyproject.toml" not in names:
            raise ValueError("це не архів autopara")
        for name in names:
            relative = name[len(prefix):]
            if ".." in Path(relative).parts or Path(relative).is_absolute():
                raise ValueError("підозрілий шлях в архіві")  # чужий архів не розпаковуємо взагалі
            if not relative:
                continue
            path = target / relative
            if name.endswith("/"):
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(zf.read(name))
    inside = (target / "autopara" / "__init__.py").read_text(encoding="utf-8")
    if f'"{release.version}"' not in inside:
        raise ValueError(f"в архіві не версія {release.version}")
    return target


def _entries(folder: Path) -> list[Path]:
    return [path for path in folder.iterdir() if path.name not in KEEP and not path.name.endswith(".egg-info")]


def apply(new: Path, install: Path, backup: Path) -> bool:
    """Замінити файли програми новими (з резервною копією). True — змінились залежності (pyproject.toml)."""
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True)
    for path in _entries(install):
        shutil.move(str(path), backup / path.name)
    for path in _entries(new):
        if path.is_dir():
            shutil.copytree(path, install / path.name)
        else:
            shutil.copy2(path, install / path.name)
    old = backup / "pyproject.toml"
    return not old.exists() or old.read_bytes() != (install / "pyproject.toml").read_bytes()


def restore(install: Path, backup: Path) -> None:
    """Повернути попередню версію з резервної копії."""
    for path in _entries(install):
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    for path in backup.iterdir():
        shutil.move(str(path), install / path.name)


def install_dependencies(install: Path) -> None:
    from .accel import pip_extras

    for extra in (f".[{pip_extras()}]", ".[ears]", "."):
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", extra], cwd=install,
                                capture_output=True, text=True)
        if result.returncode == 0:
            return
    log.warning("Не вдалося доставити залежності: %s", result.stderr.strip()[-300:])


def detached(command: list[str]) -> subprocess.Popen:
    """Процес, що переживе програму (вона саме виходить, щоб оновитися)."""
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        return subprocess.Popen(command, creationflags=flags, close_fds=True)
    return subprocess.Popen(command, start_new_session=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Updater:
    """Фоновий потік автооновлення. ``quiet()`` — чи можна зараз перезапускатися (пари немає й не скоро),
    ``quit_app()`` — попросити програму вийти (вона саме оновлюється)."""

    def __init__(self, data_dir: Path, enabled: Callable[[], bool], quiet: Callable[[], bool],
                 quit_app: Callable[[], None], install: Path = INSTALL_DIR, every: float = 6 * 3600,
                 first_after: float = 180, session: requests.Session | None = None):
        self.data_dir = data_dir
        self.enabled = enabled
        self.quiet = quiet
        self.quit_app = quit_app
        self.install = install
        self.every = every
        self.first_after = first_after
        self.session = session
        self.status = "ще не перевіряв"
        self._stop = threading.Event()
        self._now = threading.Event()
        self._thread = threading.Thread(target=self._run, name="updater", daemon=True)

    def start(self) -> None:
        self.mark_healthy()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._now.set()

    def check_now(self) -> None:
        """Кнопка «Перевірити оновлення»."""
        self._now.set()

    def mark_healthy(self) -> None:
        """Ця копія стартувала — процес оновлення (finish) чекає саме на це."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / HEALTH).write_text(json.dumps({"version": __version__, "pid": os.getpid()}),
                                            encoding="utf-8")

    def _run(self) -> None:
        wait = self.first_after
        while not self._stop.is_set():
            self._now.wait(wait)
            self._now.clear()
            if self._stop.is_set():
                return
            wait = self.every
            try:
                if self.step().startswith("є версія") and self.enabled():
                    wait = 600  # нова версія чекає, поки скінчаться пари, — дивимось частіше
            except Exception:  # noqa: BLE001 — оновлення не має ламати програму
                log.exception("Автооновлення не вдалося")
                self.status = "не вдалося — спробую пізніше"

    def step(self) -> str:
        """Одна перевірка: «актуальна», «чекаю, поки скінчаться пари», «оновлююсь» тощо."""
        if from_git(self.install):
            self.status = "копія з git — оновлюється через git"
            return self.status
        release = latest(self.session)
        if release is None:
            self.status = "не вдалося перевірити (немає інтернету?)"
            return self.status
        if not is_newer(release):
            self.status = f"актуальна версія {__version__}"
            return self.status
        if not self.enabled():
            self.status = f"є версія {release.version} — автооновлення вимкнене"
            return self.status
        if not self.quiet():
            self.status = f"є версія {release.version} — оновлюсь, коли не буде пари"
            return self.status
        work = self.install / ".update"
        new = download(release, work, self.session)
        backup = work / "backup"
        log.info("Оновлююсь до %s (зараз %s)", release.version, __version__)
        deps = apply(new, self.install, backup)
        if deps:
            install_dependencies(self.install)
        (self.data_dir / DONE).write_text(json.dumps({"from": __version__, "to": release.version, "notes": release.notes},
                                                     ensure_ascii=False), encoding="utf-8")
        (self.data_dir / HEALTH).unlink(missing_ok=True)
        detached([sys.executable, "-m", "autopara.updater", "finish", "--wait", str(os.getpid()),
                  "--version", release.version, "--data", str(self.data_dir), "--install", str(self.install)])
        self.status = f"оновлююсь до {release.version}"
        self.quit_app()
        return self.status


def finish(wait_pid: int, version: str, data_dir: Path, install: Path, start_timeout: float = 120) -> int:
    """Окремий процес: стара копія вийшла → запустити нову → переконатися, що стартувала, інакше відкотити."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and _alive(wait_pid):
        time.sleep(1)
    app = [sys.executable, "-m", "autopara", "app", "--hidden"]
    child = detached(app)
    health = data_dir / HEALTH
    deadline = time.monotonic() + start_timeout
    while time.monotonic() < deadline:
        try:
            if json.loads(health.read_text(encoding="utf-8")).get("version") == version:
                return 0  # нова версія працює
        except (OSError, ValueError):
            pass
        if child.poll() is not None:
            break
        time.sleep(2)
    # Нова версія не стартувала — повертаємо попередню.
    if child.poll() is None:
        child.terminate()
    backup = install / ".update" / "backup"
    if backup.is_dir():
        restore(install, backup)
    (data_dir / DONE).write_text(json.dumps({"failed": version}, ensure_ascii=False), encoding="utf-8")
    detached(app)
    return 1


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def take_done(data_dir: Path) -> str:
    """Повідомлення про щойно завершене оновлення (один раз), або ''."""
    path = data_dir / DONE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    path.unlink(missing_ok=True)
    if "failed" in data:
        return f"⚠️ Не вдалося оновитися до {data['failed']} — повернув попередню версію, спробую з наступною."
    notes = f"\n\n{data['notes'].strip()}" if data.get("notes", "").strip() else ""
    return f"⬆️ autopara оновилась: {data.get('from', '?')} → {data.get('to', '?')}.{notes}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    done = sub.add_parser("finish")
    done.add_argument("--wait", type=int, required=True)
    done.add_argument("--version", required=True)
    done.add_argument("--data", required=True)
    done.add_argument("--install", required=True)
    args = parser.parse_args(argv)
    return finish(args.wait, args.version, Path(args.data), Path(args.install))


if __name__ == "__main__":
    raise SystemExit(main())

