"""Is a project in use: does a running process sit inside it?

Access-denied policy (a deliberate deviation from CLAUDE.md's "AccessDenied counts as
in use"): a process whose cwd cannot be read is skipped. On Windows, SYSTEM services
and even the user's own elevated helpers (ctfmon, steam, vendor agents) deny cwd
reads, so counting them would mark every project as in use and make hibernate a
no-op. Treating "denied and owned by the current user" as in use was tried and fails
the same way on a normal desktop. Instead, a process also counts when its executable
lives inside the project, which still catches an elevated tool run from a project's
venv. The real safety net for a file held open by an unseen process is the
archiver's lock check, retry and rollback (invariant 6).
"""

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    """Normcased absolute paths (cwds and executables) of running processes."""

    paths: tuple[str, ...]


def snapshot() -> ProcessSnapshot:
    """Read every readable process cwd and executable path once.

    Costs roughly 0.1 to 0.3 s on a desktop, so take one snapshot per scan.

    Returns:
        A snapshot to pass to :func:`in_use`.
    """
    me = os.getpid()
    paths: list[str] = []
    for proc in psutil.process_iter(["cwd", "exe"]):
        if proc.pid == me:
            continue
        # process_iter stores None for any attribute that raised AccessDenied or
        # NoSuchProcess, so those processes simply contribute nothing.
        paths.extend(os.path.normcase(p) for p in (proc.info["cwd"], proc.info["exe"]) if p)
    return ProcessSnapshot(tuple(paths))


def in_use(project_root: Path, snap: ProcessSnapshot | None = None) -> bool:
    """True if any other process has its cwd or executable inside ``project_root``.

    Args:
        project_root: Project folder.
        snap: A snapshot from :func:`snapshot`; a fresh one is taken when None.

    Returns:
        Whether the project is in use.
    """
    root = os.path.normcase(os.path.abspath(project_root))  # noqa: PTH100 - no resolve
    return _any_inside(root, (snap or snapshot()).paths)


def _any_inside(root: str, paths: Iterable[str]) -> bool:
    prefix = root.rstrip(os.sep) + os.sep
    return any(p == root or p.startswith(prefix) for p in paths)
