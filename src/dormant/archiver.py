"""Hibernate: archive a target folder to tar + zstd, verify it, then remove the source.

The step order is safety invariant 1 in CLAUDE.md. Each step is a module-level
function so crash tests can monkeypatch it.
"""

import hashlib
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO, cast

import zstandard

from dormant import config
from dormant import platform as _platform
from dormant.errors import ArchiveError, RestoreError, UnsafePathError
from dormant.index import ArchiveRecord, ArchiveState, Index

logger = logging.getLogger(__name__)

FORMAT_VERSION = 1
META_NAME = "meta.json"
DATA_PREFIX = "data/"
DIRLINK_KEY = "DORMANT.dirlink"  # pax header marking a directory symlink (Windows needs it)

_ESTIMATED_RATIO = 0.4  # archive size / source size, a rough guess for free-space checks
_SPACE_MARGIN = 1.5
_GIT_TIMEOUT = 30.0
_REMOVE_DELAYS = (0.1, 0.2, 0.4)  # sleep before each retry of a failed rmtree
_CHUNK = 1 << 20
_IO_REPARSE_TAG_MOUNT_POINT = (
    0xA0000003  # stat.IO_REPARSE_TAG_MOUNT_POINT, Windows-only in typeshed
)


@dataclass(frozen=True, slots=True)
class Entry:
    """One walked filesystem entry, as it will appear in the archive."""

    name: str  # archive member name: data/<relative posix path>
    type: bytes  # tarfile.REGTYPE, DIRTYPE or SYMTYPE
    size: int
    linkname: str
    dirlink: bool
    mode: int
    mtime: float
    path: str  # long-path form on disk, for reading
    stamp: tuple[int, int, int] = (0, 0, 0)  # (st_dev, st_ino, st_mtime_ns) at walk time

    def key(self) -> tuple[str, bytes, int, str]:
        """Return what verification compares against the archive's member list."""
        return (self.name, self.type, self.size, self.linkname)


# --- path safety --------------------------------------------------------------


def is_link(st: os.stat_result) -> bool:
    """Return True for a symlink or a Windows junction (from an lstat result)."""
    junction = getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT
    return stat.S_ISLNK(st.st_mode) or junction


def check_inside(path: Path, root: Path) -> None:
    """Refuse unless ``path`` is strictly inside ``root`` with no link on the way down.

    Every existing component from ``path`` up to (not including) ``root`` must be a real
    directory or file, never a symlink or junction, so an operation on ``path`` cannot
    land outside ``root``.

    Args:
        path: Path that is about to be archived or deleted.
        root: The directory it must stay inside.

    Raises:
        UnsafePathError: A check failed.
    """
    if not (path.is_absolute() and root.is_absolute()):
        raise UnsafePathError(f"paths must be absolute: {path}, {root}")
    p = Path(os.path.normcase(os.path.abspath(path)))  # noqa: PTH100 - must not resolve
    r = Path(os.path.normcase(os.path.abspath(root)))  # noqa: PTH100
    if p == r or not p.is_relative_to(r):
        raise UnsafePathError(f"{path} is not strictly inside {root}")
    cur = Path(os.path.abspath(path))  # noqa: PTH100
    for _ in p.relative_to(r).parts:
        try:
            st = os.lstat(_platform.long_path(cur))
        except FileNotFoundError:
            st = None
        if st is not None and is_link(st):
            raise UnsafePathError(f"{cur} is a symlink or junction; refusing to touch it")
        cur = cur.parent


def _canonical(target: Path, project_root: Path) -> Path:
    """Spell ``target`` below ``project_root`` the way the filesystem does.

    Fixes case, ``..`` and Windows 8.3 short names, so git, the index and wake all see
    one name. ``project_root`` keeps the caller's spelling (it may be a symlink).
    check_inside() already proved there is no link between the two.
    """
    real = Path(os.path.realpath(os.path.abspath(target)))  # noqa: PTH100
    real_root = Path(os.path.realpath(os.path.abspath(project_root)))  # noqa: PTH100
    if not real.is_relative_to(real_root) or real == real_root:
        raise UnsafePathError(f"{target} does not resolve to a folder inside {project_root}")
    return project_root.joinpath(*real.relative_to(real_root).parts)


