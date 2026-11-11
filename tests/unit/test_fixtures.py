"""Sanity checks for the test fixtures themselves: if these lie, round-trip tests prove nothing."""

import os
from collections.abc import Callable
from pathlib import Path, PurePath

import pytest
from fixtures.trees import (
    DEEP_FILE,
    IS_WINDOWS,
    Entry,
    Fixture,
    TreeSpec,
    _long,
    build_tree,
    make_edge_tree,
    make_node_project,
    make_python_project,
    random_trees,
    requires_symlinks,
    snapshot,
)
from hypothesis import HealthCheck, given, settings

GENERATORS = [make_node_project, make_python_project, make_edge_tree]


@pytest.mark.parametrize("make", GENERATORS)
def test_generator_is_deterministic(tmp_path: Path, make: Callable[[Path], Path]) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first = snapshot(make(tmp_path / "a"))
    assert len(first) > 5
    assert first == snapshot(make(tmp_path / "b"))


def test_data_dir_env(dormant_data_dir: Path, tmp_path: Path) -> None:
    assert os.environ["DORMANT_DATA_DIR"] == str(dormant_data_dir)
    assert dormant_data_dir.is_dir()
    assert not dormant_data_dir.is_relative_to(tmp_path)


def test_fixtures_shape(node_project: Fixture, python_project: Fixture, edge_tree: Fixture) -> None:
    assert node_project.target == node_project.project_root / "node_modules"
    assert (node_project.project_root / "package-lock.json").is_file()
    assert python_project.target.name == ".venv"
    assert (python_project.project_root / "uv.lock").is_file()
    assert (edge_tree.project_root / "outside.txt").is_file()


def test_edge_tree_contents(edge_tree: Fixture) -> None:
    snap = snapshot(edge_tree.target)
    assert snap["empty"] == Entry("dir")
    assert snap["nested_empty/a/b"] == Entry("dir")
    assert snap["zero.bin"].size == 0
    assert snap["all_bytes.bin"].size == 256
    assert snap["random_2mb.bin"].size == 2 * 1024 * 1024
    assert {
        "café/日本語.txt",
        "café/emoji_🎉.txt",
        "name with spaces.txt",
        ".dotdir/.inner",
    } <= snap.keys()


def test_long_path_snapshotted(edge_tree: Fixture) -> None:
    assert len(DEEP_FILE) > 300
    assert len(str(edge_tree.target / DEEP_FILE)) > 300
    assert _long(edge_tree.target / DEEP_FILE).is_file()
    assert snapshot(edge_tree.target)[DEEP_FILE].kind == "file"


def test_detects_changed_byte(edge_tree: Fixture) -> None:
    before = snapshot(edge_tree.target)
    f = edge_tree.target / "all_bytes.bin"
    data = bytearray(f.read_bytes())
    data[100] ^= 1
    f.write_bytes(bytes(data))
    after = snapshot(edge_tree.target)
    assert after["all_bytes.bin"].size == before["all_bytes.bin"].size
    assert after != before


def test_detects_removed_empty_dir(edge_tree: Fixture) -> None:
    before = snapshot(edge_tree.target)
    (edge_tree.target / "nested_empty" / "a" / "b").rmdir()
    assert snapshot(edge_tree.target) != before


@pytest.mark.skipif(IS_WINDOWS, reason="no POSIX exec bit on Windows")
def test_detects_lost_exec_bit(node_project: Fixture) -> None:
    cli = node_project.target / "pkg" / "cli.js"
    before = snapshot(node_project.target)
    assert before["pkg/cli.js"].executable
    cli.chmod(0o644)
    assert snapshot(node_project.target)["pkg/cli.js"] != before["pkg/cli.js"]


def test_ignores_group_other_bits(node_project: Fixture) -> None:
    before = snapshot(node_project.target)
    cli = node_project.target / "pkg" / "cli.js"
    cli.chmod(0o700)
    assert snapshot(node_project.target) == before


@requires_symlinks
def test_symlinks_recorded_not_followed(edge_tree: Fixture) -> None:
    snap = snapshot(edge_tree.target)
    assert snap["outside_link"] == Entry("symlink", target=str(PurePath("..", "outside.txt")))
    assert snap["dangling"] == Entry("symlink", target="does_not_exist.txt")
    assert snap["link_to_dir"].kind == "symlink"
    assert not any(k.startswith("link_to_dir/") for k in snap)
    assert (edge_tree.target / "link_to_file").read_bytes() == bytes(range(256))
    assert (edge_tree.target / "outside_link").read_text() == "outside the target\n"


@requires_symlinks
def test_detects_changed_symlink_target(edge_tree: Fixture) -> None:
    before = snapshot(edge_tree.target)
    link = edge_tree.target / "link_to_file"
    link.unlink()
    link.symlink_to("zero.bin")
    assert snapshot(edge_tree.target)["link_to_file"] != before["link_to_file"]


@requires_symlinks
def test_node_bin_is_symlink(node_project: Fixture) -> None:
    assert snapshot(node_project.target)["pkg/cli.js"].kind == "file"
    assert snapshot(node_project.target)[".bin/pkg"].kind == "symlink"


@pytest.mark.skipif(IS_WINDOWS, reason="venvs use Scripts/python.exe on Windows")
def test_venv_python_is_absolute_symlink(python_project: Fixture) -> None:
    assert snapshot(python_project.target)["bin/python"] == Entry(
        "symlink", target="/usr/bin/python3.12"
    )


@given(spec=random_trees())
@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_build_tree_roundtrips_through_snapshot(
    tmp_path_factory: pytest.TempPathFactory, spec: TreeSpec
) -> None:
    root = tmp_path_factory.mktemp("tree")
    build_tree(root, spec)
    snap = snapshot(root)

    def expected(s: TreeSpec, prefix: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, v in s.items():
            out[prefix + name] = "dir" if isinstance(v, dict) else "file"
            if isinstance(v, dict):
                out |= expected(v, f"{prefix}{name}/")
        return out

    assert {k: e.kind for k, e in snap.items()} == expected(spec, "")
