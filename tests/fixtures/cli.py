"""Helpers for driving the CLI in tests: ageing projects, running commands, reading the index."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from typer.testing import CliRunner, Result

from dormant import config
from dormant.cli import app
from dormant.index import ArchiveRecord, Index

OLD_DATE = "2020-01-01T00:00:00+0000"
GIT_ENV = {"GIT_AUTHOR_DATE": OLD_DATE, "GIT_COMMITTER_DATE": OLD_DATE}


def age(root: Path, days: int = 200) -> None:
    """Set every non-symlink file and folder under ``root`` to ``days`` days old."""
    then = time.time() - days * 86400
    for dirpath, dirnames, filenames in os.walk(root):
        for name in (*filenames, *dirnames):
            p = os.path.join(dirpath, name)  # noqa: PTH118
            if not os.path.islink(p):  # noqa: PTH114
                os.utime(p, (then, then))
    os.utime(root, (then, then))


def run(*args: str | Path) -> Result:
    """Invoke the Typer app in-process (non-TTY stdin)."""
    return CliRunner().invoke(app, [str(a) for a in args])


def run_json(*args: str | Path, code: int = 0) -> object:
    """Run a command with ``--json``, assert its exit code, return the parsed stdout."""
    result = run(*args, "--json")
    assert result.exit_code == code, result.output
    return json.loads(result.stdout)


def records() -> list[ArchiveRecord]:
    """Every record in the test's index."""
    with Index(config.index_path()) as index:
        return index.all()


def dormant_module(
    *args: str | Path, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m dormant`` in a subprocess (cwd must be outside the project)."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "dormant", *map(str, args)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def git(cwd: Path, *args: str) -> str:
    """Run git with a fixed identity and a 2020 commit date."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],  # noqa: S607
        cwd=cwd,
        env={**os.environ, **GIT_ENV},
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=True,
    )
    return out.stdout
