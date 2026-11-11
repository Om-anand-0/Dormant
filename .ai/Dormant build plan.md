# Dormant: build plan

Nov 10, 2026 · @Om Anand

## What it is and goals

Dormant is a Python CLI that finds dependency and build folders across every project on a machine, archives the ones you haven't touched in a while, and restores them automatically the moment you `cd` back into the project. It works for any language because the default action, compressing a folder, doesn't care what's inside it.

Success for v1.0 means:

- `dormant scan` covers a 200 GB home folder in under 60 seconds on an SSD
- Restoring a hibernated 500 MB node\_modules finishes in under 10 seconds
- Zero data loss: every restore is byte-identical to what was archived, verified by checksum
- Works natively on Linux, macOS and Windows from v1.0, with hooks for zsh, bash, fish and PowerShell plus the VS Code extension
- At least 9 ecosystems supported out of the box

Non-goals: Dormant never touches source files, never runs in the background as a daemon in v1, and never deletes anything without an archive or a lockfile to rebuild from.

## Core concepts

Every reclaimable folder is one of three kinds, and the kind decides what Dormant does with it.

| Kind | Examples | Default action | Restore |
| --- | --- | --- | --- |
| Dependencies | `node_modules`, `.venv`, `Pods/`, `vendor/` | Hibernate | Decompress archive |
| Build output | Rust `target/`, Gradle `build/`, `.next/`, `dist/` | Hibernate (always archived: deleting needs an archive or a lockfile) | Decompress |
| Caches | `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.turbo` | Delete | Never, the tool regenerates them |

Two modes apply to dependencies:

- **Hibernate (default):** zstd-compress the folder into `~/.dormant/archives/`, then delete the original. Restore is exact and works offline.
- **Purge:** delete with no archive, and reinstall from the lockfile on restore. Only allowed when a lockfile exists. Frees the most space, and restore needs network.

A project is stale when the newest of these is older than the threshold (default 60 days): last git commit, newest mtime among non-ignored source files. File access time is ignored because `relatime` and Windows make it unreliable.

## Architecture

&#91;embedded content: Dormant architecture · hibernate and wake paths\]

The hibernate path writes to the archive store and the wake path only reads from it, so a bug in restore can never destroy an archive. Code layout follows the boxes: `scanner.py`, `staleness.py`, `archiver.py`, `restorer.py`, `index.py` (SQLite), `hooks/` (one snippet per shell), and `detectors.toml`.

## Detector registry

Each language is one entry in `detectors.toml`, shipped inside the package. Adding a language needs no Python code, which makes it the obvious first-contributor issue.

```toml
[[detector]]
name = "node"
markers = ["package.json"]

[[detector.target]]
path = "node_modules"
kind = "dependencies"

[[detector.target]]
path = ".next"
kind = "build"

[detector.reinstall]
# first lockfile found wins
"pnpm-lock.yaml" = "pnpm install --frozen-lockfile"
"yarn.lock" = "yarn install --immutable"
"package-lock.json" = "npm ci"
```

Rules the scanner enforces:

- A folder can match several detectors (Django backend plus React frontend). Every match counts.
- Never descend into a target folder. One `node_modules` has hundreds of nested `package.json` files.
- Skip `.git`, symlinks, and anything matched by a global ignore list in `~/.dormant/config.toml`.
- A `.dormantignore` file in a project opts it out entirely.

Ecosystems for v1.0, in build order:

| # | Ecosystem | Marker | Targets | Purge reinstall |
| --- | --- | --- | --- | --- |
| 1 | Node | `package.json` | `node_modules`, `.next`, `dist`, `.turbo` | `npm ci` / `pnpm` / `yarn` by lockfile |
| 2 | Python | `pyproject.toml`, `requirements.txt` | `.venv`, `venv`, `.tox`, caches | `uv sync` or `pip install -r` |
| 3 | Rust | `Cargo.toml` | `target/` | `cargo build` |
| 4 | Java (Maven) | `pom.xml` | `target/` | `mvn compile` |
| 5 | Java/Kotlin (Gradle) | `build.gradle`, `build.gradle.kts` | `build/`, `.gradle/` | `./gradlew build` |
| 6 | Flutter/Dart | `pubspec.yaml` | `.dart_tool/`, `build/` | `flutter pub get` |
| 7 | PHP | `composer.json` | `vendor/` | `composer install` |
| 8 | .NET | `*.csproj` | `bin/`, `obj/` | `dotnet restore` |
| 9 | iOS | `Podfile` | `Pods/` | `pod install` |
| 10 | Unity | `ProjectSettings/` | `Library/` | Unity regenerates on open |

