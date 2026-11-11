"""Crash tests: fail every step of hibernate and wake, before and after it runs.

Invariant after any crash: the source folder is whole with no index row and no archive
or tmp file, or the source is gone and an ACTIVE row's archive verifies and wakes back
to the identical tree. Then the real operation is run again and must recover.
"""

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from fixtures.trees import Fixture, snapshot

from dormant import archiver, config, restorer
from dormant.archiver import hibernate, sweep_leftovers
from dormant.index import ArchiveRecord, ArchiveState, Index
from dormant.restorer import WakeResult, wake

HIBERNATE_STEPS = [
    "_validate",
    "_git_guard",
    "_make_archive",
    "walk",
    "_check_free_space",
    "write_archive",
    "verify_archive",
    "_publish",
    "_record",
    "_remove_source",
    "_move_to_trash",
    "safe_remove",
    "_undo_partial_remove",
]
WAKE_STEPS = ["verify", "materialize", "extract"]
CRASHES: list[type[BaseException]] = [OSError, KeyboardInterrupt]


def _inject(
    monkeypatch: pytest.MonkeyPatch, module: object, name: str, exc: type[BaseException], when: str
) -> None:
    """Make the first call of ``module.name`` raise ``exc`` before or after it runs."""
    real: Callable[..., object] = getattr(module, name)
    fired: list[bool] = []

    def wrapper(*args: object, **kwargs: object) -> object:
        if fired:
            return real(*args, **kwargs)
        fired.append(True)
        if when == "after":
            real(*args, **kwargs)
        raise exc(f"injected crash {when} {name}")

    monkeypatch.setattr(module, name, wrapper)


def _fail_safe_remove_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the first delete fail so hibernate reaches _undo_partial_remove."""
    real = archiver.safe_remove
    calls: list[Path] = []

    def flaky(path: Path, root: Path) -> None:
        calls.append(path)
        if len(calls) == 1:
            raise PermissionError("locked")
        real(path, root)

    monkeypatch.setattr(archiver, "safe_remove", flaky)


def _files(suffix: str) -> list[Path]:
    return [p for p in config.archive_dir().iterdir() if p.name.endswith(suffix)]


def _leftovers(fx: Fixture) -> list[Path]:
    return [p for p in fx.project_root.iterdir() if ".dormant-" in p.name]


def _assert_hibernate_invariant(
    fx: Fixture, before: dict[str, object], index: Index, *, orphan_ok: bool
) -> None:
    assert _files(".tmp") == []
    rows = index.all()
    if os.path.lexists(fx.target):
        assert snapshot(fx.target) == before
        assert rows == []
        if not orphan_ok:
            assert _files(".tar.zst") == []
        return
    (rec,) = rows
    assert rec.state == ArchiveState.ACTIVE
    restorer.verify(rec)


def _recover_hibernate(fx: Fixture, before: dict[str, object], index: Index) -> None:
    sweep_leftovers(fx.project_root, index)
    if not os.path.lexists(fx.target):
        assert wake(fx.target, index=index) == WakeResult.RESTORED
        assert snapshot(fx.target) == before
    rec = hibernate(fx.target, fx.project_root, "dependencies", index=index)
    assert not os.path.lexists(fx.target)
    assert _leftovers(fx) == []
    assert wake(fx.target, index=index) == WakeResult.RESTORED
    assert snapshot(fx.target) == before
    assert index.get(rec.id).state == ArchiveState.RESTORED  # type: ignore[union-attr]
    assert _leftovers(fx) == []


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("exc", CRASHES, ids=lambda e: e.__name__)
@pytest.mark.parametrize("step", HIBERNATE_STEPS)
def test_hibernate_crash(
    step: str,
    exc: type[BaseException],
    when: str,
    node_project: Fixture,
    index: Index,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = node_project
    before = snapshot(fx.target)
    if step == "_undo_partial_remove":
        _fail_safe_remove_once(monkeypatch)
    _inject(monkeypatch, archiver, step, exc, when)

    with pytest.raises((exc, archiver.ArchiveError)):
        hibernate(fx.target, fx.project_root, "dependencies", index=index)

    # ponytail: a crash right after _make_archive returns (power loss before the index
    # write) leaks an unindexed .tar.zst; the source is whole. Add an orphan-archive
    # sweep to prune when it matters.
    orphan_ok = step == "_make_archive" and when == "after"
    _assert_hibernate_invariant(fx, before, index, orphan_ok=orphan_ok)
    monkeypatch.undo()
    _recover_hibernate(fx, before, index)


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("exc", CRASHES, ids=lambda e: e.__name__)
@pytest.mark.parametrize("step", WAKE_STEPS)
def test_wake_crash(
    step: str,
    exc: type[BaseException],
    when: str,
    node_project: Fixture,
    index: Index,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fx = node_project
    before = snapshot(fx.target)
    rec = hibernate(fx.target, fx.project_root, "dependencies", index=index)
    _inject(monkeypatch, restorer, step, exc, when)

    with pytest.raises((exc, restorer.RestoreError)):
        wake(fx.target, index=index)

    assert index.get(rec.id) == rec  # still ACTIVE, unchanged
    restorer.verify(rec)
    if os.path.lexists(fx.target):
        assert snapshot(fx.target) == before
    monkeypatch.undo()
    assert wake(fx.target, index=index) in (WakeResult.RESTORED, WakeResult.SKIPPED_EXISTS)
    assert snapshot(fx.target) == before
    sweep_leftovers(fx.project_root, index)
    assert _leftovers(fx) == []


# --- sweep_leftovers --------------------------------------------------------------


def _interrupted_delete(fx: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Hibernate with Ctrl-C during the final delete; return the trash dir left behind."""
    _inject(monkeypatch, archiver, "safe_remove", KeyboardInterrupt, "before")
    with pytest.raises(KeyboardInterrupt):
        hibernate(fx.target, fx.project_root, "dependencies", index=index)
    monkeypatch.undo()
    (trash,) = _leftovers(fx)
    assert ".dormant-trash-" in trash.name
    return trash


