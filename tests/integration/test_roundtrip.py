import os
from pathlib import Path

import pytest
from fixtures.trees import Fixture, TreeSpec, build_tree, random_trees, snapshot
from hypothesis import HealthCheck, given, settings

from dormant.archiver import hibernate
from dormant.index import ArchiveState, Index
from dormant.restorer import WakeResult, wake


def _round_trip(project_root: Path, target: Path, index: Index) -> None:
    before = snapshot(target)
    rec = hibernate(target, project_root, "dependencies", index=index)
    assert not os.path.lexists(target)
    assert rec.archive_path is not None
    assert rec.archive_path.is_file()
    assert index.get(rec.id).state == ArchiveState.ACTIVE  # type: ignore[union-attr]
    assert not [p for p in project_root.iterdir() if ".dormant-" in p.name]

    assert wake(target, index=index) == WakeResult.RESTORED
    assert snapshot(target) == before
    assert not rec.archive_path.exists()
    assert index.get(rec.id).state == ArchiveState.RESTORED  # type: ignore[union-attr]
    assert not [p for p in project_root.iterdir() if ".dormant-" in p.name]


@pytest.mark.parametrize("fixture_name", ["node_project", "python_project", "edge_tree"])
def test_round_trip(request: pytest.FixtureRequest, fixture_name: str, index: Index) -> None:
    fx = request.getfixturevalue(fixture_name)
    _round_trip(fx.project_root, fx.target, index)


def test_edge_tree_leaves_outside_alone(edge_tree: Fixture, index: Index) -> None:
    outside = edge_tree.project_root / "outside.txt"
    link = edge_tree.target / "outside_link"
    outside_before = outside.read_bytes()
    link_before = os.readlink(link) if link.is_symlink() else None  # noqa: PTH115 - raw string

    hibernate(edge_tree.target, edge_tree.project_root, "build", index=index)
    assert outside.read_bytes() == outside_before
    wake(edge_tree.target, index=index)

    assert outside.read_bytes() == outside_before
    if link_before is not None:
        assert link.is_symlink()
        assert os.readlink(link) == link_before  # noqa: PTH115


@pytest.mark.slow
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(spec=random_trees())
def test_random_round_trip(
    spec: TreeSpec, tmp_path_factory: pytest.TempPathFactory, index: Index
) -> None:
    root = tmp_path_factory.mktemp("prop")
    target = root / "target"
    build_tree(target, spec)
    _round_trip(root, target, index)
