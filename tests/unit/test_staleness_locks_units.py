import os
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from dormant import locks
from dormant.staleness import last_activity
from dormant.units import format_size, parse_duration, parse_size

OLD, NEWER, NEWEST = 1_000_000_000.0, 1_500_000_000.0, 1_700_000_000.0


def _file(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_newest_mtime_ignores_targets_and_git(tmp_path: Path) -> None:
    _file(tmp_path / "src" / "a.py", NEWER)
    _file(tmp_path / ".venv" / "x.py", NEWEST)
    _file(tmp_path / ".dormant" / "state", NEWEST)
    assert last_activity(tmp_path, {tmp_path / ".venv"}) == NEWER


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_commit_time_wins_when_newer(tmp_path: Path) -> None:
    _file(tmp_path / "a.py", OLD)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": f"@{int(NEWEST)}",
        "GIT_COMMITTER_DATE": f"@{int(NEWEST)}",
    }
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "x"]):
        subprocess.run([*git, *args], check=True, env=env)  # noqa: S603
    os.utime(tmp_path / "a.py", (OLD, OLD))
    assert last_activity(tmp_path) == NEWEST


def test_no_files_no_git(tmp_path: Path) -> None:
    assert last_activity(tmp_path) == 0.0


def test_in_use_by_cwd(tmp_path: Path) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path)
    try:
        assert locks.in_use(tmp_path)
        assert not locks.in_use(tmp_path / "elsewhere")
    finally:
        proc.kill()
        proc.wait()


def test_inside_prefix_is_path_aware() -> None:
    snap = locks.ProcessSnapshot((os.path.normcase(os.path.abspath("proj-two")),))  # noqa: PTH100
    assert not locks.in_use(Path("proj"), snap)
    assert locks.in_use(Path("proj-two"), snap)


@pytest.mark.parametrize(
    ("text", "days"),
    [("48h", 2), ("60d", 60), ("12w", 84), ("6m", 180), ("1y", 365), (" 2D ", 2)],
)
def test_parse_duration(text: str, days: float) -> None:
    assert parse_duration(text) == timedelta(days=days)


@pytest.mark.parametrize("text", ["", "60", "d", "-1d", "5x", "1.d"])
def test_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="invalid duration"):
        parse_duration(text)


@pytest.mark.parametrize(
    ("text", "size"),
    [("512", 512), ("1KB", 1024), ("200MB", 200 << 20), ("1.5g", 3 << 29), ("2GiB", 2 << 30)],
)
def test_parse_size(text: str, size: int) -> None:
    assert parse_size(text) == size


@pytest.mark.parametrize("text", ["", "GB", "1PB", "1 G B"])
def test_parse_size_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="invalid size"):
        parse_size(text)


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "0 B"), (1023, "1023 B"), (1536, "1.5 KB"), (200 << 20, "200 MB"), (5 << 40, "5.0 TB")],
)
def test_format_size(size: int, text: str) -> None:
    assert format_size(size) == text
