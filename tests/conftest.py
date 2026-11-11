from collections.abc import Callable
from pathlib import Path

import pytest
from fixtures.trees import Fixture, make_edge_tree, make_node_project, make_python_project


@pytest.fixture(autouse=True)
def dormant_data_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point Dormant's data dir (archives, index) at a fresh temp dir for every test."""
    path = tmp_path_factory.mktemp("dormant-data")
    monkeypatch.setenv("DORMANT_DATA_DIR", str(path))
    return path


def _build(tmp_path: Path, make: Callable[[Path], Path]) -> Fixture:
    root = tmp_path / make.__name__.removeprefix("make_")  # unique when a test uses several
    root.mkdir()
    return Fixture(root, make(root))


@pytest.fixture
def node_project(tmp_path: Path) -> Fixture:
    return _build(tmp_path, make_node_project)


@pytest.fixture
def python_project(tmp_path: Path) -> Fixture:
    return _build(tmp_path, make_python_project)


@pytest.fixture
def edge_tree(tmp_path: Path) -> Fixture:
    return _build(tmp_path, make_edge_tree)
