"""Windows і кирилиця в імені користувача: моделі розпізнавання — у теці з латинським шляхом."""

from pathlib import Path

from autopara import winpaths


def test_cyrillic_home_on_windows_moves_model_cache_to_a_latin_path(monkeypatch, tmp_path):
    monkeypatch.setattr(winpaths.sys, "platform", "win32")
    monkeypatch.delenv("HF_HOME", raising=False)
    public = tmp_path / "Public"
    folder = winpaths.ascii_model_cache(home=Path("C:/Users/Денис"), public=public)
    assert folder == public / "autopara" / "huggingface" and folder.is_dir()
    assert winpaths.os.environ["HF_HOME"] == str(folder)


def test_latin_home_other_systems_and_own_setting_are_left_alone(monkeypatch, tmp_path):
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.setattr(winpaths.sys, "platform", "win32")
    assert winpaths.ascii_model_cache(home=Path("C:/Users/denys"), public=tmp_path) is None
    monkeypatch.setattr(winpaths.sys, "platform", "linux")
    assert winpaths.ascii_model_cache(home=Path("/home/денис"), public=tmp_path) is None
    monkeypatch.setattr(winpaths.sys, "platform", "win32")
    monkeypatch.setenv("HF_HOME", "D:/models")
    assert winpaths.ascii_model_cache(home=Path("C:/Users/Денис"), public=tmp_path) is None
