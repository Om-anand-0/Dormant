"""OS and WSL detection plus small filesystem helpers that differ per platform."""

import logging
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

IS_WINDOWS: bool = sys.platform == "win32"
IS_MACOS: bool = sys.platform == "darwin"
IS_LINUX: bool = sys.platform.startswith("linux")

_EXTENDED_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\"


def _read_proc_version() -> str:
    return Path("/proc/version").read_text(encoding="utf-8", errors="replace")


def is_wsl() -> bool:
    """Return True when running under Windows Subsystem for Linux.

    Returns:
        True on Linux when ``/proc/version`` mentions Microsoft, else False.
    """
    if not IS_LINUX:
        return False
    try:
        return "microsoft" in _read_proc_version().lower()
    except OSError:
        return False


def _to_extended(p: str) -> str:
    """Convert an absolute Windows path string to its ``\\\\?\\`` extended-length form.

    Args:
        p: Absolute Windows path, drive-letter or UNC.

    Returns:
        The extended-length path; already-prefixed paths come back unchanged.
    """
    if p.startswith(_EXTENDED_PREFIX):
        return p
    if p.startswith(_UNC_PREFIX):
        return _EXTENDED_PREFIX + "UNC\\" + p[len(_UNC_PREFIX) :]
    return _EXTENDED_PREFIX + p


def long_path(path: Path) -> Path:
    """Return an absolute path that survives Windows' 260-character limit.

    Symlinks are not resolved.

    Args:
        path: Any path.

    Returns:
        On Windows, the ``\\\\?\\`` extended form of the absolute path; elsewhere the
        absolute path.
    """
    if IS_WINDOWS:
        raw = str(path)
        if raw.startswith(_EXTENDED_PREFIX):
            return path
        return Path(_to_extended(os.path.abspath(raw)))  # noqa: PTH100 - must not resolve
    return Path(os.path.abspath(path))  # noqa: PTH100 - must not resolve symlinks


def fsync_dir(path: Path) -> None:
    """Flush a directory entry to disk so a preceding rename survives a crash.

    No-op on Windows, where directories cannot be opened this way.

    Args:
        path: Directory to flush.
    """
    if IS_WINDOWS:
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def free_bytes(path: Path) -> int:
    """Return free bytes on the volume holding ``path``.

    Args:
        path: A path that may not exist yet; its nearest existing ancestor is used.

    Returns:
        Free bytes available on that volume.
    """
    probe = Path(os.path.abspath(path))  # noqa: PTH100 - must not resolve symlinks
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def symlinks_supported(probe_dir: Path) -> bool:
    """Check whether this process can create symlinks in ``probe_dir``.

    Windows needs Developer Mode or admin rights for this.

    Args:
        probe_dir: Existing writable directory to probe in.

    Returns:
        True if a symlink could be created.
    """
    link = probe_dir / f".dormant-symlink-probe-{uuid.uuid4().hex}"
    try:
        link.symlink_to(probe_dir.name)
    except OSError as exc:
        logger.debug("symlink probe failed in %s: %s", probe_dir, exc)
        return False
    link.unlink()
    return True


# A repository's own .git/config can make even read-only git commands run programs:
# core.fsmonitor on index reads, gpg.*program when log verifies signatures. Dormant runs
# git inside untrusted clones, so every such key is overridden on the command line
# (command-line -c beats repo config). Only rev-parse, ls-files and log are ever run.
GIT_SAFE_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "log.showSignature=false",
    "-c",
    "gpg.program=false",
    "-c",
    "gpg.ssh.program=false",
    "-c",
    "gpg.x509.program=false",
)

_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003


def is_link_path(path: Path) -> bool:
    """Return True if ``path`` is a symlink or Windows junction (False if missing)."""
    try:
        st = os.lstat(long_path(path))
    except FileNotFoundError:
        return False
    junction = getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT
    return stat.S_ISLNK(st.st_mode) or junction