def _overlaps(a: Path, b: Path) -> bool:
    ra, rb = Path(os.path.realpath(a)), Path(os.path.realpath(b))
    return ra.is_relative_to(rb) or rb.is_relative_to(ra)


def _validate(target: Path, project_root: Path, index: Index) -> Path:
    """Step 1: target is a real directory strictly inside project_root, not hibernated yet.

    Returns:
        The target, canonically spelled (see :func:`_canonical`).
    """
    check_inside(target, project_root)
    try:
        st = os.lstat(_platform.long_path(target))
    except FileNotFoundError as exc:
        raise UnsafePathError(f"{target} does not exist") from exc
    if is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise UnsafePathError(f"{target} is not a real directory")
    target = _canonical(target, project_root)
    # ponytail: checks the configured data dir only; an Index opened from some other
    # path is the caller's job to keep out of the target.
    if _overlaps(target, config.data_dir()):
        raise UnsafePathError(f"{target} overlaps Dormant's data dir {config.data_dir()}")
    if index.find_active(target) is not None:
        raise ArchiveError(f"{target} is already hibernated")
    return target


# --- git guard (invariant 2) ---------------------------------------------------


def _has_git_ancestor(target: Path) -> bool:
    return any(os.path.lexists(p / ".git") for p in target.parents)


def _run_git(git: str, cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    cmd = [git, *_platform.GIT_SAFE_CONFIG, "--literal-pathspecs", "-C", str(cwd), *args]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=_GIT_TIMEOUT, check=False)  # noqa: S603 - fixed argv, no shell
    except subprocess.TimeoutExpired as exc:
        raise UnsafePathError(f"git timed out checking {cwd}; refusing to continue") from exc


def _git_guard(target: Path) -> None:
    """Step 2: refuse a target that contains any git-tracked file.

    Git runs from the target's parent so the innermost enclosing repository answers.
    When git is missing or fails, any ``.git`` above the target means we cannot prove
    the target is untracked, so we refuse.
    """
    git = shutil.which("git")
    top = None if git is None else _run_git(git, target.parent, "rev-parse", "--show-toplevel")
    if git is None or top is None or top.returncode != 0:
        if _has_git_ancestor(target):
            why = "git is not installed" if top is None else top.stderr.decode(errors="replace")
            msg = f"{target} is inside a git repository but tracked files can't be checked: {why}"
            raise UnsafePathError(msg.strip())
        return
    # Run inside the target with no pathspec: git then matches by the directory itself, so
    # a different case or an 8.3 short name can't hide tracked files from a pathspec.
    ls = _run_git(git, target, "ls-files", "-z")
    if ls.returncode != 0:
        err = ls.stderr.decode(errors="replace").strip()
        raise UnsafePathError(f"git ls-files failed for {target}: {err}")
    if ls.stdout:
        raise UnsafePathError(f"{target} contains git-tracked files; refusing to archive it")


# --- step 3: walk ----------------------------------------------------------------------