Go is skipped: its modules live in the global `~/go/pkg/mod` cache, so projects hold nothing to reclaim.

## CLI commands

Seven commands cover v1.0. Every destructive command defaults to a dry run preview and asks for confirmation unless `--yes` is passed.

| Command | What it does | Key flags |
| --- | --- | --- |
| `dormant scan [path]` | Lists projects, their targets, sizes and last activity, sorted by reclaimable size | `--older 60d`, `--json`, `--kind deps,build,cache` |
| `dormant hibernate [path]` | Archives and removes targets in stale projects | `--older`, `--purge`, `--level 3` (zstd), `--yes` |
| `dormant wake [path]` | Restores everything hibernated in a project | `--reinstall` (ignore archive, use lockfile) |
| `dormant status` | Shows hibernated projects, archive sizes, total space saved | `--json` |
| `dormant hook <shell>` | Prints the shell snippet for zsh, bash, fish or PowerShell | none |
| `dormant prune` | Deletes archives whose project folder no longer exists | `--older 180d` |
| `dormant tui` | Interactive picker built on Textual, the npkill-style view | none |

The stack: Typer for the CLI, Rich for tables and progress bars, `zstandard` for compression, SQLite for the index, Textual for the TUI. All pure Python wheels, so `pipx install dormant` works on every OS.

## Restore triggers

The shell hooks and the VS Code extension both ship in v1.0. The extension matters most on Windows, where cmd.exe has no way to run code on a directory change. Both call the same `dormant wake --quiet` command, so restore logic lives in one place.

**Shell hook.** `eval "$(dormant hook zsh)"` in `.zshrc` registers a `chpwd` function. On every `cd` it walks up from the new directory looking for a project root, checks a tiny state file (`.dormant/state`) and exits in under 5 ms if nothing is hibernated. The SQLite index is only opened when a restore is needed, so normal `cd` stays instant.

**The per-cd check is pure shell, never Python.** Python startup costs 30 to 50 ms, and about 150 ms once Typer and Rich are imported, which every `cd` would feel. So the hook snippet only tests whether a `.dormant/state` marker exists in the current directory or a parent, using shell built-ins with no subprocess. `dormant wake` is launched only when that marker is found, and the wake code path imports Rich lazily and skips Typer's startup work.

- zsh: `chpwd_functions`
- bash: wrap `PROMPT_COMMAND`, compare `$PWD` to the last value
- fish: `--on-variable PWD`
- PowerShell: override the `prompt` function

**Restore UX.** Show a one-line Rich progress bar ("Waking node\_modules, 412 MB"). If the archive is over 1 GB, ask first, because someone doing `cd` to grep a file doesn't want a 30 second wait.

**VS Code extension.** A \~100 line TypeScript extension that runs `dormant wake` on `onStartupFinished` for the opened workspace. Also catches people who open projects from the editor and never touch a terminal.

**Safety net.** If a user runs `npm install` or `pip install` before the hook fires, the archive is now stale. On wake, Dormant checks whether the target folder already exists and skips the restore, marking the archive for `prune`.

## Milestones

&#91;embedded content: Dormant roadmap · Nov 2026 to Feb 2027\]

v0.1 goes public on Dec 13 with only Node and Python, because a working hibernate-and-wake loop on two ecosystems is already useful and gets early feedback. Each phase is done when:

- [ ] **Scanner + registry:** `dormant scan ~` lists Node and Python projects with correct sizes, and never enters `node_modules`
- [ ] **Hibernate and wake core:** round-trip tests pass for both fixtures on all three OSes, crash tests pass, index tracks every archive
- [ ] **Shell hooks:** zsh and bash wake a hibernated project on `cd`; v0.1 published to PyPI
- [ ] **8 ecosystems, Windows:** all 10 detectors have fixtures, CI green on ubuntu, macos and windows, long paths and file locks handled
- [ ] **Dogfood + TUI:** two weeks on your own machine with zero lost files, GB freed recorded, Textual picker works
- [ ] **VS Code ext + PowerShell:** extension on the Marketplace, PowerShell hook tested on Windows 11
- [ ] **v1.0 launch:** README with VHS GIF, Show HN and Reddit posts go out
- [ ] **Scoop, winget, Homebrew:** one-command install on every platform

## Risks and edge cases

The biggest risk is deleting something a user can't get back, so every item below fails safe.

