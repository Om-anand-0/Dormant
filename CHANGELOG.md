# Changelog

All notable changes to Dormant are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `dormant scan` hides folders smaller than `min_archive_size` from its table and says how many it hid; `--all` lists them. `--json` output still includes every folder.

### Fixed

- `dormant scan` no longer walks into a `node_modules`, `.venv`, `vendor` or similar folder whose parent isn't a recognised project, where it listed hundreds of packages as projects.

## [0.1.0] - 2026-11-11

### Added

- `dormant scan` lists projects, their reclaimable folders, sizes and last activity, with `--json` output.
- `dormant hibernate` archives dependency and build folders in stale projects into checksum-verified zstd archives, previewing and asking first unless `--yes` is passed.
- `dormant hibernate --purge` deletes dependency folders that have a lockfile and reinstalls them on wake; mostly hard-linked folders (pnpm, uv) are purged by default.
- `dormant wake` restores hibernated folders byte-for-byte, or reinstalls from the lockfile with `--reinstall`.
- `dormant status` shows hibernated projects, archive sizes and total space saved.
- `dormant prune` deletes archives whose project is gone or whose folder was recreated; it never deletes the only copy of hibernated data.
- `dormant tui` is an interactive picker (`pip install 'dormant[tui]'`): select folders with space, confirm, and hibernate them.
- Shell hooks for zsh, bash, fish and PowerShell (`dormant hook <shell>`) that wake a project when you `cd` into it.
- A VS Code extension (in `extensions/vscode/`) that wakes a workspace when it opens.
- Detectors for Node.js, Python, Rust, Maven, Gradle, Flutter, PHP, .NET, CocoaPods and Unity.
- `config.toml` settings: `older_than`, `ignore`, `confirm_restore_above`, `compression_level` and `min_archive_size` (folders under 1 MB are skipped by default, since their archive would not save space).

### Security

- Git runs with the repository's `core.fsmonitor` and signature-verification programs overridden, so a cloned repo's config can't make Dormant run a program.
- The `.dormant/` marker and `.git/info/exclude` are never written through a symlink or junction.

[Unreleased]: https://github.com/Om-anand-0/Dormant/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Om-anand-0/Dormant/releases/tag/v0.1.0
