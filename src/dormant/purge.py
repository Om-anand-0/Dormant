"""Purge mode: delete a target without an archive, reinstall it from the lockfile on wake.

Invariant 4: a target is purged only when its detector names a lockfile that exists.
Running the reinstall command is the only network access Dormant allows (invariant 8).
"""

import logging
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from dormant import archiver
from dormant.detectors import Detector, Reinstall, load_detectors
from dormant.errors import RestoreError
from dormant.index import ArchiveRecord, ArchiveState, Index

logger = logging.getLogger(__name__)

_OUTPUT_TAIL = 2000  # characters of package-manager output kept in an error message


def reinstall_for(
    project_root: Path,
    target_name: str,
    detector: str | None = None,
    detectors: Sequence[Detector] | None = None,
) -> Reinstall | None:
    """Find the reinstall command for a target whose lockfile exists.

    Args:
        project_root: Project folder that holds the lockfile.
        target_name: Folder name of the target (``node_modules``).
        detector: Only consider this detector (the one that claimed the target).
        detectors: Registry to search; the bundled one when None.

    Returns:
        The first matching entry whose lockfile exists, or None.
    """
    for det in load_detectors() if detectors is None else detectors:
        if detector is not None and det.name != detector:
            continue
        if not any(t.path == target_name for t in det.targets):
            continue
        for entry in det.reinstall:
            if (project_root / entry.lockfile).is_file():
                return entry
    return None


def purge(
    target: Path,
    project_root: Path,
    kind: str,
    reinstall: Reinstall,
    source_bytes: int,
    *,
    index: Index,
) -> ArchiveRecord:
    """Record ``target`` as PURGED, then delete it.

    The row goes in first so a crash mid-delete still leaves the reinstall command
    behind. If the delete fails and the folder is still there, the row is removed.

    Args:
        target: Folder to delete, strictly inside ``project_root``.
        project_root: Project that owns it.
        kind: Detector kind of the target.
        reinstall: Command (with an existing lockfile) that rebuilds the folder.
        source_bytes: Reclaimable size, kept for ``status``.
        index: Open archive index.

    Returns:
        The PURGED record.

    Raises:
        RestoreError: The lockfile is gone.
        UnsafePathError, OSError: The delete was refused or failed.
    """
    if not (project_root / reinstall.lockfile).is_file():
        raise RestoreError(f"{reinstall.lockfile} is missing; refusing to purge {target}")
    record = ArchiveRecord(
        id=uuid.uuid4().hex,
        project_root=project_root,
        target=target,
        kind=kind,
        archive_path=None,
        checksum="",
        source_bytes=source_bytes,
        archive_bytes=0,
        created_at=time.time(),
        format_version=archiver.FORMAT_VERSION,
        state=ArchiveState.PURGED,
        reinstall=reinstall.argv(),
        lockfile=reinstall.lockfile,
    )
    index.add(record)
    try:
        archiver.safe_remove(target, project_root)
    except BaseException:
        if os.path.lexists(target):
            index.delete(record.id)
        raise
    logger.info("purged %s (reinstall: %s)", target, " ".join(record.reinstall))
    return record


def _program(name: str, cwd: Path) -> str:
    """Resolve argv[0]: ``./x`` is a file in the project root, anything else is on PATH."""
    if name.startswith("./"):
        path = cwd / name[2:]
        if not path.is_file() or not path.absolute().is_relative_to(cwd.absolute()):
            raise RestoreError(f"{path} does not exist")
        return str(path.absolute())
    found = shutil.which(name)
    if found is None:
        raise RestoreError(f"{name!r} is not installed or not on PATH")
    return found


def run_reinstall(argv: Sequence[str], cwd: Path) -> None:
    """Run a reinstall command from ``cwd`` with no shell.

    Args:
        argv: Program and arguments.
        cwd: Project root.

    Raises:
        RestoreError: The program is missing or exited non-zero; the message includes
            the end of its output.
    """
    if not argv:
        raise RestoreError(f"no reinstall command recorded for {cwd}")
    cmd = [_program(argv[0], cwd), *argv[1:]]
    shown = " ".join(argv)
    logger.info("running %s in %s", shown, cwd)
    # ponytail: no timeout; package managers can legitimately take many minutes.
    try:
        out = subprocess.run(  # noqa: S603 - argv list from detectors.toml, no shell
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise RestoreError(f"could not run {shown!r}: {exc}") from exc
    if out.returncode != 0:
        tail = out.stdout[-_OUTPUT_TAIL:].rstrip()
        raise RestoreError(f"{shown!r} failed with exit code {out.returncode}:\n{tail}")


def wake_purged(record: ArchiveRecord, *, index: Index) -> bool:
    """Reinstall a PURGED target.

    Args:
        record: The PURGED record.
        index: Open archive index.

    Returns:
        True if the command ran, False if the folder already existed (record closed).

    Raises:
        RestoreError: The command failed; the record stays PURGED.
    """
    if os.path.lexists(record.target):
        index.set_state(record.id, ArchiveState.RESTORED)
        return False
    run_reinstall(record.reinstall, record.project_root)
    index.set_state(record.id, ArchiveState.RESTORED)
    return True


def reinstall_active(record: ArchiveRecord, command: Reinstall, *, index: Index) -> None:
    """``wake --reinstall`` for an archived target: run the command, mark the archive STALE.

    Args:
        record: An ACTIVE record whose target is absent.
        command: Reinstall entry with an existing lockfile.
        index: Open archive index.

    Raises:
        RestoreError: The command failed; the record stays ACTIVE.
    """
    run_reinstall(command.argv(), record.project_root)
    index.set_state(record.id, ArchiveState.STALE)
