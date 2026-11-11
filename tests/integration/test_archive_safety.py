"""Behaviour and safety tests for hibernate, wake and safe_remove."""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard
from fixtures.trees import Fixture, requires_symlinks, snapshot

from dormant import archiver, config, restorer
from dormant.archiver import hibernate, safe_remove
from dormant.errors import ArchiveError, RestoreError, UnsafePathError
from dormant.index import ArchiveState, Index
from dormant.restorer import WakeResult, wake

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _archive_dir_files() -> list[Path]:
    return list(config.archive_dir().iterdir())


def _assert_untouched(fx: Fixture, before: dict[str, object], index: Index) -> None:
    assert snapshot(fx.target) == before
    assert index.all() == []
    assert not [p for p in _archive_dir_files() if ".tar.zst" in p.name]
    assert not [p for p in fx.project_root.iterdir() if ".dormant-" in p.name]


def _git(cwd: Path, *args: str) -> None:
    base = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    subprocess.run([*base, *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603


# --- wake ---------------------------------------------------------------------------


def test_wake_nothing_to_do(node_project: Fixture, index: Index) -> None:
    assert wake(node_project.target, index=index) == WakeResult.NOTHING_TO_DO


def test_wake_skips_when_target_exists(node_project: Fixture, index: Index) -> None:
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    node_project.target.mkdir()
    assert wake(node_project.target, index=index) == WakeResult.SKIPPED_EXISTS
    assert index.get(rec.id).state == ArchiveState.STALE  # type: ignore[union-attr]
    assert rec.archive_path is not None
    assert rec.archive_path.exists()
    assert list(node_project.target.iterdir()) == []


def test_wake_corrupted_archive_changes_nothing(node_project: Fixture, index: Index) -> None:
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert rec.archive_path is not None
    data = bytearray(rec.archive_path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    rec.archive_path.write_bytes(data)

    with pytest.raises(RestoreError, match="corrupted"):
        wake(node_project.target, index=index)
    assert not os.path.lexists(node_project.target)
    assert rec.archive_path.read_bytes() == data
    assert index.get(rec.id) == rec
    assert not [p for p in node_project.project_root.iterdir() if ".dormant-" in p.name]


def test_wake_missing_archive(node_project: Fixture, index: Index) -> None:
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert rec.archive_path is not None
    rec.archive_path.unlink()
    with pytest.raises(RestoreError, match="cannot read"):
        wake(node_project.target, index=index)
    assert index.get(rec.id) == rec


def test_wake_removes_leftover_staging(node_project: Fixture, index: Index) -> None:
    before = snapshot(node_project.target)
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    staging = node_project.target.with_name(f"node_modules.dormant-restoring-{rec.id}")
    (staging / "junk").mkdir(parents=True)
    assert wake(node_project.target, index=index) == WakeResult.RESTORED
    assert snapshot(node_project.target) == before
    assert not staging.exists()


@requires_symlinks
def test_wake_without_symlink_privilege(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)

    def no_symlinks(*args: object, **kwargs: object) -> None:
        exc = OSError(1, "A required privilege is not held by the client")
        exc.winerror = 1314  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(restorer.os, "symlink", no_symlinks)
    with pytest.raises(RestoreError, match="--reinstall"):
        wake(node_project.target, index=index)
    assert not os.path.lexists(node_project.target)
    assert index.get(rec.id) == rec
    assert not [p for p in node_project.project_root.iterdir() if ".dormant-" in p.name]


def test_wake_target_appears_during_extract(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    real_extract = restorer.extract

    def extract_then_race(archive: Path, dest: Path) -> None:
        real_extract(archive, dest)
        node_project.target.mkdir()

    monkeypatch.setattr(restorer, "extract", extract_then_race)
    with pytest.raises(RestoreError, match="appeared"):
        wake(node_project.target, index=index)
    assert list(node_project.target.iterdir()) == []
    assert index.get(rec.id) == rec
    assert not [p for p in node_project.project_root.iterdir() if ".dormant-" in p.name]


# --- hand-made archives -------------------------------------------------------------


def _craft(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with (
        path.open("wb") as f,
        zstandard.ZstdCompressor().stream_writer(f, closefd=False) as zw,
        tarfile.open(fileobj=zw, mode="w|", format=tarfile.PAX_FORMAT) as tar,
    ):
        for info, data in members:
            info.size = len(data) if info.isreg() else 0
            tar.addfile(info, io.BytesIO(data) if info.isreg() else None)


def _meta(fmt: int = 1) -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo("meta.json"), json.dumps({"format": fmt}).encode()


def _member(
    name: str, kind: bytes = tarfile.REGTYPE, link: str = ""
) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    return info, b"x"


@pytest.mark.parametrize(
    "members",
    [
        [_meta(2)],
        [_member("data/a")],
        [],
        [_meta(), _member("data/../evil")],
        [_meta(), _member("data/a", tarfile.LNKTYPE, "data/b")],
        [
            _meta(),
            _member("data/l", tarfile.SYMTYPE, "x"),
            _member("data/l/m", tarfile.SYMTYPE, "y"),
        ],
    ],
    ids=["newer-format", "no-meta", "empty", "dotdot", "hardlink", "link-under-link"],
)
def test_extract_rejects_bad_archives(
    tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes]]
) -> None:
    archive = tmp_path / "a.tar.zst"
    _craft(archive, members)
    with pytest.raises((RestoreError, tarfile.TarError)):
        restorer.extract(archive, tmp_path / "out")
    assert not (tmp_path / "evil").exists()


@pytest.mark.parametrize(
    "name",
    [
        "meta.json",
        "other/a",
        "data/",
        "data//a",
        "data/./a",
        "data/a/../b",
        "data/C:x",
        "data/a\\b",
    ],
)
def test_member_path_rejects(name: str) -> None:
    with pytest.raises(RestoreError):
        restorer.member_path(name)


def test_member_path_accepts() -> None:
    assert restorer.member_path("data/a/b c/é.txt") == "a/b c/é.txt"


# --- hibernate refusals ---------------------------------------------------------------


def test_refuses_project_root_and_outside(
    node_project: Fixture, tmp_path: Path, index: Index
) -> None:
    before = snapshot(node_project.target)
    root = node_project.project_root
    for target, project in [
        (root, root),
        (tmp_path, root),
        (root.parent / "elsewhere", root),
        (Path("node_modules"), root),
        (root / "package.json", root),
        (root / "missing", root),
    ]:
        with pytest.raises(UnsafePathError):
            hibernate(target, project, "dependencies", index=index)
    _assert_untouched(node_project, before, index)


@requires_symlinks
def test_refuses_symlinked_target(node_project: Fixture, tmp_path: Path, index: Index) -> None:
    real = tmp_path / "real_modules"
    real.mkdir()
    link = node_project.project_root / "linked"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        hibernate(link, node_project.project_root, "dependencies", index=index)
    # A target below a symlinked directory is refused too.
    (real / "inner").mkdir()
    with pytest.raises(UnsafePathError, match="symlink"):
        hibernate(link / "inner", node_project.project_root, "dependencies", index=index)
    assert real.is_dir()
    assert (real / "inner").is_dir()


def test_hibernate_twice(node_project: Fixture, index: Index) -> None:
    hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    node_project.target.mkdir()
    (node_project.target / "f").write_text("new")
    with pytest.raises(ArchiveError, match="already hibernated"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert (node_project.target / "f").read_text() == "new"
    assert len(index.all()) == 1


@requires_git
def test_refuses_git_tracked(node_project: Fixture, index: Index) -> None:
    root = node_project.project_root
    _git(root, "init", "-q")
    _git(root, "add", "-f", "node_modules/left-pad/index.js")
    _git(root, "commit", "-q", "-m", "oops")
    before = snapshot(node_project.target)
    with pytest.raises(UnsafePathError, match="git-tracked"):
        hibernate(node_project.target, root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)


@requires_git
def test_allows_untracked_in_repo(node_project: Fixture, index: Index) -> None:
    root = node_project.project_root
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("node_modules/\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    hibernate(node_project.target, root, "dependencies", index=index)
    assert not node_project.target.exists()


def test_refuses_when_git_missing_inside_repo(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    (node_project.project_root / ".git").mkdir()
    monkeypatch.setattr(archiver.shutil, "which", lambda _name: None)
    before = snapshot(node_project.target)
    with pytest.raises(UnsafePathError, match="git is not installed"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)


def test_refuses_nested_git_repo(node_project: Fixture, index: Index) -> None:
    (node_project.target / "left-pad" / ".git").mkdir()
    before = snapshot(node_project.target)
    with pytest.raises(UnsafePathError, match="git repository inside"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)


def test_refuses_without_free_space(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archiver._platform, "free_bytes", lambda _p: 0)
    before = snapshot(node_project.target)
    with pytest.raises(ArchiveError, match="free space"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)
    assert _archive_dir_files() == [] or all(".tmp" not in p.name for p in _archive_dir_files())


def test_verify_mismatch_leaves_no_tmp(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(archiver, "file_checksum", lambda _p: "0" * 128)
    before = snapshot(node_project.target)
    with pytest.raises(ArchiveError, match="checksum mismatch"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)
    assert _archive_dir_files() == [] or all(".tmp" not in p.name for p in _archive_dir_files())


def test_member_list_mismatch(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = archiver.write_archive

    def write_short(tmp: Path, meta: bytes, entries: list[archiver.Entry], level: int) -> str:
        return real_write(tmp, meta, entries[:-1], level)  # archive misses one member

    monkeypatch.setattr(archiver, "write_archive", write_short)
    before = snapshot(node_project.target)
    with pytest.raises(ArchiveError, match="member list"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)


def test_unreadable_source_becomes_archive_error(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(archiver, "_add_entry", boom)
    before = snapshot(node_project.target)
    with pytest.raises(ArchiveError, match="denied"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)
    assert all(".tmp" not in p.name for p in _archive_dir_files())


def test_index_add_failure_removes_archive(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_record: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(index, "add", boom)
    before = snapshot(node_project.target)
    with pytest.raises(KeyboardInterrupt):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)


# --- removal failures (invariant 6) -------------------------------------------------------


def test_delete_failure_restores_source(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = archiver.safe_remove
    calls: list[Path] = []

    def fail_first(path: Path, root: Path) -> None:
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError("locked")
        real(path, root)

    monkeypatch.setattr(archiver, "safe_remove", fail_first)
    before = snapshot(node_project.target)
    with pytest.raises(ArchiveError, match="restored from the archive"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)


def test_delete_and_restore_failure_keeps_archive(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_fail(path: Path, root: Path) -> None:
        raise PermissionError("locked")

    def no_restore(*_args: object) -> None:
        raise RestoreError("disk full")

    monkeypatch.setattr(archiver, "safe_remove", always_fail)
    monkeypatch.setattr(restorer, "materialize", no_restore)
    with pytest.raises(ArchiveError, match="run wake"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    (rec,) = index.all()
    assert rec.state == ArchiveState.ACTIVE
    assert rec.archive_path is not None
    assert rec.archive_path.exists()


def test_rename_failure_rolls_back(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_rename(*_args: object) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(archiver.os, "rename", no_rename)
    before = snapshot(node_project.target)
    with pytest.raises(ArchiveError, match="in use"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)


@windows_only
def test_open_file_blocks_hibernate(node_project: Fixture, index: Index) -> None:
    before = snapshot(node_project.target)
    held = node_project.target / "left-pad" / "index.js"
    with held.open("rb"), pytest.raises(ArchiveError, match="in use"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    _assert_untouched(node_project, before, index)
    assert _archive_dir_files() == []


def test_read_only_entries_are_removed(node_project: Fixture, index: Index) -> None:
    before = snapshot(node_project.target)
    if sys.platform == "win32":
        locked = node_project.target / "left-pad" / "index.js"
    else:
        locked = node_project.target / "left-pad"
    locked.chmod(stat.S_IREAD | (stat.S_IEXEC if locked.is_dir() else 0))
    hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert not node_project.target.exists()
    wake(node_project.target, index=index)
    assert snapshot(node_project.target) == before


@windows_only
def test_refuses_junction_inside_target(
    node_project: Fixture, tmp_path: Path, index: Index
) -> None:
    import _winapi

    outside = tmp_path / "outside_dir"
    outside.mkdir()
    _winapi.CreateJunction(str(outside), str(node_project.target / "junc"))
    with pytest.raises(UnsafePathError, match="junction"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert index.all() == []


# --- safe_remove ----------------------------------------------------------------------------


def test_safe_remove_never_escapes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    doomed = root / "doomed"
    (doomed / "sub").mkdir(parents=True)
    (doomed / "sub" / "f.txt").write_text("bye")
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep.txt"
    keep.write_text("keep me")
    made_link = False
    try:
        (doomed / "link").symlink_to(outside, target_is_directory=True)
        (doomed / "sub" / "filelink").symlink_to(keep)
        made_link = True
    except OSError:
        pass
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(outside), str(doomed / "junction"))
    elif not made_link:
        pytest.skip("cannot create symlinks")

    safe_remove(doomed, root)
    assert not os.path.lexists(doomed)
    assert keep.read_text() == "keep me"
    assert outside.is_dir()


def test_safe_remove_refusals(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    for path in [root, tmp_path, root.parent / "rootx", Path("rel"), root / "a" / ".." / ".."]:
        with pytest.raises(UnsafePathError):
            safe_remove(path, root)
    assert (root / "a").is_dir()


@requires_symlinks
def test_safe_remove_refuses_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    real = tmp_path / "real"
    (real / "sub").mkdir(parents=True)
    (root / "link").symlink_to(real, target_is_directory=True)
    for path in [root / "link", root / "link" / "sub"]:
        with pytest.raises(UnsafePathError):
            safe_remove(path, root)
    assert (real / "sub").is_dir()


def test_safe_remove_retries_then_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    (root / "d").mkdir(parents=True)
    safe_remove(root / "missing", root)  # already gone is fine
    attempts: list[Path] = []

    def locked(path: Path) -> None:
        attempts.append(path)
        raise PermissionError("locked")

    monkeypatch.setattr(archiver, "_rmtree", locked)
    monkeypatch.setattr(archiver.time, "sleep", lambda _s: None)
    with pytest.raises(PermissionError):
        safe_remove(root / "d", root)
    assert len(attempts) == 4
