# Contributing to Dormant

Thanks for helping out. Dormant deletes folders on people's machines, so the bar for safety is high, but the process is simple. Adding an ecosystem is a good first PR and needs no Python.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). To report a security problem, see [SECURITY.md](SECURITY.md) instead of opening an issue.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and git. uv installs the right Python for you.

```bash
git clone https://github.com/Om-anand-0/Dormant
cd Dormant
uv sync --all-extras          # create .venv with runtime, dev and optional deps
uv run pre-commit install     # run the checks on every commit
uv run dormant --help         # run the CLI from source
```

## Running checks

```bash
uv run pytest                 # all tests
uv run pytest -m "not slow"   # fast loop
uv run ruff check . --fix     # lint
uv run ruff format .          # format
uv run mypy src               # strict type check
uv run pre-commit run --all-files
uv build                      # build sdist and wheel
```

Run `ruff check`, `ruff format`, `mypy` and the fast tests before opening a PR. CI runs the full suite on Linux, macOS and Windows with Python 3.11 to 3.14, and fails if coverage drops below 80% overall or 90% for `archiver.py`, `restorer.py` and `index.py`. Shell hooks are checked with `shellcheck`.

Mark tests slower than 2 seconds with `@pytest.mark.slow`.

## Safety invariants

Every change must keep the [safety invariants in CLAUDE.md](CLAUDE.md#safety-invariants-never-break-these) true. In short:

1. Never delete before the archive is written, synced, checksummed, verified and renamed into place.
2. Only delete paths listed as targets in `detectors.toml`, and never a target that contains git-tracked files.
3. Never follow symlinks out of a project.
4. Never delete without an archive, or a lockfile in purge mode.
5. Destructive commands preview and ask unless `--yes` is passed.
6. On any failure mid-operation, leave the source folder whole.
7. Extract archives with `tarfile` `filter="data"` and reject absolute or `..` paths.
8. No network calls except the user's own package manager on reinstall. No telemetry.

If a change you want conflicts with one of these, open an issue to discuss it first.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/). Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`. Add a scope when it helps:

```text
feat(detectors): add elixir
fix(restorer): keep the archive when extraction fails
docs: explain purge mode in the README
test(archiver): crash during parent fsync
ci: add Python 3.14 to the matrix
refactor(scanner): split size accounting into its own function
```

Breaking changes get a `!` after the type or scope (`feat(cli)!: rename --older to --stale`) and a `BREAKING CHANGE:` footer. After 1.0, any change to CLI flags, `--json` output shape, the `detectors.toml` schema or the archive format is breaking.

## Branches and pull requests

1. Fork the repo and create a short-lived branch from `main` (`feat/elixir-detector`, `fix/windows-lock-retry`).
2. Keep each PR to one change. Add or update tests with it.
3. Add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice, in the right section (`Added`, `Changed`, `Fixed`, `Removed`, ...).
4. Open the PR against `main` and fill in the template checklist, including cross-platform impact.
5. CI must be green before merge. `main` is always releasable.

New runtime dependencies need a one-line justification in the PR description. The install should stay small.

## Adding an ecosystem

Ecosystems are defined in `src/dormant/detectors.toml`. Adding one should never need a Python change; if it does, the registry is missing a feature, so open an issue.

### 1. Add a `[[detector]]` entry

Append an entry to `src/dormant/detectors.toml`:

```toml
[[detector]]
name = "elixir"
# A folder is a project when any of these files exists in it.
markers = ["mix.exs"]

# Folders Dormant may archive or delete, relative to the project root.
# kind is one of:
#   "dependencies"  archived and restored (e.g. node_modules, .venv)
#   "build"         build output; archived, or deleted when small
#   "cache"         deleted and never restored; tools regenerate it
[[detector.target]]
path = "deps"
kind = "dependencies"

[[detector.target]]
path = "_build"
kind = "build"

[[detector.target]]
path = ".elixir_ls"
kind = "cache"

# Used by purge mode and `dormant wake --reinstall`.
# Keys are lockfiles, values are the command to run. The first lockfile found wins,
# so list the most specific first. Purge mode is refused when no lockfile exists.
[detector.reinstall]
"mix.lock" = "mix deps.get"
```

Rules:

- `markers` are file or folder names in the project root. Globs work (`"*.csproj"`), and a trailing `/` means the marker must be a folder (`"ProjectSettings/"`).
- `path` is a single folder name directly inside the project root, not a nested path.
- Only list folders that are always safe to recreate. Never list a folder that can hold source files. Dormant also skips any target containing git-tracked files, but don't rely on that.
- Pick the `kind` by what happens on wake: dependencies come back, caches don't.
- The loader validates the file and names the entry and field on any error. Run `uv run dormant scan` on a real project to check it is picked up.

### 2. Add a fixture generator

Add a generator to `tests/fixtures/trees.py` next to `make_node_project` and `make_python_project`. It builds a tiny but realistic project into `tmp_path` at test time: the marker file, a lockfile, and a small target folder with a few files, a nested directory, and an executable bit where the ecosystem uses one. Return the target path. Never commit generated folders.

### 3. Add a round-trip test

Add a test under `tests/integration/` that uses the generator and checks the full cycle:

1. `before = snapshot(target)`
2. hibernate the target
3. assert the target is gone
4. wake the project
5. assert `snapshot(target) == before`

Also add a scanner test asserting the detector matches the fixture and reports the expected targets and kinds.

### 4. Update the docs

- Add a row to the [Supported ecosystems](README.md#supported-ecosystems) table in the README.
- Add a line under `## [Unreleased]` → `### Added` in [CHANGELOG.md](CHANGELOG.md), e.g. `- Elixir detector (mix.exs: deps, _build).`

Then open a PR titled `feat(detectors): add <ecosystem>`.
