"""Parse and format durations and sizes for CLI flags and output."""

import re
from datetime import timedelta

_DURATION = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([hdwmy])\s*", re.IGNORECASE)
_DURATION_DAYS = {"h": 1 / 24, "d": 1, "w": 7, "m": 30, "y": 365}

_SIZE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*", re.IGNORECASE)
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")


def parse_duration(text: str) -> timedelta:
    """Parse ``48h``, ``60d``, ``12w``, ``6m`` (30 days) or ``1y`` (365 days).

    Args:
        text: The duration string.

    Returns:
        The duration.

    Raises:
        ValueError: The string is not a duration.
    """
    match = _DURATION.fullmatch(text)
    if not match:
        raise ValueError(f"invalid duration {text!r}; use e.g. 48h, 60d, 12w, 6m or 1y")
    return timedelta(days=float(match[1]) * _DURATION_DAYS[match[2].lower()])


def parse_size(text: str) -> int:
    """Parse ``512``, ``200MB``, ``1.5G`` or ``2GiB`` into bytes (units are powers of 1024).

    Args:
        text: The size string.

    Returns:
        Size in bytes.

    Raises:
        ValueError: The string is not a size.
    """
    match = _SIZE.fullmatch(text)
    if not match:
        raise ValueError(f"invalid size {text!r}; use e.g. 500MB or 1GB")
    power = " kmgt".index(match[2].lower() or " ")
    return int(float(match[1]) * 1024**power)


def format_size(num_bytes: int) -> str:
    """Format bytes as ``"0 B"``, ``"812 KB"`` or ``"1.4 GB"`` (powers of 1024).

    Args:
        num_bytes: Size in bytes.

    Returns:
        A short human-readable size.
    """
    size = float(num_bytes)
    for unit in _SIZE_UNITS:
        if abs(size) < 1024 or unit == _SIZE_UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" or size >= 100 else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")  # pragma: no cover
