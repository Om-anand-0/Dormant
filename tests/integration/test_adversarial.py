"""Adversarial tests: odd paths, races, tampering and hostile trees. Nothing may be lost."""

import contextlib
import ctypes
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
from collections import namedtuple
from collections.abc import Callable
from pathlib import Path

import pytest
from fixtures.trees import Fixture, _long, requires_symlinks, snapshot

from dormant import archiver, config
from dormant.archiver import hibernate, sweep_leftovers
from dormant.errors import ArchiveError, DormantError, RestoreError, UnsafePathError
from dormant.index import ArchiveState, Index
from dormant.restorer import WakeResult, wake

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> None:
    base = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    subprocess.run([*base, *args], cwd=cwd, check=True, capture_output=True, timeout=60)  # noqa: S603


def _short_name(path: Path) -> Path | None:
    """``path`` with its last component in 8.3 short form, or None if there is none."""
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))  # type: ignore[attr-defined]
    short = Path(buf.value).name if n else path.name
    return path.with_name(short) if short != path.name else None


def _round_trip(fx: Fixture, given: Path, root: Path, index: Index) -> None:
    """Hibernate via the ``given`` spelling, wake via the canonical one, compare."""
    before = snapshot(fx.target)
    rec = hibernate(given, root, "dependencies", index=index)
    assert not os.path.lexists(fx.target)
    assert index.find_active(fx.target) == rec
    assert wake(fx.target, index=index) == WakeResult.RESTORED
    assert snapshot(fx.target) == before
    assert fx.target.name in [p.name for p in fx.target.parent.iterdir()]  # same spelling on disk


# --- target spelling ---------------------------------------------------------------


def test_dotdot_and_trailing_slash(node_project: Fixture, index: Index) -> None:
    root = node_project.project_root
    given = Path(str(root / "node_modules" / ".." / "node_modules") + os.sep)
    _round_trip(node_project, given, root, index)


@windows_only
def test_wrong_case_target(node_project: Fixture, index: Index) -> None:
    root = node_project.project_root
    _round_trip(node_project, root / "NODE_MODULES", root, index)
    assert index.all()[0].target == node_project.target


@windows_only
def test_short_name_target(node_project: Fixture, index: Index) -> None:
    short = _short_name(node_project.target)
    if short is None:
        pytest.skip("volume has no 8.3 names")
    _round_trip(node_project, short, node_project.project_root, index)


@windows_only
def test_extended_prefix_target(node_project: Fixture, index: Index) -> None:
    """``\\\\?\\`` on the target only: refused cleanly (not lexically inside the root)."""
    before = snapshot(node_project.target)
    given = Path("\\\\?\\" + str(node_project.target))
    with pytest.raises(UnsafePathError):
        hibernate(given, node_project.project_root, "dependencies", index=index)
    assert snapshot(node_project.target) == before
    # Both prefixed works, as long as wake uses the same spelling.
    # ponytail: project_root is indexed as given (it may be a symlink); a root spelled two
    # ways (prefix, 8.3) makes two keys. Canonicalize roots in the scanner if that bites.
    root = Path("\\\\?\\" + str(node_project.project_root))
    hibernate(given, root, "dependencies", index=index)
    assert not node_project.target.exists()
    assert wake(given, index=index) == WakeResult.RESTORED
    assert snapshot(node_project.target) == before


@windows_only
@requires_git
@pytest.mark.parametrize("spelling", ["case", "short"])
def test_git_guard_not_fooled_by_spelling(
    spelling: str, node_project: Fixture, index: Index
) -> None:
    """Bug: `git ls-files -- NODE_MODULES` is case-sensitive, so tracked files got deleted."""
    root = node_project.project_root
    _git(root, "init", "-q")
    _git(root, "add", "-f", "node_modules/left-pad/index.js")
    _git(root, "commit", "-q", "-m", "oops")
    given = root / "NODE_MODULES" if spelling == "case" else _short_name(node_project.target)
    if given is None:
        pytest.skip("volume has no 8.3 names")
    before = snapshot(node_project.target)
    with pytest.raises(UnsafePathError, match="git-tracked"):
        hibernate(given, root, "dependencies", index=index)
    assert snapshot(node_project.target) == before
    assert index.all() == []


