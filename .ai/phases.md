# Dormant: phased build plan

Execution order for the [build plan](Dormant%20build%20plan.md). Engineering rules live in [CLAUDE.md](../CLAUDE.md). Each phase ends in a working, tested state on Linux, macOS and Windows. Don't start a phase until the previous one's "Done when" list is all checked.

Ordering principle: the riskiest code (archive and delete) gets built and tested first, before the CLI around it. Repo polish (community files, CodeQL, release automation) waits until just before the first public release.

Dates are estimates, working back from the v0.1 date in the build plan (Dec 13, 2026).

| Phase | Name | Target | Tag |
| --- | --- | --- | --- |
| 0 | Skeleton | Nov 10 – 13 | |
| 1 | Archive core | Nov 14 – 24 | |
| 2 | Detection and scan | Nov 25 – Dec 1 | |
| 3 | Commands | Dec 2 – 8 | |
| 4 | Hooks and first release | Dec 9 – 13 | `v0.1.0` |
| 5 | All ecosystems, Windows hardening, prune | Dec 14 – Jan 3 | `v0.2.0` |
| 6 | Dogfood, TUI, fish | Jan 4 – 17 | `v0.3.0` |
| 7 | PowerShell, VS Code, launch | Jan 18 – Feb 7 | `v1.0.0` |
| 8 | Package managers | after 1.0 | |

---

## Phase 0: Skeleton

**Goal:** an empty `dormant --version` that installs and runs on all three OSes, with checks in CI.

Build:
- `src/dormant/` layout: `__init__.py`, `__main__.py`, `cli.py` (Typer app with `--version` only), `errors.py` (`DormantError` base, `ArchiveError`, `RestoreError`, `UnsafePathError`).
- Delete root `main.py`.
- `pyproject.toml`: hatchling + hatch-vcs, `requires-python = ">=3.11"`, console script `dormant = "dormant.__main__:main"`, runtime deps `typer`, `rich`, `zstandard`, `platformdirs`, `psutil`; dev group `ruff`, `mypy`, `pytest`, `pytest-cov`, `hypothesis`. Configure ruff, `mypy --strict` and pytest markers (`slow`) in the same file.
- `LICENSE` (MIT, Om Anand).
- `.github/workflows/ci.yml`: ubuntu, macos, windows × Python 3.11 and 3.14 (add the middle versions in Phase 4). Steps: `uv sync`, ruff check, ruff format --check, mypy, pytest. Actions pinned to a commit SHA.

Done when:
- [ ] `uv run dormant --version` prints the version from the git tag
- [ ] CI green on all six jobs

## Phase 1: Archive core

**Goal:** hibernate and wake a single folder safely, with no CLI on top yet. This phase is where data loss would come from, so it carries most of the testing.

