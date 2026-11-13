"""Textual picker over survey results: select targets, confirm, hibernate them."""

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label

from dormant import archiver, config, hibernation, marker
from dormant.errors import DormantError
from dormant.hibernation import Action, Op
from dormant.index import Index
from dormant.survey import survey
from dormant.units import format_size

logger = logging.getLogger(__name__)
KINDS = {"dependencies", "build", "cache"}
_COLUMNS = ("", "Project", "Ecosystem", "Target", "Kind", "Size", "Last activity", "Note")
_DONE = {Op.ARCHIVE: "archived", Op.PURGE: "purged", Op.DELETE: "deleted"}
_KIND_STYLE = {"dependencies": "cyan", "build": "magenta", "cache": "yellow"}
_ON, _OFF = Text("●", "bold green"), Text("○", "dim")
_CSS = """
DataTable { height: 1fr; border: round $primary; }
#summary { dock: bottom; height: 1; padding: 0 1; background: $boost; }
#confirm { width: 60; height: auto; padding: 1 2; border: thick $error;
  background: $surface; }
#confirm Label { width: 100%; margin-bottom: 1; }
#confirm Horizontal { height: auto; align-horizontal: center; }
#confirm Button { margin: 0 1; }
"""


@dataclass(frozen=True, slots=True)
class Item:
    """One table row: the planned action plus display data."""

    action: Action
    ecosystem: str
    note: str
    last_activity: float


def _execute(action: Action, level: int) -> None:
    """Run one action as the CLI does: sweep leftovers, execute, refresh the marker."""
    with Index(config.index_path()) as index:
        try:
            archiver.sweep_leftovers(action.project_root, index)
        except (DormantError, OSError) as exc:
            logger.warning("could not clean leftovers in %s: %s", action.project_root, exc)
        try:
            hibernation.execute(action, index=index, level=level)
        finally:
            marker.update(action.project_root, index)


def _load(path: Path, max_age: timedelta | None) -> list[Item]:
    """Survey and plan exactly like ``dormant hibernate`` (no --purge)."""
    settings = config.load_settings()
    rows = survey(path, KINDS, max_age or settings.older_than, settings.ignore)
    info = {t.path: (r, t) for r in rows for t in r.project.targets}
    items = []
    min_bytes = settings.min_archive_size
    for a in hibernation.plan(rows, purge_deps=False, min_archive_bytes=min_bytes):
        row, _ = info[a.target]
        note = a.reason if a.op == Op.SKIP or a.reason else ""
        eco = ", ".join(d.name for d in row.project.detectors)
        items.append(Item(a, eco, note, row.last_activity))
    return items


def _age(ts: float) -> str:
    return f"{int((time.time() - ts) // 86400)}d ago" if ts else "unknown"


def _size(n: int) -> Text:
    """Right-aligned size, hotter colour for bigger folders."""
    style = "bold red" if n >= 1 << 30 else "yellow" if n >= 100 << 20 else "green"
    return Text(format_size(n), style, justify="right")


def _cells(item: Item, selected: bool) -> tuple[Text, ...]:
    a = item.action
    cells: tuple[Text, ...] = (
        _ON if selected else _OFF,
        Text(a.project_root.name, "bold"),
        Text(item.ecosystem, "blue"),
        Text(a.target.name),
        Text(a.kind, _KIND_STYLE.get(a.kind, "")),
        _size(a.bytes),
        Text(_age(item.last_activity), "italic"),
        Text(item.note, "dim"),
    )
    if a.op == Op.SKIP:
        cells = tuple(Text(c.plain, "dim strike", justify=c.justify) for c in cells)
    return cells


