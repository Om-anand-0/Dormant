"""Regression tests for the security review: links in the marker path, repo git config."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fixtures.trees import requires_symlinks

from dormant import archiver, marker, scanner, staleness
from dormant.errors import UnsafePathError
from dormant.index import Index


@requires_symlinks
def test_marker_refuses_symlinked_dormant_dir(tmp_path: Path) -> None:
    project, outside = tmp_path / "proj", tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / ".dormant").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        marker._write(project, ["node_modules"])
    assert not (outside / "state").exists()


@requires_symlinks
def test_marker_refuses_symlinked_state_file(tmp_path: Path) -> None:
    project, victim = tmp_path / "proj", tmp_path / "victim.txt"
    (project / ".dormant").mkdir(parents=True)
    victim.write_text("keep me", encoding="utf-8")
    (project / ".dormant" / "state").symlink_to(victim)
    with pytest.raises(UnsafePathError):
        marker._write(project, ["node_modules"])
    assert victim.read_text(encoding="utf-8") == "keep me"


@requires_symlinks
def test_symlinked_git_exclude_is_skipped(tmp_path: Path) -> None:
    project, victim = tmp_path / "proj", tmp_path / "victim.txt"
    (project / ".git" / "info").mkdir(parents=True)
    victim.write_text("keep me\n", encoding="utf-8")
    (project / ".git" / "info" / "exclude").symlink_to(victim)
    marker._write(project, ["node_modules"])  # warns, doesn't raise
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert (project / ".dormant" / "state").is_file()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_repo_fsmonitor_is_never_run(tmp_path: Path, index: Index) -> None:
    """A cloned repo's core.fsmonitor must not execute during scan or hibernate."""
    repo, pwned = tmp_path / "repo", tmp_path / "pwned"
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "index.js").write_text("1", encoding="utf-8")
    (repo / "package.json").write_text("{}", encoding="utf-8")

    def git(*args: str) -> None:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo), *args],  # noqa: S607
            check=True,
            capture_output=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )

    git("init", "-q")
    git("add", "package.json")
    git("commit", "-qm", "init")
    hook = f"\"{Path(sys.executable).as_posix()}\" -c \"open(r'{pwned}', 'w')\""
    git("config", "core.fsmonitor", hook)

    scanner._tracked(repo, ["node_modules"])
    staleness._git_commit_time(repo)
    archiver.hibernate(repo / "node_modules", repo, "dependencies", index=index)
    assert not pwned.exists()