def test_sweep_removes_trash_of_verified_archive(
    node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(node_project.target)
    trash = _interrupted_delete(node_project, index, monkeypatch)
    assert sweep_leftovers(node_project.project_root, index) == [trash]
    assert _leftovers(node_project) == []
    assert sweep_leftovers(node_project.project_root, index) == []
    assert wake(node_project.target, index=index) == WakeResult.RESTORED
    assert snapshot(node_project.target) == before


def _only_record(index: Index) -> ArchiveRecord:
    (rec,) = index.all()
    assert rec.archive_path is not None
    return rec


@pytest.mark.parametrize("damage", ["corrupt", "missing", "not_active", "no_row"])
def test_sweep_keeps_trash_without_verified_archive(
    damage: str, node_project: Fixture, index: Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    trash = _interrupted_delete(node_project, index, monkeypatch)
    rec = _only_record(index)
    assert rec.archive_path is not None
    if damage == "corrupt":
        rec.archive_path.write_bytes(rec.archive_path.read_bytes()[:-10])
    elif damage == "missing":
        rec.archive_path.unlink()
    elif damage == "not_active":
        index.set_state(rec.id, ArchiveState.STALE)
    else:
        index.delete(rec.id)
    trash_before = snapshot(trash)
    assert sweep_leftovers(node_project.project_root, index) == []
    assert snapshot(trash) == trash_before


def test_sweep_restoring_dirs(node_project: Fixture, index: Index) -> None:
    fx = node_project
    rec = hibernate(fx.target, fx.project_root, "dependencies", index=index)
    staging = fx.target.with_name(f"{fx.target.name}.dormant-restoring-{rec.id}")
    (staging / "partial").mkdir(parents=True)
    # ACTIVE record: staging is disposable.
    assert sweep_leftovers(fx.project_root, index) == [staging]
    # Restored record and target present: staging is disposable too.
    wake(fx.target, index=index)
    (staging / "partial").mkdir(parents=True)
    assert sweep_leftovers(fx.project_root, index) == [staging]
    # Not ACTIVE and target missing: leave it, it may be the only copy.
    os.rename(fx.target, fx.project_root / "moved")  # noqa: PTH104
    (staging / "partial").mkdir(parents=True)
    assert sweep_leftovers(fx.project_root, index) == []
    assert staging.is_dir()


def test_sweep_ignores_other_projects_and_unknown_ids(
    node_project: Fixture, python_project: Fixture, index: Index
) -> None:
    rec = hibernate(node_project.target, node_project.project_root, "dependencies", index=index)
    stray = node_project.project_root / "node_modules.dormant-trash-deadbeef"
    (stray / "x").mkdir(parents=True)
    other = python_project.project_root / f".venv.dormant-trash-{rec.id}"
    other.mkdir()
    assert sweep_leftovers(node_project.project_root, index) == []
    assert sweep_leftovers(python_project.project_root, index) == []
    assert stray.is_dir()
    assert other.is_dir()
