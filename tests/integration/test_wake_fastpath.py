"""``dormant wake`` must not import Typer, Rich or Textual unless there is work to do."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fixtures.cli import age, dormant_module
from fixtures.trees import Fixture, snapshot

LINE = re.compile(r"import time:\s+(\d+) \|\s+(\d+) \| ( *)(\S+)")


def _imports(stderr: str) -> tuple[set[str], int]:
    """Return (all imported module names, total self time in microseconds)."""
    found = [(m[4], int(m[1])) for m in map(LINE.match, stderr.splitlines()) if m]
    return {n for n, _ in found}, sum(t for _, t in found)


def _wake_importtime(
    cwd: Path, *args: str
) -> tuple[set[str], int, subprocess.CompletedProcess[str]]:
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-X", "importtime", "-m", "dormant", "wake", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    names, total = _imports(out.stderr)
    return names, total, out


def _top(names: set[str], banned: tuple[str, ...]) -> set[str]:
    return {n for n in names if n.split(".")[0] in banned}


def test_no_marker_imports_nothing_heavy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    names, total, out = _wake_importtime(tmp_path, str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert "dormant.wakecmd" in names
    assert _top(names, ("typer", "click", "rich", "textual", "zstandard")) == set()
    assert "dormant.cli" not in names
    assert out.stdout == ""
    with capsys.disabled():
        print(f"\nwake fast path (no marker): {total / 1000:.0f} ms total import self-time")


def test_marker_without_records_never_loads_typer_or_textual(tmp_path: Path) -> None:
    (tmp_path / ".dormant").mkdir()
    state = tmp_path / ".dormant" / "state"
    state.write_text("{}\n")
    names, _, out = _wake_importtime(tmp_path, "--quiet", str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert _top(names, ("typer", "click", "textual", "rich")) == set()
    assert not state.exists()  # stale marker cleaned up
    # Without --quiet it says so, and only then pulls in Rich.
    state.parent.mkdir(exist_ok=True)
    state.write_text("{}\n")
    names, _, out = _wake_importtime(tmp_path, str(tmp_path))
    assert out.returncode == 0, out.stderr
    assert _top(names, ("typer", "click", "textual")) == set()
    assert "Nothing to wake" in out.stdout


# --- the real entry points ------------------------------------------------------------------


def _scripts_dormant() -> str | None:
    return shutil.which("dormant", path=str(Path(sys.executable).parent))


def test_console_script_version(tmp_path: Path) -> None:
    exe = _scripts_dormant()
    if exe is None:
        pytest.skip("console script not installed next to this interpreter")
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [exe, "--version"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("dormant ")


@pytest.mark.slow
def test_module_entry_point_round_trip(node_project: Fixture, tmp_path: Path) -> None:
    root, target = node_project.project_root, node_project.target
    age(root)
    before = snapshot(target)
    cwd = tmp_path  # outside the project, or it would count as in use
    scan = dormant_module("scan", root, "--json", cwd=cwd)
    assert scan.returncode == 0, scan.stderr
    assert str(target).replace("\\", "\\\\") in scan.stdout
    hib = dormant_module("hibernate", root, "--yes", cwd=cwd)
    assert hib.returncode == 0, hib.stdout + hib.stderr
    assert not target.exists()
    assert (root / ".dormant" / "state").is_file()
    # wake routed through __main__ without Typer, then status via the Typer app.
    status = dormant_module("status", "--json", cwd=cwd)
    assert status.returncode == 0, status.stderr
    assert '"state": "active"' in status.stdout
    woke = dormant_module("wake", root, cwd=cwd)
    assert woke.returncode == 0, woke.stdout + woke.stderr
    assert snapshot(target) == before
    assert not (root / ".dormant" / "state").exists()


def test_wake_usage_error_exits_2(tmp_path: Path) -> None:
    out = dormant_module("wake", "--bogus", cwd=tmp_path)
    assert out.returncode == 2
