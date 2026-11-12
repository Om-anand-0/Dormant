import json
import os
from pathlib import Path

from fixtures.trees import make_node_project
from typer.testing import CliRunner

from dormant.cli import app


def _project(tmp_path: Path) -> Path:
    make_node_project(tmp_path / "app")
    (tmp_path / "app" / ".turbo").mkdir()
    (tmp_path / "app" / ".turbo" / "log").write_text("cache")
    return tmp_path


def test_scan_json_shape(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["scan", str(_project(tmp_path)), "--json"])
    assert result.exit_code == 0, result.output
    (proj,) = json.loads(result.stdout)
    assert proj["root"] == str(tmp_path / "app")
    assert proj["detectors"] == ["node"]
    assert proj["in_use"] is False
    assert proj["last_activity"].endswith("Z")
    assert {t["kind"] for t in proj["targets"]} == {"dependencies", "cache"}
    assert proj["reclaimable_bytes"] == sum(t["reclaimable_bytes"] for t in proj["targets"])
    assert set(proj["targets"][0]) == {
        "path",
        "kind",
        "detector",
        "reclaimable_bytes",
        "total_bytes",
        "hardlinked",
        "tracked",
    }


def test_scan_kind_filter(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["scan", str(_project(tmp_path)), "--json", "--kind", "cache"])
    (proj,) = json.loads(result.stdout)
    assert [t["kind"] for t in proj["targets"]] == ["cache"]


def test_scan_older_filters_fresh_projects(tmp_path: Path) -> None:
    root = _project(tmp_path)
    result = CliRunner().invoke(app, ["scan", str(root), "--json", "--older", "1d"])
    assert json.loads(result.stdout) == []
    for dirpath, _, files in os.walk(root):
        for f in files:
            os.utime(Path(dirpath) / f, (1_000_000_000, 1_000_000_000))
    result = CliRunner().invoke(app, ["scan", str(root), "--json", "--older", "1y"])
    assert len(json.loads(result.stdout)) == 1


def test_scan_table(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["scan", str(_project(tmp_path))])
    assert result.exit_code == 0
    assert "reclaimable" in result.stdout


def test_scan_usage_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["scan", str(tmp_path), "--older", "soon"]).exit_code == 2
    assert runner.invoke(app, ["scan", str(tmp_path), "--kind", "junk"]).exit_code == 2
    assert runner.invoke(app, ["scan", str(tmp_path / "missing")]).exit_code == 2


def test_scan_hides_tiny_targets_unless_all(tmp_path: Path) -> None:
    root = _project(tmp_path)  # every target here is a few bytes, under the 1 MB default
    shown = CliRunner().invoke(app, ["scan", str(root)], env={"COLUMNS": "200"})
    assert shown.exit_code == 0, shown.output
    assert "node_modules" not in shown.stdout
    assert "folders under 1.0 MB hidden; --all shows them" in shown.stdout
    everything = CliRunner().invoke(app, ["scan", str(root), "--all"], env={"COLUMNS": "200"})
    assert "node_modules" in everything.stdout
    assert "hidden" not in everything.stdout
    data = json.loads(CliRunner().invoke(app, ["scan", str(root), "--json"]).stdout)
    assert {Path(t["path"]).name for t in data[0]["targets"]} >= {"node_modules"}
