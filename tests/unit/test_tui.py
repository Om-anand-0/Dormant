import asyncio
import builtins
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

pytest.importorskip("textual")

from textual.widgets import DataTable

from dormant import tui
from dormant.cli import app
from dormant.hibernation import Action, Op


def _item(name: str, size: int, op: Op = Op.ARCHIVE, note: str = "") -> tui.Item:
    root = Path("proj") / name
    return tui.Item(
        Action(root, root / "node_modules", "dependencies", op, size, note), "node", note, 1.0
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Action]:
    done: list[Action] = []
    monkeypatch.setattr(
        tui,
        "_load",
        lambda *_: [_item("a", 300), _item("b", 200), _item("c", 100, Op.SKIP, "tracked")],
    )
    monkeypatch.setattr(tui, "_execute", lambda action, level: done.append(action))
    return done


def _drive(script: Callable[[tui.DormantTui, object], Awaitable[None]]) -> tui.DormantTui:
    app_ = tui.DormantTui(Path())

    async def main() -> None:
        async with app_.run_test() as pilot:
            await app_.workers.wait_for_complete()
            await pilot.pause()
            await script(app_, pilot)

    asyncio.run(main())
    return app_


def test_rows_and_ineligible(calls: list[Action]) -> None:
    async def script(a: tui.DormantTui, pilot: object) -> None:
        assert a.query_one(DataTable).row_count == 3
        await pilot.press("down", "down", "space")  # type: ignore[attr-defined]
        assert not a.selected  # row c is a skip: can't be selected
        await pilot.press("a")  # type: ignore[attr-defined]
        assert len(a.selected) == 2

    _drive(script)


def test_no_does_nothing_yes_acts(calls: list[Action]) -> None:
    async def script(a: tui.DormantTui, pilot: object) -> None:
        p = pilot  # type: ignore[assignment]
        await p.press("space", "enter")
        await p.pause()
        assert isinstance(a.screen, tui.Confirm)
        await p.press("n")
        await p.pause()
        assert calls == []
        await p.press("enter")
        await p.pause()
        await p.press("y")
        await a.workers.wait_for_complete()
        await p.pause()

    _drive(script)
    assert [c.target.parent.name for c in calls] == ["a"]


def test_enter_without_selection_has_no_modal(calls: list[Action]) -> None:
    async def script(a: tui.DormantTui, pilot: object) -> None:
        await pilot.press("enter")  # type: ignore[attr-defined]
        assert not isinstance(a.screen, tui.Confirm)

    _drive(script)


def test_missing_textual(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delitem(sys.modules, "dormant.tui", raising=False)
    real = builtins.__import__

    def fake(name: str, *args: object, **kw: object) -> ModuleType:
        if name == "dormant" and args and args[2] and "tui" in args[2]:  # type: ignore[index]
            raise ImportError("no textual")
        return real(name, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", fake)
    result = CliRunner().invoke(app, ["tui", str(tmp_path)])
    assert result.exit_code == 1
    assert "pip install 'dormant[tui]'" in result.output
