"""Console-script entry point and ``python -m dormant``."""


def main() -> None:
    """Run the Dormant CLI."""
    # The `wake` fast path (Phase 3) dispatches here on sys.argv[1] == "wake",
    # before cli (and therefore Typer) is imported. Keep this module import-light.
    from dormant.cli import app

    app(prog_name="dormant")


if __name__ == "__main__":
    main()