| Risk | Example | Mitigation |
| --- | --- | --- |
| Crash mid-hibernate | Laptop dies after delete, before archive is flushed | Write archive to `.tmp`, fsync, verify checksum, rename, and only then delete the folder |
| Disk too full to archive | 2 GB free, 5 GB venv | Estimate compressed size at 40% and refuse if free space is below 1.5x that |
| Venv has absolute paths | `.venv/bin/activate` hardcodes the project path | Store the original absolute path; on wake into a moved project, warn and offer `--reinstall` |
| Symlinks and hard links | pnpm `node_modules` is mostly hard links into the store | Count only `st_nlink == 1` bytes as reclaimable; mostly-hard-linked targets default to purge + reinstall, since archiving them frees nothing and restoring loses the dedup. Never follow symlinks out of the project |
| User edited files inside node\_modules | patch-package or a manual hotfix | Hibernate keeps them because it's byte-exact; purge mode warns if `patches/` exists |
| Project is open right now | A dev server is running from it | Skip any project with a process whose cwd is inside it (psutil); on Windows, treat access denied as in use |
| Archive store grows forever | Projects deleted or moved | `dormant prune`, plus `status` shows orphaned archives |
| Marker file shows in git | `.dormant/state` gets committed | Append it to `.git/info/exclude` automatically, which never touches the repo's `.gitignore` |
| Windows long paths | node\_modules paths over 260 characters | Use `\\?\` prefixed paths on Windows; test in CI |
| Windows file locks | Antivirus or a running process holds a file, delete fails halfway | Check for locks first, retry with backoff, and on failure keep the folder whole and discard the archive |
| Windows symlink rights | Creating symlinks needs Developer Mode or admin | Restore links as links when allowed, otherwise warn and fall back to `--reinstall` |
| No hard links on the drive | pnpm project on a FAT32 or exFAT USB drive | Detect the filesystem and skip hibernating pnpm projects there |
| WSL across `/mnt/c` | Scanning Windows files from WSL is about 10x slower | Detect WSL and point users to the native Windows install for Windows-side projects |
| Store path differs per OS | `~/.dormant` makes no sense on Windows | Use `platformdirs`: `~/.local/share/dormant`, `~/Library/Application Support/dormant`, `%LOCALAPPDATA%\dormant` |
| macOS indexing | Spotlight indexes the archive store | Drop a `.metadata_never_index` file in the store folder |

## Testing strategy

The one test that matters most is the round trip: hibernate then wake must produce a byte-identical folder, every time.

1. **Fixture projects.** A `tests/fixtures/` folder with a tiny real project per ecosystem (a Node app with 3 deps, a uv project, a Cargo crate). Fixtures are generated in CI so the repo stays small.
2. **Round-trip tests.** For each fixture: hash every file, hibernate, assert folder gone, wake, hash again, assert equal. Include symlinks, empty dirs, executable bits and unicode filenames.
3. **Scanner tests.** A fake tree with a Django + React monorepo, nested `node_modules`, a `.dormantignore`, and a symlink loop. Assert exact matches and that the scan never enters target folders.
4. **Crash tests.** Kill the process at each step of hibernate (monkeypatch to raise after archive write, after verify, during delete) and assert nothing is lost.
5. **Shell hook tests.** Run zsh and bash in a subprocess, `cd` into a hibernated fixture, assert the folder comes back.
6. **CI matrix.** GitHub Actions on ubuntu, macos and windows, Python 3.10 to 3.13.
7. **Dogfooding.** Run it on your own machine for two weeks before v1.0, and record the real GB freed for the README.

## Launch and distribution

The launch hook is one real number from your own machine: "Dormant freed 47 GB from 83 projects, and they all come back on `cd`."

- **Install paths:** `pipx install dormant` and `uv tool install dormant` on day one; Scoop, winget and a Homebrew tap in the two weeks after launch.
- **README:** a 20 second terminal GIF (record with VHS) showing scan, hibernate, then `cd` triggering a wake. The GIF matters more than the text.
- **Where to post:** r/Python, r/node, r/programming, Hacker News (Show HN, weekday 8:30 pm IST, which is morning in the US), and a LinkedIn and X post with the GIF.
- **Positioning against kondo and npkill:** they delete, Dormant hibernates and brings it back. Say this in the first line of the README.
- **Contributor funnel:** open 5 to 8 `good first issue` tickets for new ecosystems in `detectors.toml` (Elixir, Haskell, Swift Package Manager, Bazel, CMake).

## Open decisions

- [ ] Name: check that `dormant` is free on PyPI and GitHub; fallbacks are `hibernate-deps` or `devsleep`
- [ ] Archive location: one global store in `~/.dormant/archives/`, or next to each project as `.dormant/archive.tar.zst`? Global is easier to prune and back up; local survives moving the project
- [ ] Default staleness threshold: 30, 60 or 90 days
- [ ] Should purge mode exist in v1.0, or wait until hibernate has proven itself?
- [ ] License: MIT or Apache 2.0