# --- project root behind a link ----------------------------------------------------------


def _linked_root(node_project: Fixture, tmp_path: Path, kind: str) -> Path:
    link = tmp_path / "linked_root"
    if kind == "junction":
        if sys.platform != "win32":
            pytest.skip("junctions are Windows only")
        import _winapi

        _winapi.CreateJunction(str(node_project.project_root), str(link))
    else:
        try:
            link.symlink_to(node_project.project_root, target_is_directory=True)
        except OSError:
            pytest.skip("cannot create symlinks")
    return link


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_project_root_is_a_link(
    kind: str, node_project: Fixture, tmp_path: Path, index: Index
) -> None:
    link = _linked_root(node_project, tmp_path, kind)
    outside = snapshot(node_project.project_root)
    before = snapshot(node_project.target)
    rec = hibernate(link / "node_modules", link, "dependencies", index=index)
    assert rec.target == link / "node_modules"
    assert not os.path.lexists(node_project.target)
    assert wake(link / "node_modules", index=index) == WakeResult.RESTORED
    assert snapshot(node_project.target) == before
    assert snapshot(node_project.project_root) == outside
    assert os.path.lexists(link)


# --- TOCTOU ------------------------------------------------------------------------------


def _after_walk(monkeypatch: pytest.MonkeyPatch, mutate: Callable[[], None]) -> None:
    real = archiver.walk

    def walk_then_mutate(target: Path) -> tuple[list[archiver.Entry], int]:
        out = real(target)
        mutate()
        return out

    monkeypatch.setattr(archiver, "walk", walk_then_mutate)


