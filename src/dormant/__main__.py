"""Console-script entry point and ``python -m dormant``."""

import sys


def main() -> None:
    """Run the Dormant CLI.

    ``dormant wake`` is routed to :mod:`dormant.wakecmd` before :mod:`dormant.cli`
    (and so Typer) is imported: the shell hooks call it on every marked ``cd``.
    """
    if len(sys.argv) > 1 and sys.argv[1] == "wake":
        from dormant import wakecmd

        sys.exit(wakecmd.main(sys.argv[2:]))
    from dormant.cli import app

    app(prog_name="dormant")


if __name__ == "__main__":
    main()