class Confirm(ModalScreen[bool]):
    """Yes/No modal; dismisses with True only on Yes."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: Text) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.question)
            with Horizontal():
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no", variant="primary")

    def action_answer(self, value: bool) -> None:
        """Close the modal with the answer."""
        self.dismiss(value)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class DormantTui(App[None]):
    """npkill-style picker: space selects, enter/h hibernates, q quits."""

    CSS = _CSS
    TITLE = "Dormant"
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "toggle_row", "Select"),
        Binding("a", "select_all", "All"),
        Binding("s", "sort", "Sort size/age"),
        Binding("h", "hibernate", "Hibernate"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, path: Path, max_age: timedelta | None = None, level: int = 3) -> None:
        super().__init__()
        self.path = path
        self.max_age = max_age
        self.level = level
        self.items: dict[str, Item] = {}
        self.selected: set[str] = set()
        self.sort_by = "size"
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row", zebra_stripes=True)
        yield Label("", id="summary")
        yield Footer()

    def on_mount(self) -> None:
        """Set up the table and start the survey."""
        table = self.query_one(DataTable)
        table.add_columns(*_COLUMNS)
        table.loading = True
        self.sub_title = f"scanning {self.path} ..."
        self._scan()

    @work(thread=True, exclusive=True)
    def _scan(self) -> None:
        # ponytail: rows arrive one at a time after the survey returns, not during it;
        # stream from the scanner generator if scans get slow enough to matter.
        for item in _load(self.path, self.max_age):
            self.call_from_thread(self._add, item)
        self.call_from_thread(self._scanned)

    def _add(self, item: Item) -> None:
        self.items[str(item.action.target)] = item
        self._render_table()

    def _scanned(self) -> None:
        self.query_one(DataTable).loading = False
        self.sub_title = f"{self.path}  ·  {len(self.items)} targets"

    def _render_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        if self.sort_by == "size":
            keys = sorted(self.items, key=lambda k: self.items[k].action.bytes, reverse=True)
        else:
            keys = sorted(self.items, key=lambda k: self.items[k].last_activity)
        for k in keys:
            table.add_row(*_cells(self.items[k], k in self.selected), key=k)
        self._update_summary()

    def _update_summary(self) -> None:
        total = sum(self.items[k].action.bytes for k in self.selected)
        text = Text.assemble(
            (f" {len(self.selected)} ", "bold reverse green"),
            " selected  ·  ",
            (format_size(total), "bold yellow"),
            " reclaimable  ·  sorted by ",
            (self.sort_by, "bold cyan"),
        )
        self.query_one("#summary", Label).update(text)

    def _current(self) -> str | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def action_toggle_row(self) -> None:
        """Toggle the row under the cursor; ineligible rows are ignored."""
        k = self._current()
        if k is None or self.busy or self.items[k].action.op == Op.SKIP:
            return
        self.selected ^= {k}
        table = self.query_one(DataTable)
        table.update_cell(k, table.ordered_columns[0].key, _ON if k in self.selected else _OFF)
        self._update_summary()

    def action_select_all(self) -> None:
        """Select every eligible row."""
        if self.busy:
            return
        self.selected = {k for k, v in self.items.items() if v.action.op != Op.SKIP}
        self._render_table()

    def action_sort(self) -> None:
        """Switch between size (largest first) and age (oldest first) ordering."""
        self.sort_by = "age" if self.sort_by == "size" else "size"
        self._render_table()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_hibernate()  # Enter

    def action_hibernate(self) -> None:
        """Ask for confirmation; nothing happens unless the answer is Yes."""
        if self.busy or not self.selected:
            return
        total = sum(self.items[k].action.bytes for k in self.selected)
        question = Text.assemble(
            "Archive ",
            (str(len(self.selected)), "bold"),
            " folders and free ",
            (format_size(total), "bold yellow"),
            "?",
        )
        self.push_screen(Confirm(question), self._confirmed)

    def _confirmed(self, yes: bool | None) -> None:
        if yes is True:
            self.busy = True
            self._run([self.items[k].action for k in sorted(self.selected)])

    @work(thread=True, exclusive=True)
    def _run(self, actions: list[Action]) -> None:
        for a in actions:
            try:
                _execute(a, self.level)
                status = _DONE[a.op]
            except (DormantError, OSError) as exc:
                status = f"failed: {exc}"
            self.call_from_thread(self._set_status, str(a.target), status)
        self.call_from_thread(self._finished)

    def _set_status(self, key: str, status: str) -> None:
        table = self.query_one(DataTable)
        style = "bold red" if status.startswith("failed") else "bold green"
        table.update_cell(key, table.ordered_columns[-1].key, Text(status, style))
        self.selected.discard(key)
        table.update_cell(key, table.ordered_columns[0].key, Text("✓", "green"))
        self._update_summary()

    def _finished(self) -> None:
        self.busy = False


def run(path: Path, max_age: timedelta | None, level: int) -> None:
    """Run the picker until the user quits."""
    DormantTui(path, max_age, level).run()
