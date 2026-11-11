"""Dormant: archive dependency and build folders in stale projects."""

__all__ = ["__version__"]


def __getattr__(name: str) -> str:
    # Lazy: importlib.metadata costs ~150 ms, and `dormant wake` runs on every marked cd.
    if name != "__version__":
        raise AttributeError(f"module 'dormant' has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("dormant")
    except PackageNotFoundError:  # running from an uninstalled source tree
        return "0.0.0+unknown"
