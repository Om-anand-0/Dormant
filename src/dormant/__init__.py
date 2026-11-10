"""Dormant: archive dependency and build folders in stale projects."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("dormant")
except PackageNotFoundError:  # running from an uninstalled source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
