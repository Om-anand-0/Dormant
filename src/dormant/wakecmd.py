"""``dormant wake``: the fast path the shell hooks call on every marked ``cd``.

Never imports Typer or Textual. Rich, zstandard and the restorer load only after a
marker shows there is work. ``__main__`` routes ``dormant wake`` here directly.
"""

import argparse
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dormant import marker

if TYPE_CHECKING:  # pragma: no cover
    from rich.console import Console

    from dormant.config import Settings
    from dormant.index import ArchiveRecord, Index

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Ui:
    """Lazy Rich console: created (and Rich imported) on first output."""

    quiet: bool
    _console: "Console | None" = field(default=None)

    @property
    def console(self) -> "Console":
        if self._console is None:
            from rich.console import Console

            self._console = Console(highlight=False)
        return self._console

    def info(self, text: str) -> None:
        if not self.quiet:
            self.console.print(text)

    def error(self, text: str) -> None:
        from rich.console import Console
        from rich.markup import escape

        Console(stderr=True, highlight=False).print(f"[red]error:[/red] {escape(text)}")


def main(argv: Sequence[str]) -> int:
    """Parse ``wake`` arguments (without Typer) and run it.

    Args:
        argv: Arguments after ``wake``.

    Returns:
        The process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="dormant wake", description="Restore everything hibernated in a project."
    )
    parser.add_argument("path", nargs="?", default=".", help="Folder inside the project.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Print only on work or error.")
    parser.add_argument(
        "--reinstall", action="store_true", help="Ignore archives; reinstall from lockfiles."
    )
    args = parser.parse_args(list(argv))  # exits 2 on a usage error
    try:
        return run(Path(args.path), quiet=args.quiet, reinstall=args.reinstall)
    except KeyboardInterrupt:
        return 3
    except Exception as exc:  # the fast path's CLI boundary
        from dormant.errors import DormantError

        _Ui(quiet=False).error(str(exc))
        if not isinstance(exc, DormantError):
            logger.debug("wake failed", exc_info=True)
            return 1
        return exc.exit_code


def run(path: Path, *, quiet: bool = False, reinstall: bool = False) -> int:
    """Wake every ACTIVE or PURGED target of the project containing ``path``.

    Args:
        path: Any folder inside the project.
        quiet: Print nothing unless something is restored or fails.
        reinstall: Run reinstall commands instead of extracting archives.

    Returns:
        0 all done (or nothing to do), 4 partial, 1 everything failed.
    """
    root = marker.find_root(path)
    if root is None:
        return 0
    from dormant import archiver, config
    from dormant.index import ArchiveState, Index

    ui = _Ui(quiet)
    settings = config.load_settings()
    with Index(config.index_path()) as index:
        archiver.sweep_leftovers(root, index)
        live = (ArchiveState.ACTIVE, ArchiveState.PURGED)
        records = [r for r in index.for_project(root) if r.state in live]
        try:
            outcomes = [_wake_one(r, index, settings, ui, reinstall) for r in records]
        finally:
            marker.update(root, index)
    if not records:
        ui.info(f"Nothing to wake in {root}")
    return _exit_code(outcomes)


def _exit_code(outcomes: list[bool | None]) -> int:
    failed = outcomes.count(False)
    if not failed:
        return 0
    return 1 if failed == len([o for o in outcomes if o is not None]) else 4


def _wake_one(
    rec: "ArchiveRecord", index: "Index", settings: "Settings", ui: _Ui, reinstall: bool
) -> bool | None:
    """Returns True on success, False on failure, None when skipped."""
    from dormant.errors import DormantError
    from dormant.units import format_size

    name, size = rec.target.name, format_size(rec.source_bytes)
    if not _confirmed(rec, settings, ui):
        return None
    try:
        with ui.console.status(f"Waking {name} ({size})"):
            done = _restore(rec, index, reinstall)
    except (DormantError, OSError) as exc:
        ui.error(f"could not wake {rec.target}: {exc}")
        return False
    if done:
        ui.console.print(f"Woke {name} ({size}) in {rec.project_root}")
    else:
        ui.info(f"{name} already exists in {rec.project_root}; left it alone")
    return True


def _confirmed(rec: "ArchiveRecord", settings: "Settings", ui: _Ui) -> bool:
    from dormant.index import ArchiveState
    from dormant.units import format_size

    if rec.state != ArchiveState.ACTIVE or rec.source_bytes <= settings.confirm_restore_above:
        return True
    size = format_size(rec.source_bytes)
    if not sys.stdin.isatty():
        ui.console.print(
            f"Skipped {rec.target.name} ({size}): larger than confirm_restore_above. "
            f'Restore it with: dormant wake "{rec.project_root}"'
        )
        return False
    from rich.prompt import Confirm

    return Confirm.ask(f"Restore {rec.target.name} ({size}) in {rec.project_root}?")


def _restore(rec: "ArchiveRecord", index: "Index", reinstall: bool) -> bool:
    """Restore one record. Returns False when the folder was already there."""
    from dormant import purge, restorer
    from dormant.index import ArchiveState

    if rec.state == ArchiveState.PURGED:
        return purge.wake_purged(rec, index=index)
    command = purge.reinstall_for(rec.project_root, rec.target.name) if reinstall else None
    if command is None:
        if reinstall:
            logger.warning("no lockfile for %s; restoring from the archive", rec.target)
        return restorer.wake(rec.target, index=index) == restorer.WakeResult.RESTORED
    if rec.target.exists():
        index.set_state(rec.id, ArchiveState.STALE)
        return False
    purge.reinstall_active(rec, command, index=index)
    return True
