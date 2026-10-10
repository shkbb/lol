"""Автооновлення: друзі не перекачують програму — вона сама ставить нову версію, коли немає пари."""

import io
import json
import sys
import zipfile

import pytest

from autopara import updater
from autopara.updater import Release, Updater, apply, download, is_newer, latest, parse_version, restore, take_done


def archive(version="0.2.0", prefix="autopara/", extra=None):
    """Архів як у релізі (git archive --prefix=autopara/)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(f"{prefix}pyproject.toml", f'[project]\nversion = "{version}"\n')
        zf.writestr(f"{prefix}autopara/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr(f"{prefix}autopara/new_module.py", "X = 1\n")
        zf.writestr(f"{prefix}README.md", "нове")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buffer.getvalue()


class Session:
    def __init__(self, release=None, blob=b""):
        self.release, self.blob = release, blob

    def get(self, url, timeout=None, headers=None, stream=False):
        session = self

        class Response:
            def json(self):
                return session.release

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                yield session.blob

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return Response()


RELEASE = {"tag_name": "v0.2.0", "body": "Вуха не глухнуть мовчки.",
           "assets": [{"name": "autopara.zip", "browser_download_url": "https://example/autopara.zip"}]}


def test_versions_and_latest_release():
    assert parse_version("v0.10.1") > parse_version("0.9.9") and parse_version("0.2") == (0, 2)
    release = latest(Session(RELEASE))
    assert release == Release("0.2.0", "https://example/autopara.zip", "Вуха не глухнуть мовчки.")
    assert is_newer(release, "0.1.0") and not is_newer(release, "0.2.0")
    assert latest(Session({"tag_name": "v0.3.0", "assets": []})) is None  # реліз без архіву — не наш


def test_download_checks_it_is_really_our_program(tmp_path):
    release = Release("0.2.0", "https://example/autopara.zip")
    new = download(release, tmp_path / "a", Session(blob=archive()))
    assert (new / "autopara" / "__init__.py").read_text().strip() == '__version__ = "0.2.0"'
    with pytest.raises(ValueError):
        download(release, tmp_path / "b", Session(blob=archive(version="0.1.9")))  # в архіві інша версія
    with pytest.raises(ValueError):
        download(release, tmp_path / "c", Session(blob=archive(prefix="evil/")))  # чужий архів
    with pytest.raises(ValueError):
        download(release, tmp_path / "d", Session(blob=archive(extra={"autopara/../../escape.txt": "x"})))


def install_dir(tmp_path):
    install = tmp_path / "install"
    (install / "autopara").mkdir(parents=True)
    (install / "autopara" / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (install / "autopara" / "old_module.py").write_text("OLD = 1\n")
    (install / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n')
    (install / ".venv").mkdir()
    (install / ".venv" / "python").write_text("venv")
    (install / "profiles").mkdir()
    (install / "profiles" / "my.toml").write_text("мій профіль")
    return install


def test_apply_replaces_program_files_and_keeps_the_rest(tmp_path):
    install = install_dir(tmp_path)
    new = download(Release("0.2.0", "x"), tmp_path / "work", Session(blob=archive()))
    deps_changed = apply(new, install, tmp_path / "backup")
    assert deps_changed  # pyproject змінився — треба доставити залежності
    assert (install / "autopara" / "new_module.py").exists() and not (install / "autopara" / "old_module.py").exists()
    assert (install / ".venv" / "python").read_text() == "venv" and (install / "profiles" / "my.toml").exists()
    restore(install, tmp_path / "backup")  # нова не стартувала — повертаємо стару
    assert (install / "autopara" / "old_module.py").exists() and not (install / "autopara" / "new_module.py").exists()
    assert '"0.1.0"' in (install / "autopara" / "__init__.py").read_text()


def make_updater(tmp_path, quiet=True, enabled=True, git=False):
    install = install_dir(tmp_path)
    if git:
        (install / ".git").mkdir()
    quits = []
    up = Updater(tmp_path / "data", lambda: enabled, lambda: quiet, lambda: quits.append(True), install=install,
                 session=Session(RELEASE, archive()))
    return up, install, quits


def test_update_waits_for_a_free_moment_then_installs_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    launched = []
    monkeypatch.setattr(updater, "detached", lambda command: launched.append(command))
    monkeypatch.setattr(updater, "install_dependencies", lambda install: launched.append(["pip"]))

    up, install, quits = make_updater(tmp_path, quiet=False)
    assert up.step() == "є версія 0.2.0 — оновлюсь, коли не буде пари" and quits == [] and not launched

    up, install, quits = make_updater(tmp_path / "2", quiet=True)
    up.data_dir.mkdir(parents=True)
    assert up.step() == "оновлююсь до 0.2.0"
    assert (install / "autopara" / "new_module.py").exists() and quits == [True]
    assert launched[0] == ["pip"] and launched[1][:5] == [sys.executable, "-P", "-m", "autopara.updater", "finish"]
    assert "Вуха не глухнуть" in take_done(up.data_dir) and take_done(up.data_dir) == ""  # сказати лише раз


def test_git_copy_and_disabled_updates_are_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "__version__", "0.1.0")
    up, _, quits = make_updater(tmp_path, git=True)
    assert up.step().startswith("копія з git")
    up, _, quits = make_updater(tmp_path / "2", enabled=False)
    assert up.step() == "є версія 0.2.0 — автооновлення вимкнене" and quits == []


def test_finish_rolls_back_when_the_new_version_does_not_start(tmp_path, monkeypatch):
    install = install_dir(tmp_path)
    new = download(Release("0.2.0", "x"), install / ".update", Session(blob=archive()))
    apply(new, install, install / ".update" / "backup")
    data = tmp_path / "data"
    data.mkdir()
    started = []

    class Child:
        def poll(self):
            return 1  # нова копія одразу впала

        def terminate(self):
            pass

    monkeypatch.setattr(updater, "detached", lambda command: started.append(command) or Child())
    assert updater.finish(wait_pid=999999, version="0.2.0", data_dir=data, install=install, start_timeout=5) == 1
    assert (install / "autopara" / "old_module.py").exists() and len(started) == 2  # відкотили й запустили стару
    assert take_done(data).startswith("⚠️ Не вдалося оновитися до 0.2.0")

    healthy = tmp_path / "data2"
    healthy.mkdir()

    class Fine(Child):
        def poll(self):
            (healthy / updater.HEALTH).write_text(json.dumps({"version": "0.2.0"}))
            return None

    monkeypatch.setattr(updater, "detached", lambda command: Fine())
    assert updater.finish(wait_pid=999999, version="0.2.0", data_dir=healthy, install=install, start_timeout=10) == 0
