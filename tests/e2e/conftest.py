from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def archive_tiny_targets(dormant_data_dir: Path) -> None:
    """Fixtures are a few KB; let the CLI archive them despite min_archive_size."""
    (dormant_data_dir / "config.toml").write_text('min_archive_size = "0B"\n', encoding="utf-8")
