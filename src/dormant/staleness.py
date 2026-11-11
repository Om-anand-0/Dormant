"""When was a project last worked on: newest git commit or source-file mtime."""

import logging
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from dormant.platform import long_path
from dormant.scanner import GIT_TIMEOUT, in_git_repo, is_link

logger = logging.getLogger(__name__)

_SKIP_NAMES = frozenset({".git", ".dormant"})


def last_activity(project_root: Path, exclude: Iterable[Path] = ()) -> float:
    """Return the newest of the last git commit touching the project and its newest file.

    File access times are ignored (``relatime`` and Windows make them unreliable).

    Args:
        project_root: Project folder.
        exclude: Folders to skip, usually every target under the project.

    Returns:
        A POSIX timestamp, or 0.0 if nothing was found.
    """
    return max(_git_commit_time(project_root), _newest_mtime(project_root, exclude))


def _git_commit_time(root: Path) -> float:
    if not in_git_repo(root):
        return 0.0
    cmd = ["git", "-C", str(root), "log", "-1", "--format=%ct", "--", "."]
    try:
        out = subprocess.run(  # noqa: S603 - fixed argument list, no shell
            cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=True
        )
        return float(out.stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.debug("no git commit time for %s: %s", root, exc)
        return 0.0


def _newest_mtime(root: Path, exclude: Iterable[Path]) -> float:
    skip = {os.path.normcase(long_path(p)) for p in exclude}
    newest = 0.0
    stack = [str(long_path(root))]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    if is_link(e) or e.name in _SKIP_NAMES or os.path.normcase(e.path) in skip:
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                    elif e.is_file(follow_symlinks=False):
                        newest = max(newest, e.stat(follow_symlinks=False).st_mtime)
        except OSError as exc:
            logger.debug("cannot read inside %s: %s", root, exc)
    return newest
