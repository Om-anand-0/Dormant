"""Decide what ``hibernate`` does with each target, then do it one target at a time."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dormant import archiver, purge
from dormant.detectors import Detector, Reinstall
from dormant.index import Index
from dormant.survey import Row
from dormant.units import format_size


class Op(StrEnum):
    """What happens to one target."""

    ARCHIVE = "archive"  # tar + zstd, restored on wake
    PURGE = "purge"  # deleted, reinstalled from the lockfile on wake
    DELETE = "delete"  # cache: deleted, never restored
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Action:
    """One planned step for one target."""

    project_root: Path
    target: Path
    kind: str
    op: Op
    bytes: int
    reason: str = ""
    reinstall: Reinstall | None = None


def plan(
    rows: Iterable[Row],
    *,
    purge_deps: bool,
    detectors: Sequence[Detector] | None = None,
    min_archive_bytes: int = 0,
) -> list[Action]:
    """Turn survey rows into actions.

    Caches are deleted; dependencies and build output are archived. A mostly
    hard-linked target is purged when it has a lockfile (archiving it frees nothing),
    else skipped. ``purge_deps`` purges every dependencies target that has a lockfile.

    Args:
        rows: Survey rows (already filtered by staleness and kind).
        purge_deps: The ``--purge`` flag.
        detectors: Registry for reinstall lookups; the bundled one when None.
        min_archive_bytes: Targets smaller than this are skipped instead of archived.

    Returns:
        Actions in row order, skips included with a reason.
    """
    actions = []
    for row in rows:
        root = row.project.root
        for t in row.project.targets:
            base = Action(root, t.path, t.kind, Op.SKIP, t.reclaimable_bytes)
            cmd = purge.reinstall_for(root, t.path.name, t.detector, detectors)
            a = _decide(base, row.in_use, t.tracked, t.hardlinked, cmd, purge_deps)
            actions.append(_skip_if_tiny(a, min_archive_bytes))
    return actions


def _decide(
    a: Action,
    in_use: bool,
    tracked: bool,
    hardlinked: bool,
    cmd: Reinstall | None,
    purge_deps: bool,
) -> Action:
    def to(op: Op, reason: str = "") -> Action:
        return Action(a.project_root, a.target, a.kind, op, a.bytes, reason, cmd)

    if in_use:
        return to(Op.SKIP, "project is in use by a running process")
    if tracked:
        return to(Op.SKIP, "contains git-tracked files")
    if a.kind == "cache":
        return to(Op.DELETE)
    if hardlinked:
        if cmd is None:
            return to(Op.SKIP, "mostly hard links and no lockfile to reinstall from")
        return to(Op.PURGE, "mostly hard links")
    if purge_deps and a.kind == "dependencies" and cmd is not None:
        return to(Op.PURGE)
    return to(Op.ARCHIVE)


def _skip_if_tiny(a: Action, min_bytes: int) -> Action:
    if a.op is not Op.ARCHIVE or a.bytes >= min_bytes:
        return a
    reason = f"under min_archive_size ({format_size(min_bytes)}); archiving would not save space"
    return Action(a.project_root, a.target, a.kind, Op.SKIP, a.bytes, reason, a.reinstall)


def execute(action: Action, *, index: Index, level: int) -> None:
    """Carry out one non-skip action.

    Args:
        action: The action.
        index: Open archive index.
        level: zstd compression level for archives.

    Raises:
        DormantError, OSError: The step failed; per the archiver's guarantees the
            target is whole (archive) or recoverable from its lockfile (purge).
    """
    if action.op == Op.ARCHIVE:
        archiver.hibernate(
            action.target, action.project_root, action.kind, index=index, level=level
        )
    elif action.op == Op.PURGE:
        assert action.reinstall is not None  # noqa: S101 - plan() only purges with one
        purge.purge(
            action.target,
            action.project_root,
            action.kind,
            action.reinstall,
            action.bytes,
            index=index,
        )
    elif action.op == Op.DELETE:
        archiver.safe_remove(action.target, action.project_root)
