from collections.abc import Iterator
from pathlib import Path

import pytest

from dormant import config
from dormant.index import Index


@pytest.fixture
def index(dormant_data_dir: Path) -> Iterator[Index]:
    """An index in the per-test data dir (archives land beside it)."""
    with Index(config.index_path()) as idx:
        yield idx


TEST_CONFIG = 'min_archive_size = "0B"\n'


@pytest.fixture(autouse=True)
def archive_tiny_targets(dormant_data_dir: Path) -> None:
    """Fixtures are a few KB; let the CLI archive them despite min_archive_size."""
    (dormant_data_dir / "config.toml").write_text(TEST_CONFIG, encoding="utf-8")
