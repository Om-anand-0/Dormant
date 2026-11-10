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
