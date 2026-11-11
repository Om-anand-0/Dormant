import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fixtures.trees import make_node_project, make_python_project, requires_symlinks

from dormant.detectors import load_detectors
from dormant.scanner import Project, dir_size, scan

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _scan(root: Path, ignore: tuple[str, ...] = ()) -> dict[str, Project]:
    return {p.root.relative_to(root).as_posix(): p for p in scan(root, load_detectors(), ignore)}


def _names(p: Project) -> set[str]:
    return {t.path.name for t in p.targets}


def test_django_react_monorepo_matches_both(tmp_path: Path) -> None:
    make_python_project(tmp_path)
    make_node_project(tmp_path)
    _write(tmp_path / "manage.py")
    _write(tmp_path / "frontend" / "package.json", b"{}")
    _write(tmp_path / "frontend" / "node_modules" / "react" / "index.js", b"1234")
    found = _scan(tmp_path)
    assert set(found) == {".", "frontend"}
    assert {d.name for d in found["."].detectors} == {"node", "python"}
    assert _names(found["."]) == {".venv", "node_modules"}
    assert _names(found["frontend"]) == {"node_modules"}
    assert found["frontend"].targets[0].reclaimable_bytes == 4


def test_never_descends_into_targets(tmp_path: Path) -> None:
    make_node_project(tmp_path)  # node_modules holds nested package.json files
    _write(tmp_path / "node_modules" / "deep" / "Cargo.toml")
    assert set(_scan(tmp_path)) == {"."}


def test_dormantignore_skips_project_and_below(tmp_path: Path) -> None:
    make_node_project(tmp_path / "skipped")
    _write(tmp_path / "skipped" / ".dormantignore", b"")
    make_node_project(tmp_path / "skipped" / "inner")
    make_node_project(tmp_path / "kept")
    assert set(_scan(tmp_path)) == {"kept"}


def test_ignore_globs(tmp_path: Path) -> None:
    make_node_project(tmp_path / "a")
    make_node_project(tmp_path / "archive" / "b")
    assert set(_scan(tmp_path, ("archive",))) == {"a"}
    assert set(_scan(tmp_path, ("*archive*",))) == {"a"}


@requires_symlinks
def test_symlink_loop_terminates(tmp_path: Path) -> None:
    make_node_project(tmp_path / "app")
    (tmp_path / "app" / "loop").symlink_to(tmp_path, target_is_directory=True)
    (tmp_path / "app" / "node_modules" / "loop").symlink_to(tmp_path, target_is_directory=True)
    found = _scan(tmp_path)
    assert set(found) == {"app"}
    assert found["app"].targets[0].total_bytes > 0


@requires_symlinks
def test_symlinked_target_is_ignored(tmp_path: Path) -> None:
    _write(tmp_path / "real" / "f", b"12345")
    _write(tmp_path / "proj" / "package.json", b"{}")
    (tmp_path / "proj" / "node_modules").symlink_to(tmp_path / "real", target_is_directory=True)
    assert _scan(tmp_path)["proj"].targets == ()


def test_hard_links_not_reclaimable(tmp_path: Path) -> None:
    _write(tmp_path / "package.json", b"{}")
    store = tmp_path / "store.bin"
    _write(store, b"a" * 1000)
    _write(tmp_path / "node_modules" / "own.js", b"b" * 100)
    os.link(store, tmp_path / "node_modules" / "linked.js")
    (target,) = _scan(tmp_path)["."].targets
    assert (target.total_bytes, target.reclaimable_bytes) == (1100, 100)
    assert target.hardlinked


def test_dir_size_empty(tmp_path: Path) -> None:
    assert dir_size(str(tmp_path)) == (0, 0)


def test_glob_and_dir_markers(tmp_path: Path) -> None:
    _write(tmp_path / "api" / "Api.csproj")
    _write(tmp_path / "api" / "bin" / "Api.dll")
    _write(tmp_path / "game" / "ProjectSettings" / "ProjectVersion.txt")
    _write(tmp_path / "game" / "Library" / "cache.bin")
    _write(tmp_path / "fake" / "ProjectSettings")  # a file, not a folder: no match
    found = _scan(tmp_path)
    assert set(found) == {"api", "game"}
    assert _names(found["api"]) == {"bin"}
    assert _names(found["game"]) == {"Library"}


def test_shared_target_claimed_once(tmp_path: Path) -> None:
    _write(tmp_path / "Cargo.toml")
    _write(tmp_path / "pom.xml")
    _write(tmp_path / "target" / "x")
    (p,) = _scan(tmp_path).values()
    assert [t.detector for t in p.targets] == ["rust"]


@needs_git
def test_committed_dist_is_tracked(tmp_path: Path) -> None:
    make_node_project(tmp_path)
    _write(tmp_path / "dist" / "bundle.js")
    _write(tmp_path / ".gitignore", b"node_modules/\n")
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    subprocess.run([*git, "add", "."], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-qm", "init"], check=True)  # noqa: S603
    tracked = {t.path.name: t.tracked for t in _scan(tmp_path)["."].targets}
    assert tracked == {"node_modules": False, "dist": True}


def test_windows_sample_switches_to_full_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("dormant.scanner.WINDOWS_NLINK_SAMPLE", 1)
    store = tmp_path / "store.bin"
    _write(store, b"a" * 10)
    _write(tmp_path / "t" / "a" / "own.js", b"b")
    os.link(store, tmp_path / "t" / "linked.js")
    os.link(store, tmp_path / "t" / "a" / "linked2.js")
    total, reclaimable = dir_size(str(tmp_path / "t"))
    assert total == 21
    assert reclaimable == 1  # linked.js is the first file sampled, so all get stat'ed
