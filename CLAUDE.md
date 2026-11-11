# CLAUDE.md

Instructions for Claude Code working in this repository. Read this file fully before writing any code.

## What Dormant is

Dormant is a cross-platform Python CLI that finds dependency and build folders (`node_modules`, `.venv`, Rust `target/`, and so on) across every project on a machine, archives the ones in stale projects, and restores them automatically when the user `cd`s back into the project or opens it in VS Code.

The full design lives in the build plan: [.ai/Dormant build plan.md](.ai/Dormant%20build%20plan.md) (published copy: https://claude.ai/artifact/ToiPz6n2pNjzxziEve7Ap3). When this file and the plan disagree, this file wins for engineering rules and the plan wins for product scope.

## Safety invariants (never break these)

Dormant deletes folders on people's machines. A single data-loss bug kills the project. Every change must keep these true:

1. **Never delete before the archive is durable.** Order is always: write archive to `<name>.tmp`, `fsync` the file, compute and store a BLAKE2b checksum, re-read and verify the checksum, atomically rename to the final name, `fsync` the parent directory (POSIX; otherwise the rename itself can be lost in a crash), record it in the index, and only then delete the source folder.
2. **Never touch source files.** Only paths listed as targets in `detectors.toml` may be archived or deleted. Any delete outside a resolved target path is a bug. Assert this in code with `path.is_relative_to(target_root)` before every removal. **Never archive or delete a target that contains git-tracked files**: if `git ls-files -- <target>` is non-empty, skip it and report why. Repos commit `dist/`, `build/` and `bin/` more often than you'd think.
3. **Never follow symlinks out of a project.** Use `os.scandir` with `follow_symlinks=False`. Archive symlinks as symlinks.
4. **Never delete without an archive or a lockfile.** Purge mode (no archive) is only allowed when the detector's lockfile exists.
5. **Destructive commands default to a dry run.** `hibernate`, `prune` and purge actions print what they would do and ask for confirmation unless `--yes` is passed.
6. **On any failure mid-operation, leave the source folder whole.** If a delete fails halfway (Windows file locks), restore from the archive just written, then report the error.
7. **Extract archives safely.** Use `tarfile` with `filter="data"` (needs Python 3.11.4+). Reject members with absolute paths or `..` components.
8. **No network calls**, except running the user's own package manager in `--reinstall` / purge mode. No telemetry, ever.

If a requested change conflicts with an invariant, stop and say so instead of writing the code.

## Tech stack

| Concern | Choice |
| --- | --- |
| Python | 3.11 to 3.14 (`requires-python = ">=3.11"`; 3.10 reached end of life in October 2026). Local dev pins 3.12 in `.python-version`. Once the floor is 3.14, drop `zstandard` for stdlib `compression.zstd` / `tarfile` mode `w:zst` |
| Package and env manager | `uv` |
| Build backend | `hatchling`, version from git tags via `hatch-vcs` |
| CLI | `typer` |
| Terminal output | `rich` (import lazily on the `wake` path) |
| TUI | `textual` (optional extra: `pip install dormant[tui]`) |
| Compression | `zstandard`, streamed through `tarfile` |
| Per-OS paths | `platformdirs` |
| Process checks | `psutil` |
| Config | `tomllib` (stdlib) for reading TOML |
| Index | `sqlite3` (stdlib) |
| Lint and format | `ruff` |
| Type checking | `mypy --strict` |
| Tests | `pytest`, `pytest-cov`, `hypothesis` |
| Shell script lint | `shellcheck` |
| Git hooks | `pre-commit` |

Do not add a runtime dependency without a one-line justification in the PR description. The install must stay small.

## Repository layout

Use the `src` layout:

```
dormant/
├── src/dormant/
│   ├── __init__.py
│   ├── __main__.py          # python -m dormant
│   ├── cli.py               # Typer app, thin: parses args, calls core
│   ├── config.py            # user config, platformdirs paths
│   ├── detectors.py         # loads and validates detectors.toml
│   ├── detectors.toml       # one entry per ecosystem, shipped as package data
│   ├── scanner.py           # walks the tree, yields Project objects
│   ├── staleness.py         # last git commit / newest source mtime
│   ├── archiver.py          # hibernate: tar + zstd, checksum, atomic rename
│   ├── restorer.py          # wake: verify, extract, or reinstall
│   ├── index.py             # SQLite index of archives
│   ├── locks.py             # in-use detection, Windows file lock checks
│   ├── platform.py          # OS, WSL and filesystem detection
│   ├── tui.py               # Textual app (optional extra)
│   └── hooks/
│       ├── dormant.zsh
│       ├── dormant.bash
│       ├── dormant.fish
│       └── dormant.ps1
├── tests/
│   ├── conftest.py
│   ├── fixtures/            # generators for tiny real projects, not committed outputs
│   ├── unit/
│   ├── integration/         # round trip, crash, hook tests
│   └── e2e/
├── extensions/vscode/       # TypeScript VS Code extension
├── docs/
├── .github/
├── pyproject.toml
├── README.md
├── CHANGELOG.md
├── CONTRIBUTING.md
├── CODE_OF_CONDUCT.md
├── SECURITY.md
└── LICENSE
```

`cli.py` stays thin. All logic lives in modules that can be unit tested without invoking the CLI.

## Commands

```bash
uv sync --all-extras          # set up the dev environment
uv run pytest                 # run all tests
uv run pytest -m "not slow"   # fast loop
uv run ruff check . --fix
uv run ruff format .
uv run mypy src
uv run pre-commit run --all-files
uv run dormant --help         # run the CLI from source
uv build                      # build sdist and wheel
```

Run `ruff check`, `ruff format`, `mypy` and the fast test suite before declaring any task done. Do not mark work complete with failing checks.

## Coding conventions

- Type hints on every function, `mypy --strict` clean. No `Any` without a comment explaining why.
- Use `pathlib.Path` everywhere. No string path concatenation.
- Use `logging` for diagnostics and Rich for user output. Never `print`. The one exception: `--json` output is written straight to `sys.stdout`, never through Rich.
- Google-style docstrings on public functions.
- Data objects are `@dataclass(frozen=True, slots=True)`.
- Raise specific exceptions from `dormant.errors` (`ArchiveError`, `RestoreError`, `UnsafePathError`, ...). The CLI layer catches them and maps them to exit codes.
- Exit codes: `0` success, `1` operation failed, `2` usage error, `3` aborted by user, `4` partial success (some projects skipped).
- Every command that prints tables also supports `--json` for scripting. JSON output is a stable interface; changing its shape is a breaking change.
- Keep functions under about 50 lines. Prefer small pure functions that tests can hit directly.

## Cross-platform rules

Dormant ships on Linux, macOS and Windows from v1.0. Every feature must work on all three or fail with a clear message.

- Get data and cache directories from `platformdirs`, never hardcode `~/.dormant`.
- On Windows, convert paths to the `\\?\` extended form before file operations so `node_modules` paths over 260 characters work.
- On Windows, deletes can fail because antivirus or a running process holds a file. Check with `locks.py` first, retry with exponential backoff (3 tries), and on final failure apply safety invariant 6.
- Creating symlinks on Windows needs Developer Mode or admin. Detect this and fall back to `--reinstall` with a warning.
- Detect WSL (`/proc/version` contains `microsoft`). If scanning under `/mnt/<drive>`, warn that it is slow and suggest the native Windows install.
- On macOS, create `.metadata_never_index` in the archive store so Spotlight skips it.
- A project is "in use" when a process's cwd or executable is inside it. Processes whose cwd can't be read (`psutil.AccessDenied`) are skipped, not treated as in use: on Windows many of the user's own elevated processes deny the read, which would mark every project in use. The archiver's lock check, retry and rollback are the real guard.

## Shell hooks

The hook runs on every `cd`, so it must cost close to nothing.

- The per-directory check is **pure shell with no subprocess**: walk up from `$PWD` checking for a `.dormant/state` marker using shell built-ins only. Python starts only when a marker is found.
- zsh: `chpwd_functions`. bash: wrap `PROMPT_COMMAND` and compare `$PWD` to the last value. fish: `--on-variable PWD`. PowerShell: wrap the `prompt` function.
- Hooks must not break an existing `PROMPT_COMMAND` or `prompt` function. Chain to the previous one.
- All `.zsh`, `.bash` and `.fish` files pass `shellcheck` where it applies.
- `dormant hook <shell>` prints the snippet; users install it with `eval "$(dormant hook zsh)"` or the PowerShell equivalent.
- The `wake` entry point avoids importing Typer and Textual, and imports Rich only after deciding there is work to do. Implement this in `__main__.py` / the console-script entry: if `sys.argv[1] == "wake"`, dispatch to `restorer` directly before `cli` (and therefore Typer) is ever imported. Going through the Typer app imports Typer regardless.

## Detector registry

- Ecosystems are defined in `src/dormant/detectors.toml`, not in Python. Adding a language must never need a code change.
- Validate the file at load time and fail with the entry name and field on any error.
- Each target has a `kind`: `dependencies`, `build` or `cache`. Caches are deleted and never restored. Build output is always archived (never deleted outright, per invariant 4).
- **Count only reclaimable bytes.** Files with `st_nlink > 1` (pnpm's store, uv's hard-linked venvs on Linux) free nothing when deleted, and tar restores them as plain files, which loses the dedup and uses more disk than before. The scanner reports size from `st_nlink == 1` files only. If a target is mostly hard links, hibernating costs disk instead of saving it, so default it to purge + reinstall (lockfile required, per invariant 4) or skip it with a message.
- The scanner records every detector that matches a folder (monorepos match several) and never descends into a target folder.
- Every new detector needs a fixture generator in `tests/fixtures/` and a round-trip test.

## Testing

- **Round trip is the core test.** For every fixture: hash every file, hibernate, assert the target is gone, wake, hash again, assert identical. Cover symlinks, empty dirs, executable bits, unicode filenames and paths over 260 characters.
- **Crash tests.** Monkeypatch failures at each step of hibernate and wake. Assert nothing is lost and no partial state is left in the index.
- **Property tests.** Use `hypothesis` to generate random directory trees for the round trip.
- **Scanner tests.** Cover a Django + React monorepo, nested `node_modules`, `.dormantignore`, and a symlink loop.
- **Hook tests.** Spawn real zsh, bash and PowerShell processes in CI, `cd` into a hibernated fixture, and assert the folder comes back.
- Fixtures are generated at test time into `tmp_path`. Never commit `node_modules` or venvs.
- Coverage floor: 90% for `archiver.py`, `restorer.py` and `index.py`; 80% overall. CI fails below that.
- Mark tests slower than 2 seconds with `@pytest.mark.slow`.

## Git and versioning

- **Conventional Commits** for every commit: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`. Use `feat(detectors): add elixir` style scopes.
- **Semantic Versioning.** Before 1.0.0, minor bumps may break things. After 1.0.0, a change to CLI flags, `--json` output shape, `detectors.toml` schema or archive format is a major bump.
- **Keep a Changelog** format in `CHANGELOG.md`. Every user-visible PR adds a line under `## [Unreleased]`.
- The archive format carries a version byte in its metadata. Newer Dormant must always read older archives.
- `main` is always releasable. Work happens on short-lived branches, merged through PRs with green CI.

## CI and release (GitHub Actions)

Create these workflows in `.github/workflows/`:

- `ci.yml`: on push and PR. Matrix of `ubuntu-latest`, `macos-latest`, `windows-latest` × Python 3.11, 3.12, 3.13, 3.14. Steps: ruff, mypy, pytest with coverage, shellcheck (Linux only), hook tests.
- `release.yml`: on a `v*` tag. Build with `uv build`, publish to PyPI using **Trusted Publishing** (OIDC, no API token stored), attach build attestations, and create a GitHub Release with the changelog section as notes.
- `codeql.yml`: GitHub CodeQL for Python, weekly and on PR.
- `.github/dependabot.yml`: weekly updates for `pip` and `github-actions`.

Pin third-party actions to a full commit SHA, not a tag.

## Community and repo files to create

Create these early so the repo looks maintained from the first public commit:

- `LICENSE`: MIT, copyright Om Anand.
- `CONTRIBUTING.md`: dev setup with uv, how to run checks, commit convention, and a step-by-step "add a new ecosystem" guide.
- `CODE_OF_CONDUCT.md`: Contributor Covenant 2.1.
- `SECURITY.md`: how to report vulnerabilities privately through GitHub Security Advisories, and the supported versions table.
- `CHANGELOG.md`: Keep a Changelog skeleton with `## [Unreleased]`.
- `.github/ISSUE_TEMPLATE/`: issue forms (YAML) for bug report, feature request, and new ecosystem request. The bug form asks for OS, shell, Python version, `dormant --version` and `dormant scan --json` output.
- `.github/PULL_REQUEST_TEMPLATE.md`: checklist for tests, changelog entry, docs, and cross-platform impact.
- `.github/CODEOWNERS`.
- `.pre-commit-config.yaml`: ruff, ruff-format, mypy, shellcheck, end-of-file-fixer, trailing-whitespace, check-toml.
- `.editorconfig`.

## Build order

Follow [.ai/phases.md](.ai/phases.md). Each phase has a "Done when" list; don't start the next phase until every item in it is checked. Summary:

0. Skeleton: `src` layout, pyproject, minimal CI. `dormant --version` works on all three OSes.
1. Archive core: `archiver.py`, `restorer.py`, `index.py`; round-trip, crash and property tests. No CLI yet.
2. Detection: Node and Python detectors, scanner, staleness, locks; `dormant scan`.
3. Commands: `hibernate`, `wake` (fast path), `status`, purge mode.
4. zsh and bash hooks, community files, release workflow. Tag `v0.1.0`.
5. Remaining 8 detectors, Windows hardening, `prune`. Tag `v0.2.0`.
6. Dogfood, Textual TUI, fish hook. Tag `v0.3.0`.
7. PowerShell hook and VS Code extension. Tag `v1.0.0`.

## Do not

- Do not use `shutil.rmtree` directly. Go through `archiver.safe_remove()`, which enforces invariants 2, 3 and 6.
- Do not shell out with `shell=True`. Pass argument lists to `subprocess.run`.
- Do not catch bare `Exception` except at the CLI boundary.
- Do not add features outside the current milestone without asking.
- Do not write to a user's project folder except the `.dormant/` marker directory, and add that to `.git/info/exclude`, never to `.gitignore`.
