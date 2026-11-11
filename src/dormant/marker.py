"""The ``<project>/.dormant/state`` marker the shell hooks look for.

It exists exactly when the project has at least one ACTIVE or PURGED index record.
This module is the only code that writes inside a user's project outside a target
folder, and only under ``.dormant/`` (plus one line in the repo's ``.git/info/exclude``).
"""

import json
import logging
import time
from pathlib import Path

from dormant.errors import UnsafePathError
from dormant.index import ArchiveState, Index
from dormant.platform import is_link_path

logger = logging.getLogger(__name__)

DIR_NAME = ".dormant"
STATE_NAME = "state"
EXCLUDE_LINE = ".dormant/"
_LIVE = (ArchiveState.ACTIVE, ArchiveState.PURGED)


def state_path(project_root: Path) -> Path:
    """Return ``<project_root>/.dormant/state``."""
    return project_root / DIR_NAME / STATE_NAME


def find_root(start: Path) -> Path | None:
    """Walk up from ``start`` to the nearest folder holding a marker.

    Args:
        start: Folder to start from.

    Returns:
        The project root, or None when no ancestor has a marker.
    """
    here = Path(start).absolute()
    return next((p for p in (here, *here.parents) if state_path(p).is_file()), None)


def update(project_root: Path, index: Index) -> None:
    """Write or remove the marker to match the project's index records.

    Args:
        project_root: Project folder.
        index: Open archive index.
    """
    live = [r for r in index.for_project(project_root) if r.state in _LIVE]
    if live:
        _write(project_root, sorted(r.target.name for r in live))
    else:
        _remove(project_root)


def _refuse_links(*paths: Path) -> None:
    """A cloned repo could ship these as links to make us write outside the project."""
    for p in paths:
        if is_link_path(p):
            raise UnsafePathError(f"{p} is a symlink or junction; refusing to write through it")


def _write(project_root: Path, targets: list[str]) -> None:
    folder = project_root / DIR_NAME
    _refuse_links(folder, folder / STATE_NAME)
    folder.mkdir(exist_ok=True)
    info = {"version": 1, "updated_at": time.time(), "targets": targets}
    (folder / STATE_NAME).write_text(json.dumps(info) + "\n", encoding="utf-8")
    try:
        exclude_from_git(project_root)
    except (OSError, UnsafePathError) as exc:
        logger.warning("could not add %s to git's exclude file: %s", EXCLUDE_LINE, exc)


def _remove(project_root: Path) -> None:
    _refuse_links(project_root / DIR_NAME)
    state_path(project_root).unlink(missing_ok=True)
    try:
        (project_root / DIR_NAME).rmdir()
    except FileNotFoundError:
        pass
    except OSError as exc:  # not empty: something else lives there, leave it
        logger.debug("kept %s: %s", project_root / DIR_NAME, exc)


def git_dir(start: Path) -> Path | None:
    """Return the git directory whose ``info/exclude`` applies to ``start``.

    Handles a ``.git`` file (worktrees, submodules) by following its ``gitdir:`` line,
    and a worktree's ``commondir`` file, since git reads ``info/exclude`` from there.

    Args:
        start: A folder inside the work tree.

    Returns:
        The git directory, or None outside a repository.
    """
    for folder in (start, *start.parents):
        dot_git = folder / ".git"
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            return _follow_gitfile(dot_git)
    return None


def _follow_gitfile(gitfile: Path) -> Path | None:
    text = gitfile.read_text(encoding="utf-8", errors="replace").strip()
    if not text.startswith("gitdir:"):
        return None
    gitdir = gitfile.parent / text.removeprefix("gitdir:").strip()
    common = gitdir / "commondir"
    if common.is_file():
        gitdir = gitdir / common.read_text(encoding="utf-8").strip()
    return Path(gitdir).resolve() if gitdir.is_dir() else None


def exclude_from_git(project_root: Path) -> None:
    """Add ``.dormant/`` to the repository's ``info/exclude`` once (never ``.gitignore``).

    Args:
        project_root: Project folder; nothing happens outside a git repository.
    """
    gdir = git_dir(project_root)
    if gdir is None:
        return
    exclude = gdir / "info" / "exclude"
    _refuse_links(exclude.parent, exclude)
    old = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if EXCLUDE_LINE in old.splitlines():
        return
    exclude.parent.mkdir(exist_ok=True)
    sep = "" if not old or old.endswith("\n") else "\n"
    with exclude.open("a", encoding="utf-8") as f:
        f.write(f"{sep}{EXCLUDE_LINE}\n")
