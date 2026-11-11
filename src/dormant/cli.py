"""Typer app. Thin: parses arguments and calls into core modules."""

import functools
import json
import logging
import re
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, ParamSpec, TypeVar

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from dormant import __version__, archiver, config, hibernation, hooks, marker, prune, wakecmd
from dormant import platform as _platform
from dormant.errors import DormantError
from dormant.hibernation import Action, Op
from dormant.index import ArchiveRecord, ArchiveState, Index
from dormant.survey import Row, survey
from dormant.units import format_size, parse_duration

logger = logging.getLogger(__name__)
app = typer.Typer(no_args_is_help=True, add_completion=False)
out = Console(highlight=False)
err = Console(stderr=True, highlight=False)

_P = ParamSpec("_P")
_R = TypeVar("_R")

_KIND_ALIASES = {
    "deps": "dependencies",
    "dependencies": "dependencies",
    "build": "build",
    "cache": "cache",
}
_WSL_DRIVE = re.compile(r"^/mnt/[a-zA-Z](/|$)")
_DONE = {Op.ARCHIVE: "archived", Op.PURGE: "purged", Op.DELETE: "deleted"}

PathArg = Annotated[Path, typer.Argument(exists=True, file_okay=False, help="Folder to scan.")]
OlderOpt = Annotated[
    str | None,
    typer.Option(help="Only projects idle this long: 48h, 60d, 12w, 6m, 1y (config: older_than)."),
]
KindOpt = Annotated[str | None, typer.Option(help="Comma list of deps, build, cache.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON (see docs/json.md).")]
YesOpt = Annotated[bool, typer.Option("--yes", "-y", help="Act without asking.")]


def _guard(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """The CLI boundary: map Dormant errors and Ctrl-C to exit codes."""

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return func(*args, **kwargs)
        except (typer.Exit, typer.TyperException):
            raise
        except (KeyboardInterrupt, typer.Abort):
            err.print("aborted")
            raise typer.Exit(3) from None
        except DormantError as exc:
            err.print(f"[red]error:[/red] {escape(str(exc))}")
            raise typer.Exit(exc.exit_code) from exc
        except Exception as exc:  # the one allowed bare catch: the CLI boundary
            logger.exception("unexpected error")
            raise typer.Exit(1) from exc

    return wrapper


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"dormant {__version__}")
        raise typer.Exit


