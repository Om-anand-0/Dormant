"""Phase 3 CLI flow through Typer's CliRunner: scan, hibernate, status, wake, purge, prune."""

import shutil
import sys
from pathlib import Path

import pytest
from fixtures.cli import age, git, records, run, run_json
from fixtures.trees import Fixture, make_node_project, snapshot

from dormant import purge
from dormant.errors import RestoreError
from dormant.index import ArchiveState

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _hibernate(project: Path, *extra: str) -> dict[str, list[dict[str, str]]]:
    data = run_json("hibernate", project, "--yes", *extra)
    assert isinstance(data, dict)
    return data


def _statuses(data: dict[str, list[dict[str, str]]]) -> dict[str, str]:
    return {Path(a["target"]).name: a["status"] for a in data["actions"]}


def _status() -> dict[str, object]:
    data = run_json("status")
    assert isinstance(data, dict)
    return data


def test_full_flow(node_project: Fixture) -> None:
    root, target = node_project.project_root, node_project.target
    age(root)
    before = snapshot(target)

    scanned = run_json("scan", root.parent)
    assert isinstance(scanned, list)
    assert [Path(t["path"]) for t in scanned[0]["targets"]] == [target]

    assert _statuses(_hibernate(root)) == {"node_modules": "archived"}
    assert not target.exists()
    assert (root / ".dormant" / "state").is_file()
    projects = _status()["projects"]
    assert isinstance(projects, list)
    assert projects[0]["root"] == str(root)
    (rec,) = projects[0]["targets"]
    assert rec["state"] == "active"
    archive = Path(rec["archive_path"])
    assert archive.is_file()

    result = run("wake", root)
    assert result.exit_code == 0, result.output
    assert snapshot(target) == before
    assert not (root / ".dormant" / "state").exists()
    assert not archive.exists()
    assert _status()["projects"] == []


def test_hibernate_needs_yes_without_a_tty(node_project: Fixture) -> None:
    age(node_project.project_root)
    before = snapshot(node_project.target)
    result = run("hibernate", node_project.project_root)
    assert result.exit_code == 3
    assert "--yes" in result.output
    assert snapshot(node_project.target) == before
    assert not (node_project.project_root / ".dormant").exists()
    assert records() == []


def test_hibernate_json_without_yes_is_a_dry_run(node_project: Fixture) -> None:
    age(node_project.project_root)
    data = run_json("hibernate", node_project.project_root)
    assert isinstance(data, dict)
    assert data["dry_run"] is True
    assert node_project.target.is_dir()
    assert records() == []


def test_recent_project_is_left_alone(node_project: Fixture) -> None:
    assert _hibernate(node_project.project_root)["actions"] == []
    assert node_project.target.is_dir()


# --- git ----------------------------------------------------------------------------------


def _commit_all(repo: Path, *paths: str) -> None:
    git(repo, "add", "--", *paths)
    git(repo, "commit", "-q", "-m", "init")


@needs_git
def test_tracked_dist_is_skipped_and_untouched(node_project: Fixture) -> None:
    root = node_project.project_root
    (root / "dist").mkdir()
    (root / "dist" / "bundle.js").write_text("built\n")
    git(root, "init", "-q")
    _commit_all(root, "package.json", "package-lock.json", "index.js", "dist")
    age(root)
    before = snapshot(root / "dist")
    data = _hibernate(root)
    by_name = {Path(a["target"]).name: a for a in data["actions"]}
    assert by_name["dist"]["status"] == "skipped"
    assert "git-tracked" in by_name["dist"]["reason"]
    assert by_name["node_modules"]["status"] == "archived"
    assert snapshot(root / "dist") == before


