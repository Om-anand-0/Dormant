"""Wake: verify an archive, extract it beside the target, and rename it into place.

Each step is a module-level function so crash tests can monkeypatch it.
"""

import json
import logging
import os
import tarfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import zstandard

from dormant import archiver
from dormant import platform as _platform
from dormant.errors import RestoreError, UnsafePathError
from dormant.index import ArchiveRecord, ArchiveState, Index

logger = logging.getLogger(__name__)

_ERROR_PRIVILEGE_NOT_HELD = 1314  # Windows: symlinks need Developer Mode or admin
_EXTRACT_ERRORS = (OSError, tarfile.TarError, zstandard.ZstdError, ValueError)


class WakeResult(StrEnum):
    """Outcome of :func:`wake`."""

    RESTORED = "restored"
    SKIPPED_EXISTS = "skipped_exists"
    NOTHING_TO_DO = "nothing_to_do"


@dataclass(frozen=True, slots=True)
class _Link:
    rel: str
    target: str
    is_dir: bool


def verify(record: ArchiveRecord) -> None:
    """Check the archive file against the checksum stored in the index.

    Args:
        record: Index record of the archive.

    Raises:
        RestoreError: The file is missing, unreadable or its checksum differs.
    """
    if record.archive_path is None:
        raise RestoreError(f"{record.target} has no archive (it was purged)")
    try:
        actual = archiver.file_checksum(record.archive_path)
    except OSError as exc:
        raise RestoreError(f"cannot read archive {record.archive_path}: {exc}") from exc
    if actual != record.checksum:
        raise RestoreError(f"archive {record.archive_path} is corrupted (checksum mismatch)")


def member_path(name: str) -> str:
    """Map an archive member name to a safe relative POSIX path (invariant 7).

    Args:
        name: Member name, expected as ``data/<relative path>``.

    Returns:
        The relative path with the ``data/`` prefix stripped.

    Raises:
        RestoreError: The name is outside ``data/``, absolute, has ``..``, ``.`` or empty
            components, a drive letter or a backslash.
    """
    rel = name.removeprefix(archiver.DATA_PREFIX)
    parts = rel.split("/")
    if (
        not name.startswith(archiver.DATA_PREFIX)
        or "\\" in rel
        or ":" in rel
        or any(p in ("", ".", "..") for p in parts)
    ):
        raise RestoreError(f"unsafe archive member name: {name!r}")
    return rel


def _check_meta(tar: tarfile.TarFile, member: tarfile.TarInfo | None) -> None:
    if member is None or member.name != archiver.META_NAME or not member.isreg():
        raise RestoreError("archive does not start with meta.json")
    fh = tar.extractfile(member)
    meta = json.loads(fh.read()) if fh is not None else None
    fmt = meta.get("format") if isinstance(meta, dict) else None
    if not isinstance(fmt, int) or fmt > archiver.FORMAT_VERSION:
        raise RestoreError(f"unsupported archive format {fmt!r}; upgrade Dormant")


def _create_links(dest: Path, links: list[_Link]) -> None:
    """Create symlinks last, so no extracted file is ever written through one."""
    names = {link.rel for link in links}
    for link in links:
        parts = link.rel.split("/")
        if any("/".join(parts[:i]) in names for i in range(1, len(parts))):
            raise RestoreError(f"symlink {link.rel!r} sits below another symlink")
        path = dest.joinpath(*parts)
        try:
            os.symlink(link.target, path, target_is_directory=link.is_dir)  # noqa: PTH211
        except OSError as exc:
            if getattr(exc, "winerror", None) == _ERROR_PRIVILEGE_NOT_HELD:
                msg = (
                    "this archive contains symlinks and Windows won't let Dormant create "
                    "them (enable Developer Mode or run as admin), or use --reinstall"
                )
                raise RestoreError(msg) from exc
            raise