@app.callback()
def root(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Archive dependency and build folders in stale projects."""


# --- scan -------------------------------------------------------------------------------


@app.command()
@_guard
def scan(
    path: PathArg = Path(),
    older: OlderOpt = None,
    kind: KindOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """List projects, their reclaimable folders, sizes and last activity."""
    max_age = _parse_older(older)
    kinds = _parse_kinds(kind)
    _warn_wsl(path)
    rows = survey(path, kinds, max_age, config.load_settings().ignore)
    if json_out:
        _write_json([_row_json(r) for r in rows])
    else:
        _print_table(rows, Path(path).absolute())


def _write_json(data: object) -> None:
    sys.stdout.write(json.dumps(data, indent=2) + "\n")


def _parse_older(older: str | None) -> timedelta | None:
    try:
        return parse_duration(older) if older is not None else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--older") from exc


def _parse_kinds(kind: str | None) -> set[str]:
    if kind is None:
        return set(_KIND_ALIASES.values())
    words = [w.strip().lower() for w in kind.split(",") if w.strip()]
    bad = [w for w in words if w not in _KIND_ALIASES]
    if bad or not words:
        raise typer.BadParameter(
            f"unknown kind(s) {bad}; use deps, build, cache", param_hint="--kind"
        )
    return {_KIND_ALIASES[w] for w in words}


def _warn_wsl(path: Path) -> None:
    if _platform.is_wsl() and _WSL_DRIVE.match(Path(path).absolute().as_posix()):
        err.print(
            "[yellow]warning:[/yellow] scanning a Windows drive from WSL is slow; "
            "install Dormant on Windows for Windows-side projects"
        )


def _iso(ts: float) -> str | None:
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z") if ts else None


def _row_json(row: Row) -> dict[str, object]:
    p = row.project
    return {
        "root": str(p.root),
        "detectors": [d.name for d in p.detectors],
        "last_activity": _iso(row.last_activity),
        "in_use": row.in_use,
        "reclaimable_bytes": row.reclaimable,
        "targets": [
            {
                "path": str(t.path),
                "kind": t.kind,
                "detector": t.detector,
                "reclaimable_bytes": t.reclaimable_bytes,
                "total_bytes": t.total_bytes,
                "hardlinked": t.hardlinked,
                "tracked": t.tracked,
            }
            for t in p.targets
        ],
    }


def _rel(path: Path, base: Path) -> str:
    return str(path.relative_to(base)) if path.is_relative_to(base) else str(path)


def _print_table(rows: list[Row], base: Path) -> None:
    table = Table(
        "Project", "Ecosystems", "Target", "Kind", "Reclaimable", "Last activity", "Notes"
    )
    flat = [(r, t) for r in rows for t in r.project.targets]
    for r, t in sorted(flat, key=lambda rt: rt[1].reclaimable_bytes, reverse=True):
        notes = [n for n, on in (("tracked", t.tracked), ("in use", r.in_use)) if on]
        notes += ["hard-linked"] if t.hardlinked else []
        table.add_row(
            _rel(r.project.root, base),
            ", ".join(d.name for d in r.project.detectors),
            t.path.name,
            t.kind,
            format_size(t.reclaimable_bytes),
            _age(r.last_activity),
            ", ".join(notes),
        )
    out.print(table)
    total = sum(r.reclaimable for r in rows)
    out.print(f"{len(flat)} targets in {len(rows)} projects, {format_size(total)} reclaimable")


def _age(ts: float) -> str:
    if not ts:
        return "unknown"
    days = int((time.time() - ts) // 86400)
    return f"{datetime.fromtimestamp(ts).date().isoformat()} ({days}d ago)"


# --- hibernate ---------------------------------------------------------------------------


@app.command()
@_guard
def hibernate(
    path: PathArg = Path(),
    older: OlderOpt = None,
    kind: KindOpt = None,
    purge: Annotated[
        bool,
        typer.Option(
            "--purge",
            help="Delete dependency folders that have a lockfile (uv.lock, package-lock.json, "
            "...) instead of archiving them; wake reinstalls them. Others are archived.",
        ),
    ] = False,
    level: Annotated[
        int | None, typer.Option(min=1, max=19, help="zstd level (config: compression_level).")
    ] = None,
    yes: YesOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Archive and remove folders in stale projects. Previews and asks first.

    Caches are deleted, dependencies and build output are archived. Projects in use
    and folders with git-tracked files are skipped.
    """
    settings = config.load_settings()
    max_age = _parse_older(older) or settings.older_than
    kinds = _parse_kinds(kind)
    _warn_wsl(path)
    rows = survey(path, kinds, max_age, settings.ignore)
    actions = hibernation.plan(rows, purge_deps=purge, min_archive_bytes=settings.min_archive_size)
    todo = [a for a in actions if a.op != Op.SKIP]
    base = Path(path).absolute()
    if json_out and not yes:
        _write_json(_hibernate_json(actions, {}, dry_run=True))
        return
    if not json_out:
        _print_plan(actions, base)
        if todo and not yes:
            _confirm(f"Proceed with {len(todo)} action(s)?")
    results = _run_actions(todo, level or settings.compression_level)
    statuses = {a: s for a, s, _ in results}
    if json_out:
        _write_json(_hibernate_json(actions, {a: (s, e) for a, s, e in results}, dry_run=False))
    else:
        _print_summary(actions, statuses)
    raise typer.Exit(_hibernate_exit(statuses, interrupted=len(results) < len(todo)))


def _confirm(question: str) -> None:
    if not sys.stdin.isatty():
        err.print("not a terminal: pass --yes to act without asking (nothing was changed)")
        raise typer.Exit(3)
    if not typer.confirm(question):
        err.print("aborted; nothing was changed")
        raise typer.Exit(3)


def _run_actions(todo: Sequence[Action], level: int) -> list[tuple[Action, str, str]]:
    """Run actions one at a time; a Ctrl-C stops after the in-flight target."""
    results: list[tuple[Action, str, str]] = []
    with Index(config.index_path()) as index:
        for root in dict.fromkeys(a.project_root for a in todo):
            _sweep(root, index)
        try:
            for action in todo:
                results.append(_run_one(action, index, level))
        except KeyboardInterrupt:
            err.print("interrupted; the folder being processed was left safe")
        finally:
            for root in dict.fromkeys(a.project_root for a in todo):
                marker.update(root, index)
    return results


def _sweep(root: Path, index: Index) -> None:
    try:
        archiver.sweep_leftovers(root, index)
    except (DormantError, OSError) as exc:
        logger.warning("could not clean leftovers in %s: %s", root, exc)


def _run_one(action: Action, index: Index, level: int) -> tuple[Action, str, str]:
    try:
        with out.status(f"{action.op.value.capitalize()} {action.target}"):
            hibernation.execute(action, index=index, level=level)
    except (DormantError, OSError) as exc:
        err.print(f"[red]failed:[/red] {escape(str(action.target))}: {escape(str(exc))}")
        return action, "failed", str(exc)
    return action, _DONE[action.op], ""


def _hibernate_exit(statuses: dict[Action, str], *, interrupted: bool) -> int:
    if interrupted:
        return 3
    failed = sum(s == "failed" for s in statuses.values())
    if not failed:
        return 0
    return 1 if failed == len(statuses) else 4


def _summary(actions: Sequence[Action], statuses: dict[Action, str]) -> dict[str, int]:
    counts = dict.fromkeys(("archived", "purged", "deleted", "skipped", "failed"), 0)
    counts["skipped"] = sum(a.op == Op.SKIP for a in actions)
    for s in statuses.values():
        counts[s] += 1
    freed = sum(a.bytes for a, s in statuses.items() if s != "failed")
    return {"freed_bytes": freed, **counts}


def _hibernate_json(
    actions: Sequence[Action], results: dict[Action, tuple[str, str]], *, dry_run: bool
) -> dict[str, object]:
    def one(a: Action) -> dict[str, object]:
        default = "skipped" if a.op == Op.SKIP else ("planned" if dry_run else "not_run")
        status, error = results.get(a, (default, ""))
        return {
            "project_root": str(a.project_root),
            "target": str(a.target),
            "kind": a.kind,
            "action": a.op.value,
            "bytes": a.bytes,
            "reason": a.reason or None,
            "reinstall": list(a.reinstall.argv()) if a.op == Op.PURGE and a.reinstall else None,
            "status": status,
            "error": error or None,
        }

    statuses = {a: s for a, (s, _) in results.items()}
    summary = _summary(actions, statuses)
    return {"dry_run": dry_run, "actions": [one(a) for a in actions], "summary": summary}


def _print_plan(actions: Sequence[Action], base: Path) -> None:
    if not actions:
        out.print("No stale projects with reclaimable folders.")
        return
    table = Table("Project", "Target", "Kind", "Action", "Size", "Note")
    for a in actions:
        note = a.reason
        if a.op == Op.PURGE and a.reinstall:
            note = ", ".join(filter(None, [note, f"wake runs: {' '.join(a.reinstall.argv())}"]))
        table.add_row(
            _rel(a.project_root, base),
            a.target.name,
            a.kind,
            a.op.value,
            format_size(a.bytes),
            note,
        )
    out.print(table)
    total = sum(a.bytes for a in actions if a.op != Op.SKIP)
    out.print(f"Would free about {format_size(total)}.")


def _print_summary(actions: Sequence[Action], statuses: dict[Action, str]) -> None:
    if not statuses:
        return
    s = _summary(actions, statuses)
    out.print(
        f"Freed {format_size(s['freed_bytes'])}: {s['archived']} archived, {s['purged']} purged, "
        f"{s['deleted']} deleted, {s['skipped']} skipped, {s['failed']} failed."
    )


# --- wake --------------------------------------------------------------------------------


@app.command()
@_guard
def wake(
    path: Annotated[Path, typer.Argument(help="Folder inside the project.")] = Path(),
    quiet: Annotated[
        bool, typer.Option("--quiet", "-q", help="Print only on work or error.")
    ] = False,
    reinstall: Annotated[
        bool, typer.Option(help="Ignore archives; reinstall from lockfiles.")
    ] = False,
) -> None:
    """Restore everything hibernated in the project containing PATH."""
    # The real entry point routes `dormant wake` straight to wakecmd (no Typer import);
    # this command exists for --help and for callers of the Typer app.
    raise typer.Exit(wakecmd.run(path, quiet=quiet, reinstall=reinstall))


# --- status ------------------------------------------------------------------------------


@app.command()
@_guard
def status(json_out: JsonOpt = False) -> None:
    """Show hibernated and purged projects, archive sizes and total space saved."""
    with Index(config.index_path()) as index:
        records = index.all()
    if json_out:
        _write_json(_status_json(records))
    else:
        _print_status(records)


def _status_json(records: Sequence[ArchiveRecord]) -> dict[str, object]:
    live = [r for r in records if r.state in (ArchiveState.ACTIVE, ArchiveState.PURGED)]
    stale = [r for r in records if r.state == ArchiveState.STALE]
    projects: dict[str, list[ArchiveRecord]] = {}
    for r in live:
        projects.setdefault(str(r.project_root), []).append(r)
    source = sum(r.source_bytes for r in live)
    stored = sum(r.archive_bytes for r in live)
    return {
        "projects": [
            {"root": root, "exists": Path(root).is_dir(), "targets": [_record_json(r) for r in rs]}
            for root, rs in projects.items()
        ],
        "source_bytes": source,
        "archive_bytes": stored,
        "saved_bytes": source - stored,
        "stale_archives": len(stale),
        "stale_bytes": sum(r.archive_bytes for r in stale),
    }


def _record_json(r: ArchiveRecord) -> dict[str, object]:
    return {
        "id": r.id,
        "path": str(r.target),
        "kind": r.kind,
        "state": r.state.value,
        "archive_path": None if r.archive_path is None else str(r.archive_path),
        "source_bytes": r.source_bytes,
        "archive_bytes": r.archive_bytes,
        "created_at": _iso(r.created_at),
        "reinstall": list(r.reinstall) or None,
    }


def _print_status(records: Sequence[ArchiveRecord]) -> None:
    live = [r for r in records if r.state in (ArchiveState.ACTIVE, ArchiveState.PURGED)]
    stale = sum(r.state == ArchiveState.STALE for r in records)
    if not live:
        out.print("Nothing is hibernated.")
    else:
        table = Table("Project", "Target", "State", "Size", "Archive", "Since")
        for r in live:
            table.add_row(
                str(r.project_root) + ("" if r.project_root.is_dir() else " (missing)"),
                r.target.name,
                "hibernated" if r.state == ArchiveState.ACTIVE else "purged",
                format_size(r.source_bytes),
                format_size(r.archive_bytes) if r.archive_path else "-",
                datetime.fromtimestamp(r.created_at).date().isoformat(),
            )
        out.print(table)
    saved = sum(r.source_bytes - r.archive_bytes for r in live)
    projects = len({str(r.project_root) for r in live})
    out.print(f"{projects} project(s), {format_size(saved)} saved.")
    if stale:
        out.print(f"{stale} stale archive(s); run `dormant prune` to delete them.")


# --- prune -------------------------------------------------------------------------------


@app.command("prune")
@_guard
def prune_cmd(
    older: Annotated[
        str | None,
        typer.Option(
            help="Also delete hibernated archives this old whose folder exists again "
            "(reinstalled by hand)."
        ),
    ] = None,
    yes: YesOpt = False,
    json_out: JsonOpt = False,
) -> None:
    """Delete archives nobody needs: missing projects, stale archives.

    Never deletes the only copy of hibernated data while its project folder exists:
    an archive whose folder is still missing is kept, however old.
    """
    max_age = _parse_older(older)
    with Index(config.index_path()) as index:
        found = prune.candidates(index, max_age)
        if json_out and not yes:
            _write_json(_prune_json(found, {}, dry_run=True))
            return
        if not json_out:
            _print_prune_plan(found)
            if found and not yes:
                _confirm(f"Delete {len(found)} archive(s)?")
        results = _run_prune(found, index)
    if json_out:
        _write_json(_prune_json(found, results, dry_run=False))
    elif results:
        freed = sum(n for n, e in results.values() if not e)
        out.print(f"Deleted {sum(not e for _, e in results.values())}, freed {format_size(freed)}.")
    failed = sum(bool(e) for _, e in results.values())
    raise typer.Exit(0 if not failed else (1 if failed == len(results) else 4))


def _run_prune(found: Sequence[prune.Candidate], index: Index) -> dict[str, tuple[int, str]]:
    results: dict[str, tuple[int, str]] = {}
    for c in found:
        try:
            results[c.record.id] = (prune.remove(c.record, index=index), "")
        except (DormantError, OSError) as exc:
            err.print(f"[red]failed:[/red] {escape(str(c.record.archive_path))}: {exc}")
            results[c.record.id] = (0, str(exc))
        if c.record.project_root.is_dir():
            marker.update(c.record.project_root, index)
    return results


def _prune_json(
    found: Sequence[prune.Candidate], results: dict[str, tuple[int, str]], *, dry_run: bool
) -> dict[str, object]:
    items = []
    for c in found:
        _, error = results.get(c.record.id, (0, ""))
        status_ = "planned" if dry_run else ("failed" if error else "deleted")
        items.append(
            {
                **_record_json(c.record),
                "project_root": str(c.record.project_root),
                "reason": c.reason,
                "status": status_,
                "error": error or None,
            }
        )
    freed_total = sum(n for n, e in results.values() if not e)
    return {"dry_run": dry_run, "items": items, "freed_bytes": freed_total}


def _print_prune_plan(found: Sequence[prune.Candidate]) -> None:
    if not found:
        out.print("Nothing to prune.")
        return
    table = Table("Project", "Target", "Archive", "Reason")
    for c in found:
        r = c.record
        size = format_size(r.archive_bytes) if r.archive_path else "-"
        table.add_row(str(r.project_root), r.target.name, size, c.reason)
    out.print(table)


# --- tui ---------------------------------------------------------------------------------


@app.command()
@_guard
def tui(
    path: Annotated[
        Path | None, typer.Argument(exists=True, file_okay=False, help="Folder (default: home).")
    ] = None,
    older: OlderOpt = None,
) -> None:
    """Interactive picker: select folders and hibernate them (needs dormant[tui])."""
    max_age = _parse_older(older)
    try:
        from dormant import tui as tui_app  # lazy: textual is an optional extra
    except ImportError:
        err.print(r"Install the TUI with: pip install 'dormant\[tui]'")
        raise typer.Exit(1) from None
    tui_app.run(path or Path.home(), max_age, config.load_settings().compression_level)


# --- hook --------------------------------------------------------------------------------


@app.command()
@_guard
def hook(shell: Annotated[str, typer.Argument(help=f"One of: {', '.join(hooks.SHELLS)}.")]) -> None:
    """Print the shell hook snippet. Install: eval "$(dormant hook zsh)"."""
    try:
        text = hooks.snippet(shell)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="SHELL") from exc
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))  # LF only, even on Windows
    sys.stdout.buffer.flush()