def _classify(entry: os.DirEntry[str], rel: str) -> Entry:
    st = os.lstat(entry.path)
    if is_link(st) and not stat.S_ISLNK(st.st_mode):
        # ponytail: junctions are refused, not archived; add a junction member type if
        # real projects turn out to have them inside dependency folders.
        raise UnsafePathError(f"{entry.path} is a Windows junction; refusing to hibernate")
    if entry.name.casefold() == ".git":
        # ponytail: any nested .git refuses the whole target; a nested repo may hold
        # tracked files that `git ls-files` from the outer repo can't see.
        raise UnsafePathError(f"{entry.path} is a git repository inside the target")
    size, linkname, dirlink = 0, "", False
    if stat.S_ISLNK(st.st_mode):
        kind = tarfile.SYMTYPE
        linkname = os.readlink(entry.path)  # noqa: PTH115 - keep the raw string
        dirlink = bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY)
    elif stat.S_ISDIR(st.st_mode):
        kind = tarfile.DIRTYPE
    elif stat.S_ISREG(st.st_mode):
        kind, size = tarfile.REGTYPE, st.st_size
    else:
        raise ArchiveError(f"{entry.path} is not a file, directory or symlink")
    mode = stat.S_IMODE(st.st_mode)
    if "\\" in entry.name or ":" in entry.name:
        # Legal on POSIX, but restorer.member_path() refuses them (Windows-unsafe), so the
        # archive could never be woken. Refuse up front instead.
        raise UnsafePathError(f"{entry.path}: name with '' or ':' can't be restored")
    stamp = (st.st_dev, st.st_ino, st.st_mtime_ns)
    return Entry(
        DATA_PREFIX + rel, kind, size, linkname, dirlink, mode, st.st_mtime, entry.path, stamp
    )


def _check_unchanged(entry: Entry, fh: BinaryIO) -> None:
    """Refuse a file that was swapped (e.g. for a symlink) or modified since the walk."""
    st = os.fstat(fh.fileno())
    if (st.st_dev, st.st_ino, st.st_mtime_ns) != entry.stamp or st.st_size != entry.size:
        raise ArchiveError(f"{entry.path} changed while it was being archived; try again")


def walk(target: Path) -> tuple[list[Entry], int]:
    """Step 3: list everything under ``target`` without following links.

    Args:
        target: Directory to walk.

    Returns:
        Entries (parents before children, sorted by name per directory) and the total
        size in bytes of the regular files.

    Raises:
        UnsafePathError: The tree contains a junction or a nested ``.git``.
        ArchiveError: The tree contains a special file (socket, device, fifo).
    """
    entries: list[Entry] = []
    stack = [(str(_platform.long_path(target)), "")]
    while stack:
        dir_path, prefix = stack.pop()
        with os.scandir(dir_path) as it:
            children = sorted(it, key=lambda e: e.name)
        subdirs: list[tuple[str, str]] = []
        for child in children:
            entry = _classify(child, prefix + child.name)
            entries.append(entry)
            if entry.type == tarfile.DIRTYPE:
                subdirs.append((child.path, prefix + child.name + "/"))
        stack.extend(reversed(subdirs))
    return entries, sum(e.size for e in entries)


def _check_free_space(archive_dir: Path, source_bytes: int) -> None:
    """Step 3b: refuse when the archive store can't hold the estimated archive."""
    need = _SPACE_MARGIN * _ESTIMATED_RATIO * source_bytes
    free = _platform.free_bytes(archive_dir)
    if free < need:
        raise ArchiveError(f"not enough free space in {archive_dir}: {free} bytes, need {need:.0f}")


# --- step 4: write -----------------------------------------------------------------


class _HashingSink:
    """Write-only file wrapper that hashes every byte on its way to disk."""

    def __init__(self, f: BinaryIO) -> None:
        self._f = f
        self.hasher = hashlib.blake2b()

    def write(self, data: bytes) -> int:
        self.hasher.update(data)
        return self._f.write(data)

    def flush(self) -> None:
        self._f.flush()


def _add_entry(tar: tarfile.TarFile, entry: Entry) -> None:
    info = tarfile.TarInfo(entry.name)
    info.type = entry.type
    info.mode = entry.mode
    info.mtime = entry.mtime
    if entry.type == tarfile.SYMTYPE:
        info.linkname = entry.linkname
        if entry.dirlink:
            info.pax_headers = {DIRLINK_KEY: "1"}
    if entry.type != tarfile.REGTYPE:
        tar.addfile(info)
        return
    info.size = entry.size
    with open(entry.path, "rb") as fh:  # noqa: PTH123 - long-path string
        _check_unchanged(entry, fh)
        tar.addfile(info, fh)
        # ponytail: a same-size write that also keeps mtime slips through; hashing each
        # file twice would catch it at double the read cost.
        _check_unchanged(entry, fh)


