"""Run the real hook snippets in real shells against a fake ``dormant`` on PATH.

Each script visits: root/a/b (wake), same dir again, root/a (same root: no retry),
an unmarked dir, root again (new visit: wake), then with DORMANT_DISABLE=1 leaves and
returns (no wake). So every shell must log exactly two wakes, both for ``root``.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dormant.hooks import snippet

FAKE_SH = '#!/bin/sh\nprintf \'%s\n\' "$*" >> "$DORMANT_FAKE_LOG"\n'


def _git_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    # Plain `bash` on Windows may be WSL's System32 shim, which can't see this filesystem.
    git = shutil.which("git")
    # git.exe lives in <Git>/cmd, <Git>/bin or <Git>/mingw64/bin; bash in <Git>/bin.
    for parent in Path(git).resolve().parents if git else []:
        if (parent / "bin" / "bash.exe").exists():
            return str(parent / "bin" / "bash.exe")
    return None


def _setup(tmp_path: Path, shell: str, hook_name: str) -> dict[str, str]:
    root = tmp_path / "proj"
    (root / ".dormant").mkdir(parents=True)
    (root / ".dormant" / "state").write_text("hibernated\n")
    (root / "a" / "b").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    fake = tmp_path / "fakebin"
    fake.mkdir()
    if os.name == "nt" and shell != "bash":
        (fake / "dormant.cmd").write_text('@echo %*>>"%DORMANT_FAKE_LOG%"\r\n')
    else:
        (fake / "dormant").write_text(FAKE_SH, newline="\n")
        (fake / "dormant").chmod(0o755 | stat.S_IEXEC)
    hook = tmp_path / hook_name
    hook.write_text(snippet(shell), encoding="utf-8", newline="\n")
    env = {k: v for k, v in os.environ.items() if k != "DORMANT_DISABLE"}
    env |= {
        "DORMANT_FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_DIR": fake.as_posix(),
        "HOOK": hook.as_posix(),
        "ROOT_DIR": root.as_posix(),
        "OTHER": (tmp_path / "elsewhere").as_posix(),
    }
    return env


def _log(env: dict[str, str]) -> list[str]:
    return Path(env["DORMANT_FAKE_LOG"]).read_text(errors="replace").splitlines()


BASH_SCRIPT = r"""
set -u
PATH="$(cd "$FAKE_DIR" && pwd):$PATH"
{prev}
source "$HOOK"
source "$HOOK"
p() {{ for c in "${{PROMPT_COMMAND[@]}}"; do eval "$c"; done; }}
cd "$ROOT_DIR/a/b"; p; p
cd ..; p
cd "$OTHER"; p
cd "$ROOT_DIR"; p
DORMANT_DISABLE=1; cd "$OTHER"; p; cd "$ROOT_DIR"; p
printf 'ROOT=%s\n' "$PWD"
"""


@pytest.mark.parametrize(
    "prev",
    [
        "PROMPT_COMMAND='echo prev >> \"$DORMANT_FAKE_LOG\"'",
        "PROMPT_COMMAND=('echo prev >> \"$DORMANT_FAKE_LOG\"')",
        "unset PROMPT_COMMAND",
    ],
    ids=["string", "array", "unset"],
)
def test_bash(tmp_path: Path, prev: str) -> None:
    bash = _git_bash()
    if bash is None:
        pytest.skip("bash not found (on Windows, Git Bash is required)")
    env = _setup(tmp_path, "bash", "hook.bash")
    script = tmp_path / "run.sh"
    script.write_text(BASH_SCRIPT.format(prev=prev), newline="\n")
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [bash, "--noprofile", "--norc", script.as_posix()],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stderr == ""
    root = out.stdout.strip().removeprefix("ROOT=")
    calls = _log(env)
    assert [c for c in calls if c != "prev"] == [f"wake --quiet {root}"] * 2
    # Previous PROMPT_COMMAND still runs on every prompt (7 prompts in the script).
    assert calls.count("prev") == (0 if prev.startswith("unset") else 7)


PS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$env:PATH = $env:FAKE_DIR + [System.IO.Path]::PathSeparator + $env:PATH
function global:prompt { Add-Content -LiteralPath $env:DORMANT_FAKE_LOG 'prev'; 'PS-PREV> ' }
$hook = Get-Content -Raw -LiteralPath $env:HOOK
Invoke-Expression $hook
Invoke-Expression $hook
Set-Location -LiteralPath (Join-Path $env:ROOT_DIR 'a/b'); $null = prompt; $null = prompt
Set-Location ..; $null = prompt
Set-Location -LiteralPath $env:OTHER; $null = prompt
Set-Location -LiteralPath $env:ROOT_DIR; $p = prompt
$env:DORMANT_DISABLE = '1'
Set-Location -LiteralPath $env:OTHER; $null = prompt
Set-Location -LiteralPath $env:ROOT_DIR; $null = prompt
"PROMPT=$p"
"ROOT=$($PWD.ProviderPath)"
"""


@pytest.mark.slow
@pytest.mark.parametrize("exe", ["powershell", "pwsh"])
def test_powershell(tmp_path: Path, exe: str) -> None:
    path = shutil.which(exe)
    if path is None:
        pytest.skip(f"{exe} not installed")
    env = _setup(tmp_path, "powershell", "hook.ps1")
    script = tmp_path / "run.ps1"
    script.write_text(PS_SCRIPT, encoding="utf-8-sig")
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [path, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    lines = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    assert lines["PROMPT"] == "PS-PREV> "
    calls = _log(env)
    wakes = [c.replace('"', "").strip() for c in calls if c != "prev"]
    assert wakes == [f"wake --quiet {lines['ROOT']}"] * 2
    assert calls.count("prev") == 7


NO_RC = {"zsh": ["-f"], "fish": ["--no-config"]}


def _posix_shell_test(tmp_path: Path, shell: str, script: str) -> None:
    path = shutil.which(shell)
    if path is None or sys.platform == "win32":
        pytest.skip(f"{shell} not installed")
    env = _setup(tmp_path, shell, f"hook.{shell}")
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [path, *NO_RC[shell], "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    root = out.stdout.strip().removeprefix("ROOT=")
    assert _log(env) == [f"wake --quiet {root}"] * 2


def test_zsh(tmp_path: Path) -> None:
    _posix_shell_test(
        tmp_path,
        "zsh",
        'PATH="$FAKE_DIR:$PATH"; source "$HOOK"; source "$HOOK"\n'
        'cd "$ROOT_DIR/a/b"; cd ..; cd "$OTHER"; cd "$ROOT_DIR"\n'
        'DORMANT_DISABLE=1; cd "$OTHER"; cd "$ROOT_DIR"\n'
        'print -r "ROOT=$PWD"',
    )


def test_fish(tmp_path: Path) -> None:
    _posix_shell_test(
        tmp_path,
        "fish",
        "set -gx PATH $FAKE_DIR $PATH; source $HOOK; source $HOOK\n"
        "cd $ROOT_DIR/a/b; cd ..; cd $OTHER; cd $ROOT_DIR\n"
        "set -gx DORMANT_DISABLE 1; cd $OTHER; cd $ROOT_DIR\n"
        'echo "ROOT=$PWD"',
    )
