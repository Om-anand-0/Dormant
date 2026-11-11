"""Textual picker over survey results: select targets, confirm, hibernate them."""

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Label

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
_CSS = """
#confirm { width: 60; height: auto; padding: 1 2; border: thick $error;
  background: $surface; }
Horizontal { height: auto; }
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
    for a in hibernation.plan(rows, purge_deps=False):
        row, _ = info[a.target]
        note = a.reason if a.op == Op.SKIP or a.reason else ""
        eco = ", ".join(d.name for d in row.project.detectors)
        items.append(Item(a, eco, note, row.last_activity))
    return items


def _age(ts: float) -> str:
    return f"{int((time.time() - ts) // 86400)}d ago" if ts else "unknown"


class Confirm(ModalScreen[bool]):
    """Yes/No modal; dismisses with True only on Yes."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, question: str) -> None:
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
        yield DataTable(cursor_type="row")
        yield Label("", id="summary")
        yield Footer()

    def on_mount(self) -> None:
        """Set up the table and start the survey."""
        self.query_one(DataTable).add_columns(*_COLUMNS)
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
        self.sub_title = f"{len(self.items)} targets"

    def _render_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        if self.sort_by == "size":
            keys = sorted(self.items, key=lambda k: self.items[k].action.bytes, reverse=True)
        else:
            keys = sorted(self.items, key=lambda k: self.items[k].last_activity)
        for k in keys:
            i = self.items[k]
            a = i.action
            cells: tuple[str, ...] = (
                "*" if k in self.selected else " ",
                a.project_root.name,
                i.ecosystem,
            )
            cells += (a.target.name, a.kind, format_size(a.bytes), _age(i.last_activity), i.note)
            if a.op == Op.SKIP:
                cells = tuple(f"[dim]{c}[/dim]" for c in cells)
            table.add_row(*cells, key=k)
        self._update_summary()

    def _update_summary(self) -> None:
        total = sum(self.items[k].action.bytes for k in self.selected)
        text = f"{len(self.selected)} selected, {format_size(total)} reclaimable"
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
        table.update_cell(k, table.ordered_columns[0].key, "*" if k in self.selected else " ")
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
        question = f"Archive {len(self.selected)} folders, free {format_size(total)}?"
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
        table.update_cell(key, table.ordered_columns[-1].key, status)
        self.selected.discard(key)
        table.update_cell(key, table.ordered_columns[0].key, " ")
        self._update_summary()

    def _finished(self) -> None:
        self.busy = False


def run(path: Path, max_age: timedelta | None, level: int) -> None:
    """Run the picker until the user quits."""
    DormantTui(path, max_age, level).run()