def extract(archive: Path, dest: Path) -> None:
    """Extract an archive into ``dest``, which must not exist yet.

    Regular files and directories go through tarfile's ``data`` filter. Symlinks skip
    the filter (it rejects absolute targets, which every POSIX venv has) and are
    created after everything else.

    Args:
        archive: The ``.tar.zst`` file.
        dest: New directory to extract into.

    Raises:
        RestoreError: Unsafe member, unsupported format or member type.
        OSError, tarfile.TarError, zstandard.ZstdError, ValueError: Reading or writing failed.
    """
    out = _platform.long_path(dest)
    out.mkdir()
    links: list[_Link] = []
    with (
        archive.open("rb") as f,
        zstandard.ZstdDecompressor().stream_reader(f) as zr,
        tarfile.open(fileobj=zr, mode="r|") as tar,
    ):
        members = iter(tar)
        _check_meta(tar, next(members, None))
        for m in members:
            rel = member_path(m.name)
            if m.issym():
                is_dir = m.pax_headers.get(archiver.DIRLINK_KEY) == "1"
                links.append(_Link(rel, m.linkname, is_dir))
            elif m.isreg() or m.isdir():
                tar.extract(m.replace(name=rel, deep=False), path=out, filter="data")
            else:
                raise RestoreError(f"unsupported member type in archive: {m.name!r}")
    _create_links(out, links)


def _discard(path: Path, root: Path) -> None:
    try:
        archiver.safe_remove(path, root)
    except (OSError, UnsafePathError) as exc:
        logger.warning("could not clean up %s: %s", path, exc)


def materialize(archive: Path, target: Path, archive_id: str, project_root: Path) -> None:
    """Extract ``archive`` beside ``target`` and atomically rename it into place.

    Args:
        archive: The ``.tar.zst`` file.
        target: Where the folder must end up; must not exist.
        archive_id: Archive id, used to name the staging folder.
        project_root: Root that every path touched must stay inside.

    Raises:
        RestoreError: Extraction failed or ``target`` appeared meanwhile. The archive is
            untouched and no partial folder is left at ``target``.
    """
    staging = archiver.staging_path(target, archive_id)
    if os.path.lexists(_platform.long_path(staging)):
        archiver.safe_remove(staging, project_root)
    try:
        extract(archive, staging)
    except BaseException as exc:
        _discard(staging, project_root)
        if isinstance(exc, _EXTRACT_ERRORS):
            raise RestoreError(f"extracting {archive} failed: {exc}") from exc
        raise
    if os.path.lexists(_platform.long_path(target)):
        _discard(staging, project_root)
        raise RestoreError(f"{target} appeared while restoring; left it alone")
    os.rename(_platform.long_path(staging), _platform.long_path(target))  # noqa: PTH104
    _platform.fsync_dir(target.parent)


def wake(target: Path, *, index: Index) -> WakeResult:
    """Restore a hibernated folder.

    Args:
        target: The folder to bring back.
        index: Open archive index.

    Returns:
        RESTORED, SKIPPED_EXISTS (target already present; record marked STALE) or
        NOTHING_TO_DO (no active archive).

    Raises:
        RestoreError: The archive is corrupt or extraction failed; archive and index
            are unchanged.
    """
    record = index.find_active(target)
    if record is None:
        return WakeResult.NOTHING_TO_DO
    if os.path.lexists(_platform.long_path(record.target)):
        index.set_state(record.id, ArchiveState.STALE)
        return WakeResult.SKIPPED_EXISTS
    verify(record)
    assert record.archive_path is not None  # noqa: S101 - verify() checked it
    materialize(record.archive_path, record.target, record.id, record.project_root)
    index.set_state(record.id, ArchiveState.RESTORED)
    try:
        record.archive_path.unlink()
    except OSError as exc:
        logger.warning("restored %s but could not delete %s: %s", target, record.archive_path, exc)
    logger.info("restored %s", record.target)
    return WakeResult.RESTORED
