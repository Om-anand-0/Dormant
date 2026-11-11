"""Shell hook snippets printed by ``dormant hook <shell>``."""

from importlib.resources import files

SHELLS = ("zsh", "bash", "fish", "powershell")
_FILES = {
    "zsh": "dormant.zsh",
    "bash": "dormant.bash",
    "fish": "dormant.fish",
    "powershell": "dormant.ps1",
}


def snippet(shell: str) -> str:
    """Return the hook snippet for a shell.

    Args:
        shell: One of ``SHELLS``.

    Returns:
        The snippet source, ready to be evaluated by that shell.

    Raises:
        ValueError: If ``shell`` is not supported.
    """
    if shell not in _FILES:
        raise ValueError(f"unknown shell {shell!r}; choose one of: {', '.join(SHELLS)}")
    return files("dormant").joinpath("hooks", _FILES[shell]).read_text(encoding="utf-8")
