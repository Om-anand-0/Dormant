from pathlib import Path

import pytest
from fixtures.trees import ECOSYSTEMS, make_dotnet_project, make_maven_project, make_rust_project

from dormant.detectors import load_detectors
from dormant.scanner import Project, scan


def _scan(root: Path) -> dict[str, Project]:
    return {p.root.relative_to(root).as_posix(): p for p in scan(root, load_detectors())}


def _targets(p: Project) -> dict[str, tuple[str, str]]:
    return {t.path.name: (t.kind, t.detector) for t in p.targets}


@pytest.mark.parametrize("eco", ECOSYSTEMS)
def test_generator_is_detected(tmp_path: Path, eco: str) -> None:
    detector, make, kinds = ECOSYSTEMS[eco]
    primary = make(tmp_path)
    assert primary == tmp_path / next(iter(kinds))
    found = _scan(tmp_path)
    assert set(found) == {"."}
    assert [d.name for d in found["."].detectors] == [detector]
    assert _targets(found["."]) == {name: (kind, detector) for name, kind in kinds.items()}
    assert all(t.total_bytes > 0 and not t.tracked for t in found["."].targets)


def test_cargo_and_pom_in_one_folder_rust_claims_target(tmp_path: Path) -> None:
    # Both detectors match; the first in detectors.toml (rust) claims the shared folder.
    make_rust_project(tmp_path)
    make_maven_project(tmp_path)
    found = _scan(tmp_path)
    assert [d.name for d in found["."].detectors] == ["rust", "maven"]
    assert _targets(found["."]) == {"target": ("build", "rust")}


def test_rust_workspace_counts_shared_target_once(tmp_path: Path) -> None:
    make_rust_project(tmp_path)
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/*"]\n')
    for member in ("a", "b"):
        (tmp_path / "crates" / member / "src").mkdir(parents=True)
        (tmp_path / "crates" / member / "Cargo.toml").write_text(f'[package]\nname = "{member}"\n')
    # `cargo package` leaves crate copies with their own Cargo.toml inside target/.
    pkg = tmp_path / "target" / "package" / "a-0.1.0"
    pkg.mkdir(parents=True)
    (pkg / "Cargo.toml").write_text('[package]\nname = "a"\n')
    found = _scan(tmp_path)
    assert set(found) == {".", "crates/a", "crates/b"}
    assert [t.path for p in found.values() for t in p.targets] == [tmp_path / "target"]


def test_two_csproj_in_one_folder_is_one_project(tmp_path: Path) -> None:
    make_dotnet_project(tmp_path)
    (tmp_path / "Demo.Tests.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk" />\n')
    found = _scan(tmp_path)
    assert set(found) == {"."}
    assert _targets(found["."]) == {"bin": ("build", "dotnet"), "obj": ("build", "dotnet")}


def test_solution_layout_finds_each_csproj(tmp_path: Path) -> None:
    make_dotnet_project(tmp_path / "src" / "Demo")
    make_dotnet_project(tmp_path / "tests" / "Demo.Tests")
    (tmp_path / "Demo.sln").write_text("Microsoft Visual Studio Solution File\n")
    found = _scan(tmp_path)
    assert set(found) == {"src/Demo", "tests/Demo.Tests"}
    assert all(set(_targets(p)) == {"bin", "obj"} for p in found.values())


def test_unity_needs_projectsettings_folder(tmp_path: Path) -> None:
    (tmp_path / "ProjectSettings").write_text("not a folder\n")
    (tmp_path / "Library" / "x").mkdir(parents=True)
    assert _scan(tmp_path) == {}
