"""Load and validate the ecosystem registry in ``detectors.toml``."""

import functools
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, cast

from dormant import platform as _platform
from dormant.errors import DetectorError

Kind = Literal["dependencies", "build", "cache"]
KINDS: frozenset[str] = frozenset({"dependencies", "build", "cache"})

_DETECTOR_KEYS = {"name", "markers", "target", "reinstall"}
_TARGET_KEYS = {"path", "kind"}
_OS_KEYS = {"posix", "windows"}


@dataclass(frozen=True, slots=True)
class Target:
    """A folder a detector may reclaim, relative to the project root."""

    path: str
    kind: Kind


@dataclass(frozen=True, slots=True)
class Reinstall:
    """A lockfile and the argument lists that rebuild a target from it, per OS."""

    lockfile: str
    posix: tuple[str, ...]
    windows: tuple[str, ...]

    def argv(self) -> tuple[str, ...]:
        """Return the command for the running OS (never run through a shell)."""
        return self.windows if _platform.IS_WINDOWS else self.posix


@dataclass(frozen=True, slots=True)
class Detector:
    """One ecosystem: how to recognise a project and which folders it owns.

    ``reinstall`` is in file order; the first lockfile that exists wins.
    """

    name: str
    markers: tuple[str, ...]
    targets: tuple[Target, ...]
    reinstall: tuple[Reinstall, ...]


def load_detectors(path: Path | None = None) -> tuple[Detector, ...]:
    """Load detectors from ``path``, or the bundled ``detectors.toml`` when None.

    Args:
        path: A detectors file to load instead of the bundled one (used by tests).

    Returns:
        The detectors in file order.

    Raises:
        DetectorError: The file is not valid TOML or breaks the schema.
    """
    if path is None:
        return _bundled()
    return _parse(path.read_bytes(), str(path))


@functools.cache
def _bundled() -> tuple[Detector, ...]:
    data = resources.files("dormant").joinpath("detectors.toml").read_bytes()
    return _parse(data, "detectors.toml")


def _parse(data: bytes, source: str) -> tuple[Detector, ...]:
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise DetectorError(f"{source}: invalid TOML: {exc}") from exc
    if set(doc) - {"detector"}:
        raise DetectorError(f"{source}: unknown top-level keys {sorted(set(doc) - {'detector'})}")
    entries = doc.get("detector")
    if not isinstance(entries, list) or not entries:
        raise DetectorError(f"{source}: needs at least one [[detector]] table")
    detectors = tuple(_detector(entry, i) for i, entry in enumerate(entries))
    names = [d.name for d in detectors]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise DetectorError(f"{source}: duplicate detector names {dupes}")
    return detectors


def _detector(entry: object, index: int) -> Detector:
    if not isinstance(entry, dict):
        raise DetectorError(f"detector #{index}: must be a table")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise DetectorError(f"detector #{index}: field 'name' must be a non-empty string")
    where = f"detector {name!r}"
    _check_keys(entry, _DETECTOR_KEYS, {"name", "markers", "target"}, where)
    markers = entry["markers"]
    if not isinstance(markers, list) or not markers:
        raise DetectorError(f"{where}: field 'markers' must be a non-empty list")
    targets = entry["target"]
    if not isinstance(targets, list) or not targets:
        raise DetectorError(f"{where}: field 'target' must be a non-empty list of tables")
    return Detector(
        name=name,
        markers=tuple(_marker(m, where) for m in markers),
        targets=tuple(_target(t, f"{where}: target #{i}") for i, t in enumerate(targets)),
        reinstall=_reinstall(entry.get("reinstall", {}), where),
    )


def _check_keys(
    table: Mapping[str, object], allowed: set[str], required: set[str], where: str
) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise DetectorError(f"{where}: unknown field(s) {unknown}")
    missing = sorted(required - set(table))
    if missing:
        raise DetectorError(f"{where}: missing field(s) {missing}")


def _marker(marker: object, where: str) -> str:
    bare = marker[:-1] if isinstance(marker, str) and marker.endswith("/") else marker
    if not isinstance(bare, str) or not _is_plain_name(bare):
        raise DetectorError(f"{where}: field 'markers' has invalid entry {marker!r}")
    return cast(str, marker)


def _target(table: object, where: str) -> Target:
    if not isinstance(table, dict):
        raise DetectorError(f"{where}: must be a table")
    _check_keys(table, _TARGET_KEYS, _TARGET_KEYS, where)
    path, kind = table["path"], table["kind"]
    if not isinstance(path, str) or not _is_plain_name(path):
        raise DetectorError(
            f"{where}: field 'path' must be a single relative folder name, got {path!r}"
        )
    if kind not in KINDS:
        raise DetectorError(f"{where}: field 'kind' must be one of {sorted(KINDS)}, got {kind!r}")
    return Target(path, cast(Kind, kind))


def _reinstall(table: object, where: str) -> tuple[Reinstall, ...]:
    if not isinstance(table, dict):
        raise DetectorError(f"{where}: field 'reinstall' must be a table")
    out = []
    for lockfile, value in table.items():
        bad = DetectorError(f"{where}: field 'reinstall' has invalid entry {lockfile!r}")
        commands = value if isinstance(value, dict) else {"posix": value, "windows": value}
        if not _is_plain_name(lockfile) or set(commands) != _OS_KEYS:
            raise bad
        posix, windows = _argv(commands["posix"]), _argv(commands["windows"])
        if not posix or not windows:
            raise bad
        out.append(Reinstall(lockfile, posix, windows))
    return tuple(out)


def _argv(command: object) -> tuple[str, ...]:
    """Split a command with POSIX shell rules (on every OS); empty on any problem."""
    if not isinstance(command, str):
        return ()
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return ()


def _is_plain_name(name: str) -> bool:
    """True for a single path component: not empty, not absolute, no separators or ``..``."""
    if name in {"", ".", ".."} or "/" in name or "\\" in name:
        return False
    return not (PurePosixPath(name).is_absolute() or PureWindowsPath(name).drive)
