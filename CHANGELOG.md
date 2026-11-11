# Changelog

All notable changes to Dormant are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `dormant scan` lists projects, their reclaimable folders, sizes and last activity, with `--json` output.
- `dormant hibernate` archives dependency and build folders in stale projects into checksum-verified zstd archives, previewing first unless `--yes` is passed.
- `dormant wake` restores hibernated folders byte-for-byte, or reinstalls from the lockfile with `--reinstall`.
- `dormant tui` is an interactive picker (`pip install 'dormant[tui]'`): select folders with space, confirm, and hibernate them.
- `dormant status` shows hibernated projects, archive sizes and total space saved.
- zsh and bash hooks (`dormant hook zsh|bash`) that wake a project when you `cd` into it.
- Node.js and Python detectors.

[Unreleased]: https://github.com/Om-anand-0/Dormant/commits/main
