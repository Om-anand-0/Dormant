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
