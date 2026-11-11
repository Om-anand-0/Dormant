from pathlib import Path

import pytest

from dormant import config, platform


@pytest.fixture
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "data"
    monkeypatch.setenv("DORMANT_DATA_DIR", str(path))
    return path


def test_data_dir_override_created(data: Path) -> None:
    assert config.data_dir() == data
    assert data.is_dir()


def test_index_path(data: Path) -> None:
    assert config.index_path() == data / "index.sqlite3"


@pytest.mark.parametrize("macos", [True, False])
def test_archive_dir(data: Path, monkeypatch: pytest.MonkeyPatch, macos: bool) -> None:
    monkeypatch.setattr(platform, "IS_MACOS", macos)
    path = config.archive_dir()
    assert path == data / "archives"
    assert path.is_dir()
    assert (path / ".metadata_never_index").exists() is macos
    config.archive_dir()  # idempotent


def test_data_dir_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DORMANT_DATA_DIR", raising=False)
    monkeypatch.setattr(config.platformdirs, "user_data_path", lambda *a, **k: tmp_path / "d")
    assert config.data_dir() == tmp_path / "d"