Build:
- `platform.py`: OS, WSL and filesystem detection; extended `\\?\` path helper for Windows.
- `config.py`: platformdirs data dir, archive store path, `.metadata_never_index` on macOS.
- `index.py`: SQLite schema (archive id, project path, target path, kind, checksum, sizes, created, format version, state). Migrations from day one, starting at version 1.
- `archiver.py`:
  - `hibernate(target, project_root)`: stream tar → zstd into `<name>.tmp` → `fsync` file → BLAKE2b → re-read and verify → atomic rename → `fsync` parent dir → index row → `safe_remove`.
  - `safe_remove(path, target_root)`: the only place that deletes. Asserts `is_relative_to`, refuses symlinked roots, no symlink following, Windows lock retry (3 tries, exponential backoff), restores from the archive on partial failure (invariant 6).
  - Archive metadata carries a format version byte.
  - Free-space check: refuse if free space is under 1.5× the estimated archive size (40% of source).
- `restorer.py`: verify checksum → extract with `filter="data"` into a temp sibling → atomic rename into place → mark the index row. Skip if the target already exists and mark the archive for prune.
- `tests/fixtures/`: generators for a tiny Node project and a tiny Python venv project, plus edge cases: symlinks, empty dirs, executable bits, unicode names, paths over 260 characters.

Done when:
- [ ] Round-trip test passes for every fixture: hash all files, hibernate, assert gone, wake, hash, assert identical
- [ ] Hypothesis round trip on random trees
- [ ] Crash tests: failure injected at every step of hibernate and wake; source or archive always intact, index never half-written
- [ ] Coverage ≥ 90% on `archiver.py`, `restorer.py`, `index.py`
- [ ] Green on all three OSes

## Phase 2: Detection and scan

**Goal:** `dormant scan ~` lists Node and Python projects with correct reclaimable sizes.

Build:
- `detectors.toml` with `node` and `python`, including `[detector.reinstall]` lockfile commands.
- `detectors.py`: load from package data, validate, errors name the entry and field.
- `scanner.py`: `os.scandir` walk, `follow_symlinks=False`, never descends into a target or `.git`, honours `.dormantignore` and config `ignore`, records every matching detector per folder.
- Size accounting counts only `st_nlink == 1` files; flag mostly-hard-linked targets (pnpm, uv) for purge.
- Git-tracked check: targets with tracked files are reported as skipped.
- `staleness.py`: last git commit time and newest source mtime, ignoring target folders.
- `locks.py`: projects with a process whose cwd is inside them are "in use"; `psutil.AccessDenied` counts as in use.
- `dormant scan [PATH] --older --kind --json`.

Done when:
- [ ] Scanner tests: Django + React monorepo, nested `node_modules`, `.dormantignore`, symlink loop, committed `dist/`, pnpm-style hard links
- [ ] `--json` shape written down in `docs/json.md` (it's a stable interface from here on)
- [ ] Scan of a real home folder finishes and the numbers match `du` (within hard-link accounting)

## Phase 3: Commands

**Goal:** a full manual loop: scan, hibernate, status, wake.

Build:
- `dormant hibernate [PATH] --older --purge --level --yes`: preview table, confirmation prompt, then act. Caches are deleted, build output under 200 MB is deleted, everything else archived. Hard-linked targets go to purge when a lockfile exists, otherwise skipped.
- Purge mode: delete without archive, store the reinstall command; `wake --reinstall` runs it as an argument list (no `shell=True`).
- `.dormant/state` marker in the project; add `.dormant/` to `.git/info/exclude`.
- `dormant wake [PATH] --reinstall --quiet`, with the fast path: `__main__` dispatches `wake` before importing `cli`; Rich imported only when there is work. Ask before restoring anything over `confirm_restore_above`.
- `dormant status --json`.
- Exit codes 0–4 mapped at the CLI boundary.

Done when:
- [ ] Integration test runs scan → hibernate → status → wake on a fixture tree via the CLI
- [ ] `python -X importtime -m dormant wake` on a project with nothing to do shows no Typer or Rich import
- [ ] Ctrl-C during hibernate leaves source intact (exit 3)

## Phase 4: Hooks and first release (`v0.1.0`)

**Goal:** `cd` into a hibernated project brings it back, and v0.1 is on PyPI.

Build:
- `hooks/dormant.zsh` (`chpwd_functions`) and `hooks/dormant.bash` (wrap `PROMPT_COMMAND`, chain the existing one). Pure-shell walk up for `.dormant/state`, no subprocess on the miss path.
- `dormant hook zsh|bash`.
- Hook tests spawn real zsh and bash, `cd` into a hibernated fixture, assert the folder is back.
- CI: full Python matrix (3.11–3.14), shellcheck on Linux, coverage floors enforced.
- Repo files: `README` placeholders filled, `CHANGELOG.md`, `CONTRIBUTING.md` (with "add an ecosystem"), `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue forms, PR template, `.pre-commit-config.yaml`, `.editorconfig`.
- `release.yml`: `uv build`, PyPI Trusted Publishing, attestations, GitHub Release from the changelog.
- Confirm the `dormant` name is free on PyPI (do this in Phase 0 if possible).

Done when:
- [ ] Hook adds no measurable delay to `cd` outside hibernated projects
- [ ] `uv tool install dormant` from PyPI works on all three OSes
- [ ] Tag `v0.1.0`

## Phase 5: All ecosystems, Windows hardening, prune (`v0.2.0`)

Build:
- Remaining 8 detectors (Rust, Maven, Gradle, Flutter, PHP, .NET, CocoaPods, Unity), each with a fixture generator and round-trip test. No Python changes should be needed; if one is, the registry is missing a feature.
- Windows: long paths everywhere, lock checks before delete, symlink privilege detection with `--reinstall` fallback, FAT32/exFAT detection (skip hard-link projects).
- WSL: warn when scanning `/mnt/<drive>`.
- `dormant prune --older`: archives whose project is gone or that were marked stale on wake.
- `codeql.yml`, `dependabot.yml`.

Done when:
- [ ] All 10 detectors pass round trip on all three OSes
- [ ] Windows crash test with a held file lock leaves the source whole

## Phase 6: Dogfood, TUI, fish (`v0.3.0`)

Build:
- Two weeks running Dormant on your own machines. Log GB freed and every surprise.
- `tui.py` (Textual, `dormant[tui]` extra): npkill-style picker over scan results.
- `hooks/dormant.fish` (`--on-variable PWD`).

Done when:
- [ ] Two weeks, zero lost files, GB freed recorded
- [ ] TUI can select and hibernate; fish hook test passes

## Phase 7: PowerShell, VS Code, launch (`v1.0.0`)

Build:
- `hooks/dormant.ps1`: wrap `prompt`, chain the existing one. Hook test on Windows 11.
- `extensions/vscode/`: ~100-line TypeScript extension, runs `dormant wake --quiet` on `onStartupFinished` for each workspace folder. Publish to the Marketplace.
- README demo GIF (VHS), final docs pass.
- Freeze `--json` shape, `detectors.toml` schema and archive format: changes after this are major bumps.

Done when:
- [ ] Every v1.0 success metric in the build plan is measured and met (scan 200 GB < 60 s, restore 500 MB < 10 s)
- [ ] Tag `v1.0.0`; Show HN and Reddit posts go out

## Phase 8: Package managers (after 1.0)

Scoop, winget and Homebrew manifests, each installing the PyPI release.

## Cut first if time runs short

1. Textual TUI and fish hook (move after 1.0)
2. Unity and CocoaPods detectors
3. CodeQL and attestations
