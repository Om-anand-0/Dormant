from pathlib import Path

import pytest

from dormant import platform
from dormant.detectors import Detector, Reinstall, Target, load_detectors
from dormant.errors import DetectorError

GOOD = """
[[detector]]
name = "node"
markers = ["package.json"]

[[detector.target]]
path = "node_modules"
kind = "dependencies"

[detector.reinstall]
"pnpm-lock.yaml" = "pnpm install"
"package-lock.json" = "npm ci"
"""


def _load(tmp_path: Path, text: str) -> tuple[Detector, ...]:
    f = tmp_path / "detectors.toml"
    f.write_text(text, encoding="utf-8")
    return load_detectors(f)


def test_good_file(tmp_path: Path) -> None:
    (node,) = _load(tmp_path, GOOD)
    assert node == Detector(
        "node",
        ("package.json",),
        (Target("node_modules", "dependencies"),),
        (
            Reinstall("pnpm-lock.yaml", ("pnpm", "install"), ("pnpm", "install")),
            Reinstall("package-lock.json", ("npm", "ci"), ("npm", "ci")),
        ),
    )


def test_bundled_has_all_ecosystems() -> None:
    names = {d.name for d in load_detectors()}
    assert names == {
        "node",
        "python",
        "rust",
        "maven",
        "gradle",
        "flutter",
        "php",
        "dotnet",
        "cocoapods",
        "unity",
    }
    assert load_detectors() is load_detectors()  # cached


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('name = "node"', 'name = ""', "'name'"),
        ('markers = ["package.json"]', "markers = []", "'markers'"),
        ('markers = ["package.json"]', 'markers = ["../x"]', "'markers'"),
        ('kind = "dependencies"', 'kind = "junk"', "'kind'"),
        ('path = "node_modules"', 'path = "/abs"', "'path'"),
        ('path = "node_modules"', 'path = "C:/x"', "'path'"),
        ('path = "node_modules"', 'path = ".."', "'path'"),
        ('path = "node_modules"', 'path = "a/../b"', "'path'"),
        ('kind = "dependencies"', 'kind = "build"\nsize = 1', "unknown field"),
        ('name = "node"', 'name = "node"\ncolour = "red"', "unknown field"),
        ('kind = "dependencies"', "", "missing field"),
        ('"npm ci"', '""', "'reinstall'"),
        ('"npm ci"', '"npm \'ci"', "'reinstall'"),
        ('"npm ci"', '{ posix = "npm ci" }', "'reinstall'"),
        ('"npm ci"', '{ posix = "a", windows = "b", mac = "c" }', "'reinstall'"),
        ('"npm ci"', "3", "'reinstall'"),
        ("[[detector]]", "oops = 1\n[[detector]]", "unknown top-level"),
        ("[[detector]]", "[[detector", "invalid TOML"),
    ],
)
def test_validation_errors(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(DetectorError, match=message) as info:
        _load(tmp_path, GOOD.replace(old, new, 1))
    if "name" not in message and "TOML" not in message and "top-level" not in message:
        assert "'node'" in str(info.value)  # errors name the detector


def test_duplicate_names(tmp_path: Path) -> None:
    with pytest.raises(DetectorError, match="duplicate"):
        _load(tmp_path, GOOD + GOOD.replace("[detector.reinstall]", "[detector.reinstall]\n"))


def test_missing_targets(tmp_path: Path) -> None:
    with pytest.raises(DetectorError, match="'target'"):
        _load(tmp_path, '[[detector]]\nname = "x"\nmarkers = ["a"]\ntarget = []\n')


@pytest.mark.parametrize("windows", [True, False])
def test_per_os_reinstall(monkeypatch: pytest.MonkeyPatch, windows: bool) -> None:
    monkeypatch.setattr(platform, "IS_WINDOWS", windows)
    gradle = next(d for d in load_detectors() if d.name == "gradle")
    want = ("./gradlew.bat", "build") if windows else ("./gradlew", "build")
    assert gradle.reinstall[0].argv() == want


def test_python_requirements_not_reinstallable() -> None:
    python = next(d for d in load_detectors() if d.name == "python")
    assert [r.lockfile for r in python.reinstall] == ["uv.lock", "poetry.lock"]
    assert python.reinstall[0].argv() == ("uv", "sync", "--frozen")
