from datetime import timedelta
from pathlib import Path

import pytest

from dormant import config
from dormant.errors import ConfigError


def _write(data_dir: Path, text: str) -> None:
    (data_dir / "config.toml").write_text(text, encoding="utf-8")


def test_defaults_without_file() -> None:
    assert config.load_settings() == config.Settings(timedelta(days=60), (), 1024**3, 3)


def test_all_keys(dormant_data_dir: Path) -> None:
    _write(
        dormant_data_dir,
        'older_than = "2w"\nignore = ["~/work/*", "**/backup"]\n'
        'confirm_restore_above = "500MB"\ncompression_level = 19\n',
    )
    s = config.load_settings()
    assert s.older_than == timedelta(weeks=2)
    assert s.ignore == (str(Path("~").expanduser()) + "/work/*", "**/backup")
    assert s.confirm_restore_above == 500 * 1024**2
    assert s.compression_level == 19


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('older_than = "soon"', "older_than"),
        ("older_than = 60", "older_than"),
        ('confirm_restore_above = "big"', "confirm_restore_above"),
        ("compression_level = 0", "compression_level"),
        ("compression_level = 20", "compression_level"),
        ("compression_level = true", "compression_level"),
        ('ignore = "x"', "ignore"),
        ('ignore = [""]', "ignore"),
        ("colour = 1", "colour"),
        ("older_than = ", "cannot read"),
    ],
)
def test_bad_values_name_the_key(dormant_data_dir: Path, text: str, key: str) -> None:
    _write(dormant_data_dir, text)
    with pytest.raises(ConfigError, match=key) as info:
        config.load_settings()
    assert info.value.exit_code == 2
