"""Walk a folder tree and yield the projects (and reclaimable targets) found in it."""

import fnmatch
import logging
import os
import stat
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from dormant.detectors import Detector, Kind
from dormant.platform import GIT_SAFE_CONFIG, IS_WINDOWS, long_path

logger = logging.getLogger(__name__)

IGNORE_FILE = ".dormantignore"
GIT_TIMEOUT = 30.0
_GLOB_CHARS = frozenset("*?[")
# ponytail: on Windows only the first N files of a target get a real stat for st_nlink;
# if none is hard-linked the rest are assumed not to be. A target whose hard links all
# sit past the sample is over-counted as reclaimable. Full stats cost 40 s per 1M files.
WINDOWS_NLINK_SAMPLE = 256


@dataclass(frozen=True, slots=True)
class FoundTarget:
    """A target folder that exists on disk.

    ``reclaimable_bytes`` counts only files with one hard link: deleting a file that
    has other links frees nothing. ``tracked`` targets hold git-tracked files and must
    never be archived or deleted.
    """

    path: Path
    kind: Kind
    detector: str
    reclaimable_bytes: int
    total_bytes: int
    hardlinked: bool
    tracked: bool


@dataclass(frozen=True, slots=True)
class Project:
    """A folder matched by one or more detectors, with the targets that exist in it."""

    root: Path
    detectors: tuple[Detector, ...]
    targets: tuple[FoundTarget, ...]


def scan(
    root: Path, detectors: Sequence[Detector], ignore: Sequence[str] = ()
) -> Iterator[Project]:
    """Yield every project under ``root``.

    Never follows symlinks or Windows junctions, never enters ``.git``, a detected
    target folder, or any folder named like a dependencies target (a stray
    ``node_modules`` whose parent isn't a project holds packages, not projects), and
    skips any folder holding a ``.dormantignore`` file (and all below it). Non-target
    subfolders of a project are still walked, so nested projects in a monorepo are found.

    Args:
        root: Folder to scan.
        detectors: Detectors to match folders against.
        ignore: Glob patterns; a folder whose name or full path matches is skipped.

    Yields:
        Projects in walk order. Projects whose targets are all absent have no targets.
    """
    start = Path(os.path.abspath(root))  # noqa: PTH100 - must not resolve symlinks
    never_enter = _dependency_dirs(detectors) | {".git"}
    stack = [(start, in_git_repo(start))]
    while stack:
        path, in_repo = stack.pop()
        entries = _list_dir(path)
        if entries is None or IGNORE_FILE in entries:
            continue
        in_repo = in_repo or ".git" in entries
        matched = tuple(d for d in detectors if any(_has_marker(m, entries) for m in d.markers))
        if matched:
            yield _project(path, matched, entries, in_repo)
        skip = {t.path for d in matched for t in d.targets} | never_enter
        for name, entry in entries.items():
            child = path / name
            if name in skip or not _is_real_dir(entry) or _ignored(child, ignore):
                continue
            stack.append((child, in_repo))


def _dependency_dirs(detectors: Sequence[Detector]) -> set[str]:
    return {t.path for d in detectors for t in d.targets if t.kind == "dependencies"}


def is_link(entry: os.DirEntry[str]) -> bool:
    """True for a symlink or a Windows junction (mount point), which must not be followed.

    Args:
        entry: A directory entry.

    Returns:
        Whether the entry is a link of either kind.
    """
    if entry.is_symlink():
        return True
    if sys.platform == "win32":
        tag = entry.stat(follow_symlinks=False).st_reparse_tag
        return tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    return False


def in_git_repo(path: Path) -> bool:
    """True when ``path`` or an ancestor holds a ``.git`` entry (folder or worktree file).

    Args:
        path: An absolute path.

    Returns:
        Whether the path is inside a git work tree.
    """
    return any((p / ".git").exists() for p in (path, *path.parents))


