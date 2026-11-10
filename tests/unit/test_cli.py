import re

import pytest
from typer.testing import CliRunner

from dormant import errors
from dormant.cli import app


def test_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert re.fullmatch(r"dormant \d+\.\d+\.\d+\S*\n", result.output)


@pytest.mark.parametrize("cls", [errors.ArchiveError, errors.RestoreError, errors.UnsafePathError])
def test_errors_subclass_base(cls: type[errors.DormantError]) -> None:
    assert issubclass(cls, errors.DormantError)
    assert cls.exit_code == 1
