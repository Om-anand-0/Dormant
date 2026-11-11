"""End to end: hibernate with the real CLI, then ``cd`` in a real shell and watch it come back."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fixtures.cli import age
from fixtures.trees import Entry, Fixture, snapshot

pytestmark = pytest.mark.slow

SCRIPTS = Path(sys.executable).parent


def _git_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    git = shutil.which("git")  # plain `bash` may be WSL's shim, which cannot see this disk
    for parent in Path(git).resolve().parents if git else []:
        if (parent / "bin" / "bash.exe").exists():
            return str(parent / "bin" / "bash.exe")
    return None


def _env(project: Path) -> dict[str, str]:
    env = dict(os.environ)  # includes DORMANT_DATA_DIR from the autouse fixture
    env["PATH"] = str(SCRIPTS) + os.pathsep + env["PATH"]
    env["PROJ"] = project.as_posix()
    env.pop("DORMANT_DISABLE", None)
    return env


def _run(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def _hibernate(fx: Fixture, env: dict[str, str], cwd: Path) -> dict[str, Entry]:
    if shutil.which("dormant", path=str(SCRIPTS)) is None:
        pytest.skip("dormant console script not installed next to this interpreter")
    age(fx.project_root)
    snap = snapshot(fx.target)
    out = _run(["dormant", "hibernate", str(fx.project_root), "--yes"], env, cwd)
    assert out.returncode == 0, out.stdout + out.stderr
    assert not fx.target.exists()
    return snap


def test_bash_hook_restores_on_cd(node_project: Fixture, tmp_path: Path) -> None:
    bash = _git_bash()
    if bash is None:
        pytest.skip("bash not found (on Windows, Git Bash is required)")
    outside = tmp_path / "outside"
    outside.mkdir()
    env = _env(node_project.project_root)
    before = _hibernate(node_project, env, outside)
    script = 'eval "$(dormant hook bash)"; cd "$PROJ" || exit 9; eval "$PROMPT_COMMAND"'
    out = _run([bash, "--norc", "--noprofile", "-c", script], env, outside)
    assert out.returncode == 0, out.stdout + out.stderr
    assert snapshot(node_project.target) == before
    assert not (node_project.project_root / ".dormant" / "state").exists()


def test_powershell_hook_restores_on_cd(node_project: Fixture, tmp_path: Path) -> None:
    ps = shutil.which("powershell.exe")
    if ps is None:
        pytest.skip("powershell.exe not available")
    outside = tmp_path / "outside"
    outside.mkdir()
    env = _env(node_project.project_root)
    before = _hibernate(node_project, env, outside)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Invoke-Expression (& dormant hook powershell | Out-String); "
        "Set-Location -LiteralPath $env:PROJ; "
        "$null = prompt"
    )
    out = _run([ps, "-NoProfile", "-NonInteractive", "-Command", script], env, outside)
    assert out.returncode == 0, out.stdout + out.stderr
    assert snapshot(node_project.target) == before
    assert not (node_project.project_root / ".dormant" / "state").exists()