def _meta(target: Path, project_root: Path, created_at: float, entries: list[Entry]) -> bytes:
    meta = {
        "format": FORMAT_VERSION,
        "target": str(target),
        "project_root": str(project_root),
        "created_at": created_at,
        "entries": len(entries),
        "source_bytes": sum(e.size for e in entries),
    }
    return json.dumps(meta, indent=2).encode()


def write_archive(tmp: Path, meta: bytes, entries: list[Entry], level: int) -> str:
    """Step 4: stream ``meta.json`` plus every entry into ``tmp`` and fsync it.

    Args:
        tmp: Archive path to create; must not exist.
        meta: Contents of the ``meta.json`` member.
        entries: Output of :func:`walk`.
        level: zstd compression level.

    Returns:
        BLAKE2b hex digest of the compressed bytes written.
    """
    with tmp.open("xb") as f:
        sink = _HashingSink(f)
        cctx = zstandard.ZstdCompressor(level=level, write_checksum=True)
        with (
            cctx.stream_writer(cast("IO[bytes]", sink), closefd=False) as zw,
            tarfile.open(fileobj=zw, mode="w|", format=tarfile.PAX_FORMAT) as tar,
        ):
            info = tarfile.TarInfo(META_NAME)
            info.size = len(meta)
            info.mtime = time.time()
            tar.addfile(info, io.BytesIO(meta))
            for entry in entries:
                _add_entry(tar, entry)
        f.flush()
        os.fsync(f.fileno())
    return sink.hasher.hexdigest()


# --- step 5: verify ----------------------------------------------------------------


def file_checksum(path: Path) -> str:
    """Return the BLAKE2b hex digest of a file, read from disk in chunks.

    Args:
        path: File to hash.

    Returns:
        Hex digest.
    """
    h = hashlib.blake2b()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_archive(tmp: Path, checksum: str, entries: list[Entry]) -> None:
    """Step 5: re-read ``tmp`` from disk; checksum and member list must match.

    Args:
        tmp: The archive just written.
        checksum: Digest returned by :func:`write_archive`.
        entries: Output of :func:`walk`.

    Raises:
        ArchiveError: Any mismatch.
    """
    if file_checksum(tmp) != checksum:
        raise ArchiveError(f"checksum mismatch re-reading {tmp}")
    with (
        tmp.open("rb") as f,
        zstandard.ZstdDecompressor().stream_reader(f) as zr,
        tarfile.open(fileobj=zr, mode="r|") as tar,
    ):
        got = [(m.name, m.type, m.size, m.linkname) for m in tar]
        while zr.read(_CHUNK):  # drain to the frame end so zstd checks its own checksum
            pass
    if not got or got[0][0] != META_NAME or got[1:] != [e.key() for e in entries]:
        raise ArchiveError(f"member list in {tmp} does not match the source folder")


def _publish(tmp: Path, final: Path) -> None:
    """Step 6: atomic rename into place, then make the rename durable."""
    tmp.replace(final)
    _platform.fsync_dir(final.parent)


def _make_archive(target: Path, project_root: Path, kind: str, level: int) -> ArchiveRecord:
    """Steps 3 to 6. On any failure the tmp file is gone and nothing else exists."""
    archive_id = uuid.uuid4().hex
    final = config.archive_dir() / f"{archive_id}.tar.zst"
    tmp = final.with_name(final.name + ".tmp")
    created_at = time.time()
    try:
        entries, source_bytes = walk(target)
        _check_free_space(final.parent, source_bytes)
        meta = _meta(target, project_root, created_at, entries)
        checksum = write_archive(tmp, meta, entries, level)
        verify_archive(tmp, checksum, entries)
        _publish(tmp, final)
        archive_bytes = final.stat().st_size
    except BaseException as exc:
        # final has a fresh id, so if the rename happened it is ours and unindexed.
        tmp.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        if isinstance(exc, (OSError, tarfile.TarError, zstandard.ZstdError)):
            raise ArchiveError(f"archiving {target} failed: {exc}") from exc
        raise
    return ArchiveRecord(
        id=archive_id,
        project_root=project_root,
        target=target,
        kind=kind,
        archive_path=final,
        checksum=checksum,
        source_bytes=source_bytes,
        archive_bytes=archive_bytes,
        created_at=created_at,
        format_version=FORMAT_VERSION,
        state=ArchiveState.ACTIVE,
    )


