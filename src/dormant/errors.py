"""Exceptions raised by Dormant. The CLI maps them to exit codes."""

from typing import ClassVar


class DormantError(Exception):
    """Base class for all Dormant errors."""

    exit_code: ClassVar[int] = 1


class ArchiveError(DormantError):
    """Creating or verifying an archive failed."""


class RestoreError(DormantError):
    """Restoring a folder from an archive failed."""


class UnsafePathError(DormantError):
    """A path fell outside an allowed target root."""


class IndexVersionError(DormantError):
    """The index database was written by a newer Dormant than this one."""


class ArchiveNotFoundError(DormantError):
    """No archive record has the requested id."""


class DetectorError(DormantError):
    """``detectors.toml`` is malformed. The message names the detector and field."""


class ConfigError(DormantError):
    """``config.toml`` is malformed. The message names the key."""

    exit_code: ClassVar[int] = 2
