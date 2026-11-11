"""Find and delete archives nobody can need any more.

Never deletes the only copy of hibernated data while its project still exists: an
ACTIVE archive goes only when its project folder is gone, or (with ``--older``) when
its target folder exists again.
"""

import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from dormant import config
from dormant.errors import UnsafePathError
from dormant.index import ArchiveRecord, ArchiveState, Index


@dataclass(frozen=True, slots=True)
class Candidate:
    """An index record to delete, with why."""

    record: ArchiveRecord
    reason: str


def candidates(index: Index, older: timedelta | None = None) -> list[Candidate]:
    """List records whose archives (or rows) can go.

    Args:
        index: Open archive index.
        older: Also take ACTIVE archives at least this old whose target exists again.

    Returns:
        Candidates, oldest first.
    """
    cutoff = None if older is None else time.time() - older.total_seconds()
    out = []
    for rec in index.all():
        reason = _reason(rec, cutoff)
        if reason:
            out.append(Candidate(rec, reason))
    return out


def _reason(rec: ArchiveRecord, cutoff: float | None) -> str:
    if rec.state == ArchiveState.RESTORED:
        return ""
    if not os.path.isdir(rec.project_root):  # noqa: PTH112
        return "project folder no longer exists"
    if rec.state == ArchiveState.STALE:
        return "target was recreated; archive is stale"
    target_back = os.path.lexists(rec.target)
    old = cutoff is not None and rec.created_at <= cutoff
    if rec.state == ArchiveState.ACTIVE and old and target_back:
        return "older than --older and the target exists again"
    return ""


def remove(rec: ArchiveRecord, *, index: Index) -> int:
    """Delete a record's archive file (if any), then its index row.

    Args:
        rec: The record.
        index: Open archive index.

    Returns:
        Bytes freed in the archive store.

    Raises:
        UnsafePathError: The archive path is not inside the archive store.
        OSError: The file could not be deleted (the row is kept).
    """
    freed = 0
    if rec.archive_path is not None:
        store = Path(os.path.realpath(config.archive_dir()))
        path = Path(os.path.realpath(rec.archive_path))
        if path.parent != store:
            raise UnsafePathError(f"{rec.archive_path} is not in the archive store {store}")
        if path.exists():
            freed = path.stat().st_size
            path.unlink()
    index.delete(rec.id)
    return freed
