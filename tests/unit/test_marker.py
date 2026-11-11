import json
import uuid
from pathlib import Path

from dormant import marker
from dormant.index import ArchiveRecord, ArchiveState, Index


def _rec(root: Path, state: ArchiveState) -> ArchiveRecord:
    return ArchiveRecord(
        uuid.uuid4().hex,
        root,
        root / "node_modules",
        "dependencies",
        None,
        "",
        1,
        0,
        0.0,
        1,
        state,
    )


def test_marker_follows_records(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    with Index(tmp_path / "i.db") as idx:
        marker.update(root, idx)
        assert not (root / ".dormant").exists()
        rec = _rec(root, ArchiveState.PURGED)
        idx.add(rec)
        marker.update(root, idx)
        info = json.loads(marker.state_path(root).read_text())
        assert info["targets"] == ["node_modules"]
        assert marker.find_root(root / "sub") == root
        idx.set_state(rec.id, ArchiveState.RESTORED)
        marker.update(root, idx)
    assert not (root / ".dormant").exists()
    assert marker.find_root(root / "sub") is None
    assert sorted(p.name for p in root.iterdir()) == ["sub"]  # nothing else written


def test_exclude_added_once(tmp_path: Path) -> None:
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text("# comment")
    proj = tmp_path / "pkg"
    proj.mkdir()
    marker.exclude_from_git(proj)
    marker.exclude_from_git(proj)
    lines = (tmp_path / ".git" / "info" / "exclude").read_text().splitlines()
    assert lines == ["# comment", ".dormant/"]
    assert not (tmp_path / ".gitignore").exists()


def test_worktree_git_file_uses_common_dir(tmp_path: Path) -> None:
    main_git = tmp_path / "main" / ".git"
    wt_dir = main_git / "worktrees" / "wt"
    wt_dir.mkdir(parents=True)
    (wt_dir / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_dir}\n")
    assert marker.git_dir(wt) == main_git.resolve()
    marker.exclude_from_git(wt)
    assert (main_git / "info" / "exclude").read_text() == ".dormant/\n"


def test_submodule_relative_gitdir(tmp_path: Path) -> None:
    modules = tmp_path / ".git" / "modules" / "sub"
    modules.mkdir(parents=True)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".git").write_text("gitdir: ../.git/modules/sub\n")
    assert marker.git_dir(sub) == modules.resolve()


def test_no_repo(tmp_path: Path) -> None:
    marker.exclude_from_git(tmp_path)  # no error, nothing written
    assert list(tmp_path.iterdir()) == []