def _record(record: ArchiveRecord, index: Index) -> None:
    """Step 7: index the archive. hibernate() rolls back if this or anything later fails."""
    index.add(record)


def _rollback(record: ArchiveRecord, index: Index) -> None:
    """Forget an archive whose source is still whole: row first, then the file."""
    index.delete(record.id)  # if this raises, the file stays so the row never dangles
    _discard_archive(record)


def _discard_archive(record: ArchiveRecord) -> None:
    if record.archive_path is None:
        return
    try:
        record.archive_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("could not delete orphaned archive %s: %s", record.archive_path, exc)


# --- step 8: remove the source -------------------------------------------------------


def _exists(path: Path) -> bool:
    return os.path.lexists(_platform.long_path(path))


def _trash_path(record: ArchiveRecord) -> Path:
    return record.target.with_name(f"{record.target.name}.dormant-trash-{record.id}")


def staging_path(target: Path, archive_id: str) -> Path:
    """Return the folder wake extracts into before renaming it to ``target``."""
    return target.with_name(f"{target.name}.dormant-restoring-{archive_id}")


def _move_to_trash(record: ArchiveRecord, index: Index) -> Path:
    """Step 8a: rename the target aside. If that fails, roll back and leave it untouched."""
    trash = _trash_path(record)
    try:
        os.rename(_platform.long_path(record.target), _platform.long_path(trash))  # noqa: PTH104
    except OSError as exc:
        if not os.path.lexists(_platform.long_path(record.target)):
            # The rename may have happened anyway; keep the archive so nothing is lost.
            raise ArchiveError(f"moving {record.target} aside failed oddly: {exc}") from exc
        _rollback(record, index)
        msg = f"{record.target} is in use (could not move it: {exc}); it was left untouched"
        raise ArchiveError(msg) from exc
    _platform.fsync_dir(record.target.parent)
    return trash


def _undo_partial_remove(record: ArchiveRecord, trash: Path, index: Index) -> None:
    """Invariant 6: put the target back from the archive after a failed delete."""
    from dormant import restorer  # local import: restorer imports this module

    if record.archive_path is None:  # pragma: no cover - hibernate always sets it
        raise ArchiveError(f"no archive to restore {record.target} from")
    try:
        restorer.materialize(record.archive_path, record.target, record.id, record.project_root)
    except (RestoreError, OSError) as exc:
        msg = (
            f"deleting {record.target} failed and restoring it failed too; its contents are "
            f"safe in {record.archive_path} (archive id {record.id}), run wake to restore"
        )
        raise ArchiveError(msg) from exc
    _rollback(record, index)
    try:
        safe_remove(trash, record.project_root)
    except (OSError, UnsafePathError) as exc:
        logger.warning("could not clean up %s: %s", trash, exc)


def _remove_source(record: ArchiveRecord, index: Index) -> None:
    """Step 8: rename-then-delete the source folder."""
    trash = _move_to_trash(record, index)
    try:
        safe_remove(trash, record.project_root)
    except (OSError, UnsafePathError) as exc:
        _undo_partial_remove(record, trash, index)
        msg = f"could not delete {record.target} ({exc}); it was restored from the archive"
        raise ArchiveError(msg) from exc


# --- safe_remove -----------------------------------------------------------------------


def _clear_readonly(path: str, top: str) -> None:
    """Make ``path`` and its parent writable, if inside ``top``. Never touches links."""
    top_key = os.path.normcase(top)
    for p in (path, os.path.dirname(path)):  # noqa: PTH120
        key = os.path.normcase(p)
        if key != top_key and not key.startswith(top_key + os.sep):
            continue
        st = os.lstat(p)
        if not is_link(st):
            os.chmod(p, st.st_mode | stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)  # noqa: PTH101


