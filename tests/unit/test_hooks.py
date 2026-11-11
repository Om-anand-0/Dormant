import re
import shutil
import subprocess
from importlib.resources import as_file, files

import pytest

from dormant.hooks import SHELLS, snippet

HOOKS = files("dormant").joinpath("hooks")


@pytest.mark.parametrize("shell", SHELLS)
def test_snippet_mentions_wake(shell: str) -> None:
    text = snippet(shell)
    assert re.search(r"wake\W+--quiet", text)
    assert "DORMANT_DISABLE" in text
    assert "\r" not in text


def test_unknown_shell_lists_valid_ones() -> None:
    with pytest.raises(ValueError, match="zsh, bash, fish, powershell"):
        snippet("tcsh")


@pytest.mark.parametrize("shell", SHELLS)
def test_no_subprocess_on_the_cd_path(shell: str) -> None:
    # Grep-level guard: the per-cd check must use built-ins only. Comments are ignored.
    code = "\n".join(line.split("#", 1)[0] for line in snippet(shell).splitlines())
    for bad in ("$(", "`", "dirname", "command -v", "which ", "realpath"):
        assert bad not in code, bad
    # Exactly one external call: the wake itself.
    assert len(re.findall(r"\bwake\b", code)) == 1


@pytest.mark.parametrize("name", ["dormant.bash", "dormant.zsh"])
def test_shellcheck(name: str) -> None:
    exe = shutil.which("shellcheck")
    if exe is None:
        pytest.skip("shellcheck not installed (try: uvx --from shellcheck-py shellcheck)")
    with as_file(HOOKS.joinpath(name)) as path:
        result = subprocess.run(  # noqa: S603 - fixed argv
            [exe, str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    assert result.returncode == 0, result.stdout