@requires_symlinks
def test_file_swapped_for_symlink_after_walk(
    node_project: Fixture, tmp_path: Path, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: open() followed the swapped-in link and archived an outside file's bytes."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET " * 100)
    victim = node_project.target / "left-pad" / "index.js"

    def swap() -> None:
        victim.unlink()
        victim.symlink_to(secret)

    _after_walk(monkeypatch, swap)
    with pytest.raises(ArchiveError, match="changed"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert node_project.target.is_dir()
    assert victim.is_symlink()
    assert secret.read_text() == "TOP SECRET " * 100
    assert index.all() == []
    assert not [p for p in config.archive_dir().iterdir() if ".tar.zst" in p.name]


def test_file_grows_after_walk(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: tarfile copies only the walked size, so a growing file was silently truncated."""
    victim = node_project.target / "left-pad" / "index.js"
    _after_walk(monkeypatch, lambda: victim.write_text(victim.read_text() + "// more\n"))
    with pytest.raises(ArchiveError, match="changed"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert victim.read_text().endswith("// more\n")
    assert index.all() == []


# --- locked and read-only files ------------------------------------------------------------


@windows_only
def test_file_open_with_share_delete(node_project: Fixture, index: Index) -> None:
    """A handle that allows delete (Node, AV scanners) lets the rename through."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    held = node_project.target / "left-pad" / "index.js"
    before = snapshot(node_project.target)
    share_all, generic_read, open_existing = 0x7, 0x80000000, 3
    h = k32.CreateFileW(str(held), generic_read, share_all, None, open_existing, 0, None)
    assert h != wintypes.HANDLE(-1).value
    try:
        with contextlib.suppress(ArchiveError):
            hibernate(node_project.target, node_project.project_root, "deps", index=index)
    finally:
        k32.CloseHandle(h)
    sweep_leftovers(node_project.project_root, index)
    if os.path.lexists(node_project.target):
        assert snapshot(node_project.target) == before
    else:
        assert wake(node_project.target, index=index) == WakeResult.RESTORED
        assert snapshot(node_project.target) == before


@windows_only
def test_read_only_dir_attribute(node_project: Fixture, index: Index) -> None:
    before = snapshot(node_project.target)
    d = node_project.target / "chalk"
    (d / "package.json").chmod(0o444)
    subprocess.run(["attrib", "+R", str(d)], check=True, capture_output=True, timeout=30)  # noqa: S603, S607
    hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert not node_project.target.exists()
    wake(node_project.target, index=index)
    assert snapshot(node_project.target) == before


@posix_only
def test_read_only_tree_posix(node_project: Fixture, index: Index) -> None:
    before = snapshot(node_project.target)
    for d in sorted(node_project.target.rglob("*"), reverse=True):
        if d.is_dir() and not d.is_symlink():
            d.chmod(0o555)
    hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert not node_project.target.exists()
    wake(node_project.target, index=index)
    assert snapshot(node_project.target) == before


# --- data dir vs target ---------------------------------------------------------------------


def test_data_dir_inside_target(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug: archives written into node_modules were deleted along with it."""
    monkeypatch.setenv(config.DATA_DIR_ENV, str(node_project.target / ".dormant"))
    config.archive_dir()
    before = snapshot(node_project.target)
    with pytest.raises(UnsafePathError, match="data dir"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert snapshot(node_project.target) == before
    assert index.all() == []


def test_target_inside_data_dir(
    dormant_data_dir: Path, node_project: Fixture, index: Index
) -> None:
    hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    archives = config.archive_dir()
    kept = sorted(p.name for p in archives.iterdir())
    for target, root in [(archives, dormant_data_dir), (archives.parent, archives.parent.parent)]:
        with pytest.raises(UnsafePathError, match="data dir"):
            hibernate(target, root, "dependencies", index=index)
    assert sorted(p.name for p in archives.iterdir()) == kept


# --- races ------------------------------------------------------------------------------------


@pytest.mark.parametrize("round_", range(5))
def test_two_hibernates_race(round_: int, node_project: Fixture) -> None:
    fx = node_project
    before = snapshot(fx.target)
    gate = threading.Barrier(2)
    outcomes: list[object] = []

    def run() -> None:
        with Index(config.index_path()) as idx:
            gate.wait()
            try:
                outcomes.append(hibernate(fx.target, fx.project_root, "dependencies", index=idx))
            except DormantError as exc:
                outcomes.append(exc)

    Index(config.index_path()).close()  # migrate first; concurrent migration is a known gap
    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(outcomes) == 2
    assert sum(not isinstance(o, Exception) for o in outcomes) <= 1
    with Index(config.index_path()) as idx:
        sweep_leftovers(fx.project_root, idx)
        if os.path.lexists(fx.target):
            assert idx.find_active(fx.target) is None
        else:
            assert wake(fx.target, index=idx) == WakeResult.RESTORED
        assert snapshot(fx.target) == before
        while wake(fx.target, index=idx) != WakeResult.NOTHING_TO_DO:
            pass  # a losing racer may keep a spare ACTIVE copy; it is marked STALE
    assert not [p for p in fx.project_root.iterdir() if ".dormant-" in p.name]


# --- index and archive tampering ------------------------------------------------------------


@pytest.mark.parametrize("how", ["deleted", "corrupted"])
def test_index_lost_between_hibernate_and_wake(how: str, node_project: Fixture) -> None:
    path = config.index_path()
    with Index(path) as idx:
        rec = hibernate(node_project.target, node_project.project_root, "deps", index=idx)
    assert rec.archive_path is not None
    archive = rec.archive_path.read_bytes()
    for p in path.parent.glob(path.name + "*"):
        p.unlink()
    if how == "deleted":
        # ponytail: a lost index can't be rebuilt yet; wake finds nothing and touches
        # nothing. Rebuilding from each archive's meta.json is the upgrade path.
        with Index(path) as idx:
            assert wake(node_project.target, index=idx) == WakeResult.NOTHING_TO_DO
    else:
        path.write_bytes(b"this is not a sqlite database" * 100)
        with pytest.raises(sqlite3.DatabaseError, match="not a database"):
            Index(path)
    assert not os.path.lexists(node_project.target)
    assert rec.archive_path.read_bytes() == archive


@pytest.mark.parametrize("how", ["truncated", "extended", "replaced"])
def test_archive_tampered(
    how: str, node_project: Fixture, python_project: Fixture, index: Index
) -> None:
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert rec.archive_path is not None
    data = rec.archive_path.read_bytes()
    if how == "truncated":
        bad = data[: len(data) // 2]
    elif how == "extended":
        bad = data + data
    else:
        other = hibernate(python_project.target, python_project.project_root, "x", index=index)
        assert other.archive_path is not None
        bad = other.archive_path.read_bytes()
    rec.archive_path.write_bytes(bad)
    with pytest.raises(RestoreError, match="corrupted"):
        wake(node_project.target, index=index)
    assert not os.path.lexists(node_project.target)
    assert rec.archive_path.read_bytes() == bad
    assert index.get(rec.id) == rec


# --- names ---------------------------------------------------------------------------------


def test_nfc_and_nfd_names(tmp_path: Path, index: Index) -> None:
    root = tmp_path / "proj"
    target = root / "node_modules"
    target.mkdir(parents=True)
    nfc, nfd = unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café")
    (target / nfc).write_text("composed")
    (target / nfd).write_text("decomposed")
    if len(list(target.iterdir())) != 2:
        pytest.skip("filesystem normalizes names")
    before = snapshot(target)
    hibernate(target, root, "dependencies", index=index)
    assert wake(target, index=index) == WakeResult.RESTORED
    assert snapshot(target) == before
    assert sorted(p.name for p in target.iterdir()) == sorted([nfc, nfd])


@posix_only
@pytest.mark.parametrize("name", ["a:b", "a\\b"])
def test_names_wake_would_reject(name: str, tmp_path: Path, index: Index) -> None:
    """Bug: hibernate archived names that member_path() refuses, so wake could never restore."""
    root = tmp_path / "proj"
    target = root / ".venv"
    target.mkdir(parents=True)
    (target / name).write_text("x")
    before = snapshot(target)
    with pytest.raises(UnsafePathError, match="restore"):
        hibernate(target, root, "dependencies", index=index)
    assert snapshot(target) == before
    assert index.all() == []


@windows_only
@pytest.mark.parametrize("name", ["trailing.", "trailing ", "CON", "nul.txt"])
def test_windows_unrestorable_names(name: str, tmp_path: Path, index: Index) -> None:
    """Names only reachable through ``\\\\?\\`` must round-trip or be refused, never lost."""
    root = tmp_path / "proj"
    target = root / "node_modules"
    target.mkdir(parents=True)
    try:
        (_long(target) / name).write_text("odd")
    except OSError:
        pytest.skip(f"cannot create {name!r}")
    before = snapshot(target)
    try:
        hibernate(target, root, "dependencies", index=index)
    except UnsafePathError:
        assert snapshot(target) == before
        assert index.all() == []
        return
    assert wake(target, index=index) == WakeResult.RESTORED
    assert snapshot(target) == before


# --- scale and space --------------------------------------------------------------------


@pytest.mark.slow
def test_twenty_thousand_files(tmp_path: Path, index: Index) -> None:
    root = tmp_path / "proj"
    target = root / "node_modules"
    for i in range(200):
        d = target / f"pkg{i:03d}"
        d.mkdir(parents=True)
        for j in range(100):
            (d / f"f{j:03d}.js").write_bytes(b"x" * (j % 7))
    before = snapshot(target)
    start = time.perf_counter()
    rec = hibernate(target, root, "dependencies", index=index)
    mid = time.perf_counter()
    assert wake(target, index=index) == WakeResult.RESTORED
    end = time.perf_counter()
    assert snapshot(target) == before
    print(  # timing report for -s runs
        f"\n20k files: hibernate {mid - start:.1f}s, wake {end - mid:.1f}s, "
        f"archive {rec.archive_bytes} bytes"
    )


@pytest.mark.parametrize(("free", "ok"), [(0, False), (10**15, True)])
def test_free_space_from_disk_usage(
    free: int, ok: bool, node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(archiver._platform.shutil, "disk_usage", lambda _p: usage(0, 0, free))
    before = snapshot(node_project.target)
    if ok:
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
        assert not node_project.target.exists()
        return
    with pytest.raises(ArchiveError, match="free space"):
        hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    assert snapshot(node_project.target) == before


def test_sweep_after_state_change(node_project: Fixture, index: Index) -> None:
    """A trash dir whose record is RESTORED is not proof the data is safe: keep it."""
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    wake(node_project.target, index=index)
    assert index.get(rec.id).state == ArchiveState.RESTORED  # type: ignore[union-attr]
    trash = node_project.project_root / f"node_modules.dormant-trash-{rec.id}"
    trash.mkdir()
    assert sweep_leftovers(node_project.project_root, index) == []
    assert trash.is_dir()
