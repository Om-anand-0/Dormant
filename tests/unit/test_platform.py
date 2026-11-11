from pathlib import Path

import pytest

from dormant import platform


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"C:\x\node_modules", r"\\?\C:\x\node_modules"),
        (r"\\server\share\x", r"\\?\UNC\server\share\x"),
        (r"\\?\C:\already", r"\\?\C:\already"),
        (r"\\?\UNC\server\share", r"\\?\UNC\server\share"),
    ],
)
def test_to_extended(raw: str, expected: str) -> None:
    assert platform._to_extended(raw) == expected


def test_long_path_is_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = platform.long_path(Path("rel"))
    if platform.IS_WINDOWS:
        assert str(result) == "\\\\?\\" + str(tmp_path / "rel")
        assert platform.long_path(result) == result
    else:
        assert result == tmp_path / "rel"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Linux version 5.15.90.1-microsoft-standard-WSL2", True),
        ("Linux version 6.8 generic", False),
    ],
)
def test_is_wsl(monkeypatch: pytest.MonkeyPatch, text: str, expected: bool) -> None:
    monkeypatch.setattr(platform, "IS_LINUX", True)
    monkeypatch.setattr(platform, "_read_proc_version", lambda: text)
    assert platform.is_wsl() is expected


def test_is_wsl_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise OSError("no /proc")

    monkeypatch.setattr(platform, "IS_LINUX", True)
    monkeypatch.setattr(platform, "_read_proc_version", boom)
    assert platform.is_wsl() is False


def test_is_wsl_false_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "IS_LINUX", False)
    assert platform.is_wsl() is False


def test_fsync_dir(tmp_path: Path) -> None:
    platform.fsync_dir(tmp_path)


def test_free_bytes_missing_path(tmp_path: Path) -> None:
    assert platform.free_bytes(tmp_path / "not" / "yet") > 0


def test_symlinks_supported_cleans_up(tmp_path: Path) -> None:
    assert isinstance(platform.symlinks_supported(tmp_path), bool)
    assert list(tmp_path.iterdir()) == []