@needs_git
def test_exclude_entry_added_once_over_two_cycles(node_project: Fixture) -> None:
    root = node_project.project_root
    git(root, "init", "-q")
    _commit_all(root, "package.json", "package-lock.json", "index.js")
    for _ in range(2):
        age(root)
        assert _statuses(_hibernate(root)) == {"node_modules": "archived"}
        assert run("wake", root).exit_code == 0
    lines = (root / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines.count(".dormant/") == 1
    assert not (root / ".gitignore").exists()


@needs_git
@pytest.mark.parametrize("layout", ["worktree", "separate-gitdir"])
def test_gitfile_layouts(tmp_path: Path, layout: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    make_node_project(repo)
    if layout == "worktree":
        git(repo, "init", "-q")
        _commit_all(repo, "package.json", "package-lock.json", "index.js")
        project = tmp_path / "wt"
        git(repo, "worktree", "add", "-q", str(project), "-b", "side")
        gitdir = repo / ".git"
        make_node_project(project)
    else:
        project = repo
        git(project, "init", "-q", "--separate-git-dir", str(tmp_path / "gd"))
        _commit_all(project, "package.json", "package-lock.json", "index.js")
        gitdir = tmp_path / "gd"
    assert (project / ".git").is_file()
    age(project)
    assert _statuses(_hibernate(project)) == {"node_modules": "archived"}
    assert (gitdir / "info" / "exclude").read_text().splitlines().count(".dormant/") == 1
    assert run("wake", project).exit_code == 0
    assert (project / "node_modules").is_dir()


# --- caches -------------------------------------------------------------------------------


def test_cache_is_deleted_not_archived(python_project: Fixture) -> None:
    root = python_project.project_root
    cache = root / ".pytest_cache"
    cache.mkdir()
    (cache / "README.md").write_text("cache\n")
    age(root)
    before = snapshot(python_project.target)
    assert _statuses(_hibernate(root, "--kind", "cache")) == {".pytest_cache": "deleted"}
    assert not cache.exists()
    assert snapshot(python_project.target) == before  # the venv is not a cache
    assert records() == []
    assert not (root / ".dormant").exists()


# --- purge --------------------------------------------------------------------------------


@pytest.fixture
def fake_reinstall(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    """Replace the package manager with a python snippet that recreates node_modules."""
    seen: list[tuple[str, ...]] = []
    real = purge.run_reinstall
    snippet = (
        "import pathlib; p = pathlib.Path('node_modules/dep'); "
        "p.mkdir(parents=True); (p / 'index.js').write_text('x')"
    )

    def fake(argv: tuple[str, ...], cwd: Path) -> None:
        seen.append(tuple(argv))
        real([sys.executable, "-c", snippet], cwd)

    monkeypatch.setattr(purge, "run_reinstall", fake)
    return seen


def test_purge_then_wake_reinstalls(
    node_project: Fixture, fake_reinstall: list[tuple[str, ...]]
) -> None:
    root = node_project.project_root
    age(root)
    assert _statuses(_hibernate(root, "--purge")) == {"node_modules": "purged"}
    assert not node_project.target.exists()
    (rec,) = records()
    assert rec.state == ArchiveState.PURGED
    assert rec.archive_path is None
    assert rec.reinstall == ("npm", "ci")

    result = run("wake", root)
    assert result.exit_code == 0, result.output
    assert fake_reinstall == [("npm", "ci")]
    assert (node_project.target / "dep" / "index.js").read_text() == "x"
    assert [r.state for r in records()] == [ArchiveState.RESTORED]
    assert not (root / ".dormant" / "state").exists()


def test_wake_reinstall_on_archived_target(
    node_project: Fixture, fake_reinstall: list[tuple[str, ...]]
) -> None:
    root = node_project.project_root
    age(root)
    assert _statuses(_hibernate(root)) == {"node_modules": "archived"}
    result = run("wake", root, "--reinstall")
    assert result.exit_code == 0, result.output
    assert fake_reinstall == [("npm", "ci")]
    assert (node_project.target / "dep").is_dir()
    assert [r.state for r in records()] == [ArchiveState.STALE]  # archive kept for prune
    assert run("prune", "--yes").exit_code == 0
    assert records() == []


def test_failed_reinstall_keeps_the_purged_record(
    node_project: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = node_project.project_root
    age(root)
    _hibernate(root, "--purge")

    def boom(argv: tuple[str, ...], cwd: Path) -> None:
        raise RestoreError("npm exploded")

    monkeypatch.setattr(purge, "run_reinstall", boom)
    result = run("wake", root)
    assert result.exit_code == 1
    assert [r.state for r in records()] == [ArchiveState.PURGED]
    assert (root / ".dormant" / "state").is_file()


# --- confirm_restore_above ----------------------------------------------------------------


def test_big_restore_skipped_without_a_tty(node_project: Fixture, dormant_data_dir: Path) -> None:
    (dormant_data_dir / "config.toml").write_text('confirm_restore_above = "1B"\n')
    root = node_project.project_root
    age(root)
    _hibernate(root)
    result = run("wake", root)
    assert result.exit_code == 0
    assert "Skipped node_modules" in result.output
    assert not node_project.target.exists()
    projects = _status()["projects"]
    assert isinstance(projects, list)
    assert len(projects) == 1
    assert (root / ".dormant" / "state").is_file()


# --- prune --------------------------------------------------------------------------------


def _prune_items(*args: str) -> list[dict[str, object]]:
    data = run_json("prune", "--yes", *args)
    assert isinstance(data, dict)
    assert data["dry_run"] is False
    items = data["items"]
    assert isinstance(items, list)
    return items


def test_prune_archive_of_deleted_project(node_project: Fixture) -> None:
    root = node_project.project_root
    age(root)
    _hibernate(root)
    (rec,) = records()
    assert rec.archive_path is not None
    shutil.rmtree(root)
    (item,) = _prune_items()
    assert item["status"] == "deleted"
    assert not rec.archive_path.exists()
    assert records() == []


def test_prune_stale_record(node_project: Fixture) -> None:
    root = node_project.project_root
    age(root)
    _hibernate(root)
    node_project.target.mkdir()  # reinstalled by hand
    assert run("wake", root).exit_code == 0
    assert [r.state for r in records()] == [ArchiveState.STALE]
    assert _status()["stale_archives"] == 1
    (item,) = _prune_items()
    assert item["status"] == "deleted"
    assert records() == []


def test_prune_never_deletes_the_only_copy(node_project: Fixture) -> None:
    root = node_project.project_root
    age(root)
    _hibernate(root)
    (rec,) = records()
    assert _prune_items("--older", "0d") == []
    assert rec.archive_path is not None
    assert rec.archive_path.is_file()
    assert [r.state for r in records()] == [ArchiveState.ACTIVE]


def test_prune_dry_run_changes_nothing(node_project: Fixture) -> None:
    age(node_project.project_root)
    _hibernate(node_project.project_root)
    shutil.rmtree(node_project.project_root)
    data = run_json("prune")
    assert isinstance(data, dict)
    assert data["dry_run"] is True
    assert len(records()) == 1
    assert run("prune").exit_code == 3  # non-TTY, no --yes
    assert len(records()) == 1