def _rmtree(path: Path) -> None:
    top = str(path)

    def retry(func: Callable[..., object], p: str, exc: BaseException) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        if func not in (os.unlink, os.rmdir, os.remove) or not isinstance(exc, PermissionError):
            raise exc
        _clear_readonly(p, top)
        func(p)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:  # pragma: no cover - only the 3.11 run takes this branch
        shutil.rmtree(path, onerror=lambda f, p, ei: retry(f, p, ei[1]))


def safe_remove(path: Path, root: Path) -> None:
    """Delete the directory ``path``, which must be strictly inside ``root``.

    This is the only function in Dormant allowed to call ``shutil.rmtree``. It never
    follows symlinks or junctions, so nothing outside ``path`` can be deleted.

    Args:
        path: Directory to delete.
        root: Directory that must contain it.

    Raises:
        UnsafePathError: ``path`` is not absolute, not strictly inside ``root``, or is
            (or sits below) a symlink or junction.
        OSError: The delete still failed after retries.
    """
    check_inside(path, root)
    target = _platform.long_path(path)
    for attempt, delay in enumerate((*_REMOVE_DELAYS, None), start=1):
        try:
            _rmtree(target)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if delay is None:
                raise
            logger.debug("rmtree %s failed (try %d): %s", path, attempt, exc)
            time.sleep(delay)


# --- leftovers --------------------------------------------------------------------------


def _archive_verifies(record: ArchiveRecord) -> bool:
    if record.state != ArchiveState.ACTIVE or record.archive_path is None:
        return False
    try:
        return file_checksum(record.archive_path) == record.checksum
    except OSError:
        return False


def sweep_leftovers(project_root: Path, index: Index) -> list[Path]:
    """Delete trash and staging folders that interrupted runs left in a project.

    A ``<target>.dormant-trash-<id>`` folder goes only when record ``<id>`` is ACTIVE and
    its archive verifies, so the data is safe. A ``<target>.dormant-restoring-<id>``
    folder goes when the target exists or the record is ACTIVE. Folders whose id is not
    in the index are never touched.

    Args:
        project_root: Project whose index records are checked.
        index: Open archive index.

    Returns:
        The folders removed.
    """
    removed: list[Path] = []
    for rec in index.for_project(project_root):
        trash = _trash_path(rec)
        if _exists(trash) and _archive_verifies(rec):
            safe_remove(trash, rec.project_root)
            removed.append(trash)
        staging = staging_path(rec.target, rec.id)
        if _exists(staging) and (_exists(rec.target) or rec.state == ArchiveState.ACTIVE):
            safe_remove(staging, rec.project_root)
            removed.append(staging)
    return removed


# --- public API --------------------------------------------------------------------------


def hibernate(
    target: Path, project_root: Path, kind: str, *, index: Index, level: int = 3
) -> ArchiveRecord:
    """Archive ``target`` durably, index it, then delete it.

    Args:
        target: Folder to hibernate, strictly inside ``project_root``.
        project_root: The project that owns it; nothing outside it is ever deleted.
        kind: Detector kind (``dependencies``, ``build`` or ``cache``).
        index: Open archive index.
        level: zstd compression level.

    Returns:
        The ACTIVE index record.

    Raises:
        UnsafePathError: The target fails a safety check (path, links, git-tracked).
        ArchiveError: Archiving or removal failed; the source folder is whole.
    """
    target = _validate(target, project_root, index)
    _git_guard(target)
    record = _make_archive(target, project_root, kind, level)
    try:
        _record(record, index)
        _remove_source(record, index)
    except BaseException:
        # Source never moved aside: it is whole, so the archive and row must go too, or
        # the index would claim a hibernated folder that is still there. Otherwise keep
        # both; sweep_leftovers() clears the trash once the archive verifies.
        if _exists(target) and not _exists(_trash_path(record)):
            _rollback(record, index)
        raise
    logger.info("hibernated %s -> %s", target, record.archive_path)
    return record