def _list_dir(path: Path) -> dict[str, os.DirEntry[str]] | None:
    try:
        with os.scandir(long_path(path)) as it:
            return {e.name: e for e in it}
    except OSError as exc:
        logger.debug("cannot read %s: %s", path, exc)
        return None


def _is_real_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False) and not is_link(entry)
    except OSError:
        return False


def _has_marker(marker: str, entries: dict[str, os.DirEntry[str]]) -> bool:
    want_dir = marker.endswith("/")
    name = marker.rstrip("/")
    if _GLOB_CHARS.isdisjoint(name):
        found = [entries[name]] if name in entries else []
    else:
        found = [entries[n] for n in fnmatch.filter(entries, name)]
    return any(_is_real_dir(e) for e in found) if want_dir else bool(found)


def _ignored(path: Path, patterns: Sequence[str]) -> bool:
    text = str(path)
    return any(fnmatch.fnmatch(path.name, p) or fnmatch.fnmatch(text, p) for p in patterns)


def _project(
    root: Path, matched: tuple[Detector, ...], entries: dict[str, os.DirEntry[str]], in_repo: bool
) -> Project:
    claims: dict[str, tuple[Kind, str]] = {}  # first detector to claim a folder wins
    for det in matched:
        for t in det.targets:
            if t.path in entries and _is_real_dir(entries[t.path]):
                claims.setdefault(t.path, (t.kind, det.name))
    tracked = _tracked(root, sorted(claims)) if in_repo and claims else set()
    targets = []
    for name, (kind, det_name) in claims.items():
        total, reclaimable = dir_size(entries[name].path)
        targets.append(
            FoundTarget(
                path=root / name,
                kind=kind,
                detector=det_name,
                reclaimable_bytes=reclaimable,
                total_bytes=total,
                hardlinked=total > 0 and reclaimable * 2 < total,
                tracked=name in tracked,
            )
        )
    return Project(root, matched, tuple(targets))


def dir_size(path: str) -> tuple[int, int]:
    """Measure a folder without following links.

    Args:
        path: Folder to measure (ideally already in long-path form).

    Returns:
        ``(total_bytes, reclaimable_bytes)``: all regular files, and only those with
        ``st_nlink == 1``.
    """
    total = reclaimable = 0
    sampled, stat_all = 0, not IS_WINDOWS  # POSIX gets nlink for free
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if is_link(e):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        probe = stat_all or sampled < WINDOWS_NLINK_SAMPLE
                        size, nlink = _size_nlink(e, probe=probe)
                        sampled += 1
                        stat_all = stat_all or nlink > 1
                        total += size
                        reclaimable += size if nlink == 1 else 0
        except OSError as exc:
            logger.warning("cannot measure inside %s: %s", path, exc)
    return total, reclaimable


def _size_nlink(entry: os.DirEntry[str], *, probe: bool) -> tuple[int, int]:
    st = entry.stat(follow_symlinks=False)
    if not IS_WINDOWS:
        return st.st_size, st.st_nlink
    # DirEntry.stat() reports st_nlink == 0 on Windows; only a real stat (a file open,
    # ~40 us warm and far worse cold under antivirus) knows. Past the sample, assume 1.
    if not probe:
        return st.st_size, 1
    return st.st_size, os.stat(entry.path, follow_symlinks=False).st_nlink  # noqa: PTH116


def _tracked(root: Path, names: list[str]) -> set[str]:
    """Return which of ``names`` (folders in ``root``) contain git-tracked files.

    One ``git ls-files`` call per project. If git is missing or fails inside a repo,
    every name counts as tracked: unknown means hands off.
    """
    cmd = ["git", *GIT_SAFE_CONFIG, "-C", str(root), "ls-files", "-z", "--", *names]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=GIT_TIMEOUT, check=True)  # noqa: S603
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git ls-files failed in %s, treating targets as tracked: %s", root, exc)
        return set(names)
    files = out.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return {f.split("/", 1)[0] for f in files if f}
