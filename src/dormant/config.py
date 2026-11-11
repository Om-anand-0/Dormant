"""Per-user data locations and the optional ``config.toml`` settings file."""

import os
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TypeVar

import platformdirs

from dormant import platform as _platform
from dormant.errors import ConfigError
from dormant.units import parse_duration, parse_size

DATA_DIR_ENV = "DORMANT_DATA_DIR"
CONFIG_NAME = "config.toml"

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class Settings:
    """User settings from ``config.toml``; every field has a default."""

    older_than: timedelta = timedelta(days=60)
    ignore: tuple[str, ...] = ()
    confirm_restore_above: int = 1024**3
    compression_level: int = 3


def data_dir() -> Path:
    """Return Dormant's data directory, creating it if needed.

    ``DORMANT_DATA_DIR`` overrides the platform default (used by tests).

    Returns:
        The data directory.
    """
    override = os.environ.get(DATA_DIR_ENV)
    path = Path(override) if override else platformdirs.user_data_path("dormant", appauthor=False)
    path.mkdir(parents=True, exist_ok=True)
    return path


def archive_dir() -> Path:
    """Return the archive store, creating it if needed.

    On macOS an empty ``.metadata_never_index`` file keeps Spotlight out.

    Returns:
        The archive directory.
    """
    path = data_dir() / "archives"
    path.mkdir(exist_ok=True)
    if _platform.IS_MACOS:
        (path / ".metadata_never_index").touch()
    return path


def index_path() -> Path:
    """Return the path of the SQLite index file.

    Returns:
        ``<data_dir>/index.sqlite3``.
    """
    return data_dir() / "index.sqlite3"


def load_settings() -> Settings:
    """Read ``config.toml`` from the data directory; defaults when it is absent.

    Returns:
        The validated settings.

    Raises:
        ConfigError: The file is not valid TOML, has an unknown key or a bad value.
    """
    path = data_dir() / CONFIG_NAME
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Settings()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    unknown = sorted(set(doc) - set(Settings.__dataclass_fields__))
    if unknown:
        raise ConfigError(f"{path}: unknown key(s) {unknown}")
    defaults = Settings()
    return Settings(
        older_than=_parsed(doc, "older_than", parse_duration, defaults.older_than, path),
        ignore=_ignore(doc.get("ignore", []), path),
        confirm_restore_above=_parsed(
            doc, "confirm_restore_above", parse_size, defaults.confirm_restore_above, path
        ),
        compression_level=_level(doc.get("compression_level", defaults.compression_level), path),
    )


def _parsed(
    doc: dict[str, object], key: str, parse: Callable[[str], _T], default: _T, path: Path
) -> _T:
    value = doc.get(key)
    if value is None:
        return default
    try:
        if not isinstance(value, str):
            raise ValueError(f"must be a string, got {value!r}")
        return parse(value)
    except ValueError as exc:
        raise ConfigError(f"{path}: key {key!r}: {exc}") from exc


def _ignore(value: object, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{path}: key 'ignore' must be a list of non-empty strings")
    return tuple(os.path.expanduser(v) for v in value)  # noqa: PTH111 - globs, not paths


def _level(value: object, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 19:
        raise ConfigError(f"{path}: key 'compression_level' must be an integer from 1 to 19")
    return value
