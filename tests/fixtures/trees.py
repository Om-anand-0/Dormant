"""Snapshot a directory tree and generate fixture projects into tmp_path.

Typical round trip::

    target = make_node_project(tmp_path)
    before = snapshot(target)
    ...hibernate, assert gone, wake...
    assert snapshot(target) == before
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal, TypeAlias

import pytest
from hypothesis import strategies as st

IS_WINDOWS = os.name == "nt"

Kind = Literal["file", "dir", "symlink"]
TreeSpec: TypeAlias = dict[str, "bytes | TreeSpec"]


@dataclass(frozen=True, slots=True)
class Entry:
    """One filesystem entry in a snapshot. Fields not relevant to ``kind`` stay ``None``."""

    kind: Kind
    size: int | None = None
    blake2b: str | None = None
    executable: bool = False
    target: str | None = None


@dataclass(frozen=True, slots=True)
class Fixture:
    """A generated project: ``target`` is the folder Dormant would hibernate."""

    project_root: Path
    target: Path


def _long(path: Path | str) -> Path:
    """Return an absolute path, in ``\\\\?\\`` extended form on Windows (>260 chars safe)."""
    s = os.path.abspath(path)  # noqa: PTH100 - abspath does not touch the disk or resolve links
    if IS_WINDOWS and not s.startswith("\\\\?\\"):
        s = "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return Path(s)


def _hash(path: str) -> str:
    h = hashlib.blake2b()
    with open(path, "rb") as f:  # noqa: PTH123
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(root: Path) -> dict[str, Entry]:
    """Describe every entry under ``root`` (excluding root itself), keyed by POSIX relative path.

    Never follows symlinks. Records content hash, size and owner exec bit for files,
    the raw ``os.readlink`` string for symlinks, and every directory (so empty ones count).
    Mtimes and other mode bits are ignored on purpose.
    """
    out: dict[str, Entry] = {}
    stack: list[tuple[str, str]] = [(str(_long(root)), "")]
    while stack:
        dir_path, rel_dir = stack.pop()
        with os.scandir(dir_path) as it:
            for e in it:
                rel = f"{rel_dir}{e.name}"
                if e.is_symlink():
                    out[rel] = Entry("symlink", target=os.readlink(e.path))  # noqa: PTH115
                elif e.is_dir(follow_symlinks=False):
                    out[rel] = Entry("dir")
                    stack.append((e.path, rel + "/"))
                else:
                    st_ = e.stat(follow_symlinks=False)
                    exe = not IS_WINDOWS and bool(st_.st_mode & stat.S_IXUSR)
                    out[rel] = Entry("file", st_.st_size, _hash(e.path), exe)
    return out


# --- symlink support -------------------------------------------------------


def symlinks_supported(tmp: Path) -> bool:
    """Probe whether this process can create symlinks inside ``tmp``."""
    probe = tmp / f".symlink-probe-{os.getpid()}"
    try:
        probe.symlink_to("nowhere")
    except (OSError, NotImplementedError):
        return False
    probe.unlink()
    return True


with tempfile.TemporaryDirectory() as _d:
    SYMLINKS = symlinks_supported(Path(_d))

requires_symlinks = pytest.mark.skipif(
    not SYMLINKS, reason="cannot create symlinks (Windows needs Developer Mode or admin)"
)


# --- low-level writers (all go through _long) -------------------------------


def _write(path: Path, data: bytes | str, *, executable: bool = False) -> None:
    p = _long(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode() if isinstance(data, str) else data)
    if executable and not IS_WINDOWS:
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _mkdir(path: Path) -> None:
    _long(path).mkdir(parents=True, exist_ok=True)


def _symlink(link: Path, target: str, *, is_dir: bool = False) -> bool:
    """Create ``link`` -> ``target`` (stored verbatim). Returns False if symlinks unsupported."""
    if not SYMLINKS:
        return False
    p = _long(link)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.symlink_to(target, target_is_directory=is_dir)
    return True


def _rel(*parts: str) -> str:
    """Relative link target with native separators, so the link resolves on Windows too."""
    return str(PurePath(*parts))


def _pkg_json(name: str, version: str, **extra: object) -> str:
    return json.dumps({"name": name, "version": version, **extra}, indent=2) + "\n"


# --- generators ---------------------------------------------------------------


def make_node_project(root: Path) -> Path:
    """Create a tiny npm project in ``root``; return its ``node_modules``."""
    _write(root / "package.json", _pkg_json("demo-app", "1.0.0", dependencies={"left-pad": "^1"}))
    _write(root / "package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {}}) + "\n")
    _write(root / "index.js", "require('left-pad');\n")
    nm = root / "node_modules"
    _write(nm / ".package-lock.json", json.dumps({"lockfileVersion": 3}) + "\n")
    _write(nm / "left-pad" / "package.json", _pkg_json("left-pad", "1.3.0"))
    _write(nm / "left-pad" / "index.js", "module.exports = (s, n) => String(s).padStart(n);\n")
    _write(nm / "chalk" / "package.json", _pkg_json("chalk", "5.3.0", type="module"))
    _write(nm / "chalk" / "source" / "index.js", "export default {};\n")
    nested = nm / "chalk" / "node_modules" / "ansi-styles"
    _write(nested / "package.json", _pkg_json("ansi-styles", "6.2.1"))
    _write(nested / "index.js", "export default {};\n")
    cli = "#!/usr/bin/env node\nconsole.log('hi');\n"
    _write(nm / "pkg" / "package.json", _pkg_json("pkg", "0.1.0", bin={"pkg": "cli.js"}))
    _write(nm / "pkg" / "cli.js", cli, executable=True)
    if not _symlink(nm / ".bin" / "pkg", _rel("..", "pkg", "cli.js")):
        _write(nm / ".bin" / "pkg", cli, executable=True)
    return nm


def make_python_project(root: Path) -> Path:
    """Create a tiny uv project in ``root``; return its ``.venv``."""
    _write(root / "pyproject.toml", '[project]\nname = "demo"\nversion = "0.1.0"\n')
    _write(root / "uv.lock", 'version = 1\nrequires-python = ">=3.11"\n')
    _write(root / "main.py", "import tinypkg\n")
    venv = root / ".venv"
    _write(venv / "pyvenv.cfg", "home = /usr/bin\nversion_info = 3.12.4\n")
    _write(venv / ".gitignore", "*\n")
    site = venv / "Lib" / "site-packages" if IS_WINDOWS else venv / "lib/python3.12/site-packages"
    _write(site / "tinypkg" / "__init__.py", "__version__ = '1.0'\n")
    _write(site / "tinypkg" / "core.py", "def f() -> int:\n    return 1\n")
    _write(site / "tinypkg-1.0.dist-info" / "METADATA", "Name: tinypkg\nVersion: 1.0\n")
    _write(site / "tinypkg-1.0.dist-info" / "RECORD", "")
    _mkdir(site / "tinypkg" / "__pycache__")
    if IS_WINDOWS:
        _write(venv / "Scripts" / "python.exe", b"MZ\x90\x00" + bytes(60))
        _write(venv / "Scripts" / "activate.bat", "@echo off\r\n")
    else:
        # Real venvs point at an absolute interpreter path; it need not exist here.
        _symlink(venv / "bin" / "python", "/usr/bin/python3.12")
        _write(venv / "bin" / "activate", "# activate\n")
    return venv


DEEP_FILE = "/".join(f"deep_segment_{i:02d}_abcdefgh" for i in range(12)) + "/long_file.txt"
"""Path of the long file, relative to the edge target (over 300 chars on its own)."""


def make_edge_tree(root: Path) -> Path:
    """Create ``root/edge`` full of edge cases; return it. Also creates ``root/outside.txt``."""
    t = root / "edge"
    _mkdir(t / "empty")
    _mkdir(t / "nested_empty" / "a" / "b")
    _write(t / "zero.bin", b"")
    _write(t / "all_bytes.bin", bytes(range(256)))
    _write(t / "random_2mb.bin", random.Random(1234).randbytes(2 * 1024 * 1024))  # noqa: S311 - seeded on purpose
    _write(t / "café" / "日本語.txt", "こんにちは\n")
    _write(t / "café" / "emoji_🎉.txt", "party\n")
    _write(t / "name with spaces.txt", "spaces\n")
    _write(t / ".hidden", "dot\n")
    _write(t / ".dotdir" / ".inner", "dot dir\n")
    _write(t / DEEP_FILE, "deep\n")
    _write(t / "run.sh", "#!/bin/sh\necho run\n", executable=True)
    _write(root / "outside.txt", "outside the target\n")
    _symlink(t / "link_to_file", "all_bytes.bin")
    _symlink(t / "link_to_dir", "café", is_dir=True)
    _symlink(t / "dangling", "does_not_exist.txt")
    _symlink(t / "outside_link", _rel("..", "outside.txt"))
    return t


# --- hypothesis ------------------------------------------------------------------

_ALPHABET = "abcXYZ019 .-éñü日"
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(10)}


def _valid_name(name: str) -> bool:
    return (
        name not in {".", ".."}
        and not name.endswith((".", " "))
        and name.split(".")[0].strip().upper() not in _RESERVED
    )


_names = st.text(_ALPHABET, min_size=1, max_size=12).filter(_valid_name)


def _depth(spec: TreeSpec) -> int:
    return 1 + max((_depth(v) for v in spec.values() if isinstance(v, dict)), default=0)


def _dir_of(children: st.SearchStrategy[bytes | TreeSpec]) -> st.SearchStrategy[TreeSpec]:
    entries = st.lists(st.tuples(_names, children), max_size=5, unique_by=lambda e: e[0].casefold())
    return entries.map(dict)


def random_trees() -> st.SearchStrategy[TreeSpec]:
    """Strategy for small random trees: ``{name: bytes (file) | dict (dir)}``, depth <= 4.

    Names are valid on Windows, macOS and Linux and unique case-insensitively per directory.
    """
    node: st.SearchStrategy[bytes | TreeSpec] = st.recursive(
        st.binary(max_size=256), _dir_of, max_leaves=30
    )
    return _dir_of(node).filter(lambda s: _depth(s) <= 4)


def build_tree(root: Path, spec: TreeSpec) -> None:
    """Materialise ``spec`` under ``root`` (created if missing)."""
    _mkdir(root)
    for name, value in spec.items():
        if isinstance(value, dict):
            build_tree(root / name, value)
        else:
            _write(root / name, value)


# --- ecosystem generators -----------------------------------------------------------
# Each returns its primary target; ECOSYSTEMS (below) lists every target and its kind.

_JAVA_CLASS = b"\xca\xfe\xba\xbe\x00\x00\x00\x41" + bytes(range(64))
_ZIP = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + bytes(range(48))
_PE = b"MZ\x90\x00\x03\x00\x00\x00" + bytes(56) + b"PE\x00\x00"
_ELF = b"\x7fELF\x02\x01\x01" + bytes(range(120))


def make_rust_project(root: Path) -> Path:
    """Create a tiny Cargo binary crate in ``root``; return its ``target``."""
    _write(root / "Cargo.toml", '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n')
    _write(root / "Cargo.lock", 'version = 3\n\n[[package]]\nname = "demo"\nversion = "0.1.0"\n')
    _write(root / "src" / "main.rs", 'fn main() {\n    println!("hi");\n}\n')
    t = root / "target"
    _write(t / "CACHEDIR.TAG", "Signature: 8a477f597d28d172789f06886806bc55\n")
    _write(t / ".rustc_info.json", '{"rustc_fingerprint":1}\n')
    dbg = t / "debug"
    _write(dbg / "demo", _ELF, executable=True)
    _write(dbg / "demo.d", "/src/main.rs:\n")
    _write(dbg / "deps" / "libserde-1a2b3c.rlib", b"!<arch>\n" + bytes(range(256)) * 4)
    _write(dbg / ".fingerprint" / "demo-9f8e7d" / "bin-demo", "0123456789abcdef")
    _write(dbg / "incremental" / "démo-1x2y" / "s-abc" / "query-cache.bin", bytes(128))
    _mkdir(dbg / "build")
    _mkdir(t / "release")
    return t


def make_maven_project(root: Path) -> Path:
    """Create a tiny Maven project in ``root``; return its ``target``."""
    _write(
        root / "pom.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n<project>\n  <modelVersion>4.0.0</modelVersion>\n'
        "  <groupId>com.example</groupId>\n  <artifactId>demo</artifactId>\n"
        "  <version>1.0</version>\n</project>\n",
    )
    _write(root / "src/main/java/com/example/App.java", "package com.example;\nclass App {}\n")
    t = root / "target"
    _write(t / "classes" / "com" / "example" / "App.class", _JAVA_CLASS)
    _write(t / "classes" / "messages_ü.properties", "greeting=Grüß Gott\n")
    _write(t / "demo-1.0.jar", _ZIP)
    _write(t / "maven-archiver" / "pom.properties", "artifactId=demo\nversion=1.0\n")
    lst = t / "maven-status/maven-compiler-plugin/compile/default-compile/createdFiles.lst"
    _write(lst, "com/example/App.class\n")
    _mkdir(t / "generated-sources" / "annotations")
    return t


def make_gradle_project(root: Path, *, kotlin: bool = False) -> Path:
    """Create a tiny Gradle project in ``root``; return its ``build`` (``.gradle`` too)."""
    if kotlin:
        _write(root / "build.gradle.kts", 'plugins {\n    java\n}\nversion = "1.0"\n')
        _write(root / "settings.gradle.kts", 'rootProject.name = "demo"\n')
    else:
        _write(root / "build.gradle", "plugins {\n    id 'java'\n}\nversion = '1.0'\n")
        _write(root / "settings.gradle", "rootProject.name = 'demo'\n")
    _write(root / "gradlew", '#!/bin/sh\nexec java -jar "$0.jar" "$@"\n', executable=True)
    _write(root / "gradlew.bat", "@rem Gradle startup script\r\n")
    _write(root / "gradle" / "wrapper" / "gradle-wrapper.properties", "distributionUrl=x\n")
    _write(root / "gradle" / "wrapper" / "gradle-wrapper.jar", _ZIP)
    b = root / "build"
    _write(b / "classes" / "java" / "main" / "com" / "example" / "App.class", _JAVA_CLASS)
    _write(b / "libs" / "demo-1.0.jar", _ZIP)
    _write(b / "scripts" / "demo", "#!/bin/sh\necho demo\n", executable=True)
    _write(b / "resources" / "main" / "ñandú.txt", "unicode\n")
    _mkdir(b / "tmp" / "compileJava")
    g = root / ".gradle"
    _write(g / "8.5" / "fileHashes" / "fileHashes.bin", bytes(range(256)))
    _write(g / "8.5" / "fileHashes" / "fileHashes.lock", b"\x00\x01")
    _write(g / "buildOutputCleanup" / "cache.properties", "gradle.version=8.5\n")
    _mkdir(g / "vcs-1")
    return b


def make_gradle_kts_project(root: Path) -> Path:
    """Kotlin DSL variant of :func:`make_gradle_project`."""
    return make_gradle_project(root, kotlin=True)


def make_flutter_project(root: Path) -> Path:
    """Create a tiny Flutter app in ``root``; return its ``.dart_tool`` (``build`` too)."""
    _write(root / "pubspec.yaml", "name: demo\nenvironment:\n  sdk: '>=3.0.0 <4.0.0'\n")
    _write(root / "pubspec.lock", "packages: {}\nsdks:\n  dart: '>=3.0.0 <4.0.0'\n")
    _write(root / "lib" / "main.dart", "void main() {}\n")
    dt = root / ".dart_tool"
    _write(dt / "package_config.json", json.dumps({"configVersion": 2, "packages": []}) + "\n")
    _write(dt / "flutter_build" / "a1b2c3" / "app.dill", bytes(range(256)) * 2)
    _write(dt / "flutter_build" / "dart_plugin_registrant.dart", "// generated\n")
    _write(dt / "version", "3.19.0\n")
    b = root / "build"
    _write(b / "app" / "outputs" / "flutter-apk" / "app-release.apk", _ZIP)
    fw = b / "macos" / "Build" / "Products" / "Release" / "App.framework"
    _write(fw / "Versions" / "A" / "App", b"\xcf\xfa\xed\xfe" + bytes(60), executable=True)
    _write(fw / "Versions" / "A" / "Resources" / "Info.plist", "<plist/>\n")
    _symlink(fw / "Versions" / "Current", "A", is_dir=True)  # real frameworks do this
    _write(b / "flutter_assets" / "fonts" / "Ωmega.ttf", b"\x00\x01\x00\x00" + bytes(32))
    return dt


def make_php_project(root: Path) -> Path:
    """Create a tiny Composer project in ``root``; return its ``vendor``."""
    _write(root / "composer.json", json.dumps({"require": {"monolog/monolog": "^3"}}) + "\n")
    _write(root / "composer.lock", json.dumps({"content-hash": "abc", "packages": []}) + "\n")
    _write(root / "index.php", "<?php\nrequire __DIR__ . '/vendor/autoload.php';\n")
    v = root / "vendor"
    _write(v / "autoload.php", "<?php\nreturn require __DIR__ . '/composer/autoload_real.php';\n")
    _write(v / "composer" / "autoload_real.php", "<?php\n")
    _write(v / "composer" / "installed.json", json.dumps({"packages": []}) + "\n")
    _write(v / "monolog" / "monolog" / "src" / "Monolog" / "Logger.php", "<?php\nclass Logger {}\n")
    _write(v / "monolog" / "monolog" / "README.md", "# Monolog: logging für PHP\n")
    phpunit = "#!/usr/bin/env php\n<?php\n"
    _write(v / "phpunit" / "phpunit" / "phpunit", phpunit, executable=True)
    if not _symlink(v / "bin" / "phpunit", _rel("..", "phpunit", "phpunit", "phpunit")):
        _write(v / "bin" / "phpunit", phpunit, executable=True)
    return v


def make_dotnet_project(root: Path) -> Path:
    """Create a tiny .NET console app in ``root``; return its ``bin`` (``obj`` too)."""
    _write(
        root / "Demo.csproj",
        '<Project Sdk="Microsoft.NET.Sdk">\n  <PropertyGroup>\n'
        "    <OutputType>Exe</OutputType>\n    <TargetFramework>net8.0</TargetFramework>\n"
        "  </PropertyGroup>\n</Project>\n",
    )
    _write(root / "Program.cs", 'System.Console.WriteLine("hi");\n')
    out = root / "bin" / "Debug" / "net8.0"
    _write(out / "Demo.dll", _PE)
    _write(out / "Demo.pdb", b"BSJB" + bytes(64))
    _write(out / "Demo", _ELF, executable=True)  # the POSIX apphost
    _write(out / "Demo.runtimeconfig.json", '{"runtimeOptions": {"tfm": "net8.0"}}\n')
    _write(out / "ja" / "Demo.resources.dll", _PE)
    obj = root / "obj"
    _write(obj / "project.assets.json", json.dumps({"version": 3, "targets": {}}) + "\n")
    _write(obj / "Demo.csproj.nuget.g.props", "<Project />\n")
    _write(obj / "Debug" / "net8.0" / "Demo.dll", _PE)
    _write(obj / "Debug" / "net8.0" / "Démo.AssemblyInfo.cs", "// generated\n")
    _mkdir(obj / "Debug" / "net8.0" / "refint")
    return root / "bin"


def make_cocoapods_project(root: Path) -> Path:
    """Create a tiny CocoaPods iOS project in ``root``; return its ``Pods``."""
    _write(root / "Podfile", "platform :ios, '15.0'\ntarget 'App' do\n  pod 'Alamofire'\nend\n")
    lock = "PODS:\n  - Alamofire (5.8.1)\n\nCOCOAPODS: 1.15.2\n"
    _write(root / "Podfile.lock", lock)
    _write(root / "App" / "AppDelegate.swift", "import UIKit\n")
    p = root / "Pods"
    _write(p / "Manifest.lock", lock)
    _write(p / "Alamofire" / "Source" / "Session.swift", "public class Session {}\n")
    _write(p / "Alamofire" / "Source" / "Alamofire.h", "#import <Foundation/Foundation.h>\n")
    _write(p / "Pods.xcodeproj" / "project.pbxproj", "// !$*UTF8*$!\n{}\n")
    support = p / "Target Support Files" / "Pods-App"
    _write(support / "Pods-App-frameworks.sh", "#!/bin/sh\nset -e\n", executable=True)
    _write(support / "Pods-App-acknowledgements.plist", "<plist>Grüße</plist>\n")
    _write(support / "Pods-App.debug.xcconfig", "FRAMEWORK_SEARCH_PATHS = $(inherited)\n")
    header = p / "Headers" / "Public" / "Alamofire" / "Alamofire.h"  # CocoaPods symlinks these
    if not _symlink(header, _rel("..", "..", "..", "Alamofire", "Source", "Alamofire.h")):
        _write(header, "#import <Foundation/Foundation.h>\n")
    return p


def make_unity_project(root: Path) -> Path:
    """Create a tiny Unity project in ``root``; return its ``Library``."""
    ps = root / "ProjectSettings"
    _write(ps / "ProjectVersion.txt", "m_EditorVersion: 2022.3.20f1\n")
    _write(ps / "ProjectSettings.asset", "%YAML 1.1\n--- !u!129 &1\nPlayerSettings: {}\n")
    _write(root / "Assets" / "Scenes" / "Main.unity", "%YAML 1.1\n")
    _write(root / "Packages" / "manifest.json", json.dumps({"dependencies": {}}) + "\n")
    lib = root / "Library"
    _write(lib / "ArtifactDB", bytes(range(256)) * 8)
    _write(lib / "Artifacts" / "3f" / "3fa1b2c3d4e5f60718293a4b5c6d7e8f", bytes(512))
    _write(lib / "Artifacts" / "Ünïcödé.asset", "unicode\n")
    _write(lib / "ScriptAssemblies" / "Assembly-CSharp.dll", _PE)
    pkg = lib / "PackageCache" / "com.unity.textmeshpro@3.0.6"
    _write(pkg / "package.json", _pkg_json("com.unity.textmeshpro", "3.0.6"))
    _mkdir(lib / "Temp")
    return lib


ECOSYSTEMS: dict[str, tuple[str, Callable[[Path], Path], dict[str, str]]] = {
    "rust": ("rust", make_rust_project, {"target": "build"}),
    "maven": ("maven", make_maven_project, {"target": "build"}),
    "gradle": ("gradle", make_gradle_project, {"build": "build", ".gradle": "cache"}),
    "gradle_kts": ("gradle", make_gradle_kts_project, {"build": "build", ".gradle": "cache"}),
    "flutter": ("flutter", make_flutter_project, {".dart_tool": "dependencies", "build": "build"}),
    "php": ("php", make_php_project, {"vendor": "dependencies"}),
    "dotnet": ("dotnet", make_dotnet_project, {"bin": "build", "obj": "build"}),
    "cocoapods": ("cocoapods", make_cocoapods_project, {"Pods": "dependencies"}),
    "unity": ("unity", make_unity_project, {"Library": "build"}),
}
"""Fixture id -> (detector name, generator, {target folder: kind}). The generator returns
the first target listed; every listed target exists after it runs."""
