<div align="center">

# Dormant

**Put your old projects' `node_modules`, `.venv` and `target/` folders to sleep. They wake up when you come back.**

[![PyPI](https://img.shields.io/pypi/v/dormant.svg)](https://pypi.org/project/dormant/)
[![Python versions](https://img.shields.io/pypi/pyversions/dormant.svg)](https://pypi.org/project/dormant/)
[![CI](https://github.com/OWNER/dormant/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/dormant/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/codecov/c/github/OWNER/dormant)](https://codecov.io/gh/OWNER/dormant)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

![Dormant demo: scan, hibernate, then cd back in and the folder is restored](docs/demo.gif)

</div>

> **Status: pre-alpha.** Dormant is under active development and not yet ready for daily use. Follow the [roadmap](#roadmap) or watch the repo for the v0.1 release.

## Why

Every project you've ever cloned leaves a few hundred megabytes of dependencies behind. Fifty side projects and a few PyTorch venvs later, that's tens of gigabytes of disk holding code you haven't run in months.

Tools like [kondo](https://github.com/tbillington/kondo) and [npkill](https://github.com/voidcosmos/npkill) delete those folders. Then you open the project again, nothing runs, and you're waiting on `npm install` or rebuilding a CUDA venv.

Dormant **hibernates** instead of deleting. It compresses the folder into a local archive, removes the original, and restores it byte-for-byte the next time you `cd` into the project. Your disk gets the space back, and the project still runs the moment you return to it.

## Features

- **Restores itself.** A shell hook brings folders back when you `cd` into a project. No commands to remember.
- **Byte-exact.** Restores are checksum-verified copies of what you had, so they work offline and don't depend on a lockfile.
- **Every major ecosystem.** Node, Python, Rust, Java, Kotlin, Flutter, PHP, .NET, iOS and Unity out of the box. Adding a new one takes a few lines of TOML.
- **Linux, macOS and Windows.** Hooks for zsh, bash, fish and PowerShell, plus a VS Code extension.
- **Safe by default.** Every destructive command previews first. Nothing is deleted until its archive is written, synced and verified, and folders containing git-tracked files are never touched.
- **Knows what to skip.** Projects with a running dev server or recent commits are left alone. Caches like `__pycache__` are deleted, not archived, because tools regenerate them.

## Installation

Dormant needs Python 3.11 or newer.

```bash
# recommended
uv tool install dormant

# or
pipx install dormant
```

Then add the hook for your shell so projects wake up automatically:

<details open>
<summary><b>zsh</b></summary>

```bash
echo 'eval "$(dormant hook zsh)"' >> ~/.zshrc
```
</details>

<details>
<summary><b>bash</b></summary>

```bash
echo 'eval "$(dormant hook bash)"' >> ~/.bashrc
```
</details>

<details>
<summary><b>fish</b></summary>

```fish
echo 'dormant hook fish | source' >> ~/.config/fish/config.fish
```
</details>

<details>
<summary><b>PowerShell</b></summary>

```powershell
Add-Content $PROFILE 'Invoke-Expression (& dormant hook powershell | Out-String)'
```
</details>

The hook adds no noticeable delay to `cd`: it's a few lines of shell that check for a marker file, and Python only starts when a project actually needs restoring.

If you open projects from VS Code instead of a terminal, install the **Dormant** extension from the VS Code Marketplace.

## Quick start

```bash
# See what's taking space, biggest first
dormant scan ~/code

# Preview what would be hibernated in projects untouched for 60+ days
dormant hibernate ~/code --older 60d

# Do it
dormant hibernate ~/code --older 60d --yes

# Later, just cd into a project. Dormant restores it:
cd ~/code/old-nextjs-app
#  ⏾ Waking node_modules (412 MB) ━━━━━━━━━━━━━━━━ 100%

# See what's hibernated and how much space you've saved
dormant status
```

## Commands

| Command | What it does |
| --- | --- |
| `dormant scan [PATH]` | List projects, their reclaimable folders, sizes and last activity |
| `dormant hibernate [PATH]` | Archive and remove folders in stale projects (shows a preview and asks first unless `--yes`) |
| `dormant wake [PATH]` | Restore everything hibernated in a project |
| `dormant status` | Show hibernated projects, archive sizes and total space saved |
| `dormant prune` | Delete archives whose project no longer exists |
| `dormant hook SHELL` | Print the hook snippet for `zsh`, `bash`, `fish` or `powershell` |
| `dormant tui` | Interactive picker (install with `dormant[tui]`) |

Common flags: `--older 60d` sets the staleness threshold, `--json` gives machine-readable output, and `--purge` deletes without archiving and reinstalls from the lockfile on wake. Run `dormant COMMAND --help` for the full list.

## Supported ecosystems

| Ecosystem | Detected by | Folders handled |
| --- | --- | --- |
| Node.js | `package.json` | `node_modules`, `.next`, `dist`, `.turbo` |
| Python | `pyproject.toml`, `requirements.txt` | `.venv`, `venv`, `.tox`, `__pycache__`, `.pytest_cache`, `.mypy_cache` |
| Rust | `Cargo.toml` | `target/` |
| Java (Maven) | `pom.xml` | `target/` |
| Java / Kotlin (Gradle) | `build.gradle`, `build.gradle.kts` | `build/`, `.gradle/` |
| Flutter / Dart | `pubspec.yaml` | `.dart_tool/`, `build/` |
| PHP | `composer.json` | `vendor/` |
| .NET | `*.csproj` | `bin/`, `obj/` |
| iOS (CocoaPods) | `Podfile` | `Pods/` |
| Unity | `ProjectSettings/` | `Library/` |

Missing your stack? See [adding an ecosystem](CONTRIBUTING.md#adding-an-ecosystem). It's usually under 15 lines of TOML.

Go isn't listed because Go keeps dependencies in a global module cache, so its projects have nothing to reclaim.

## How it works

1. **Scan.** Dormant walks your folders, matches projects against the detector registry, and measures each reclaimable folder. It never descends into `node_modules` or similar folders.
2. **Decide.** A project is stale when both its last git commit and its newest source file are older than the threshold. Projects with a running process inside them are skipped.
3. **Hibernate.** The folder is streamed into a zstd-compressed tar archive in your data directory. The archive is synced to disk, checksummed and verified before the original is removed. A small `.dormant/` marker is left in the project and excluded from git through `.git/info/exclude`.
4. **Wake.** When the hook sees the marker, it runs `dormant wake`, which verifies the checksum and extracts the folder exactly as it was.

Archives are stored in:

| OS | Location |
| --- | --- |
| Linux | `~/.local/share/dormant/` |
| macOS | `~/Library/Application Support/dormant/` |
| Windows | `%LOCALAPPDATA%\dormant\` |

## Configuration

Optional settings go in `config.toml` in the same data directory:

```toml
# Projects untouched for this long are considered stale
older_than = "60d"

# Never scan these paths
ignore = ["~/work/client-x", "**/node_modules_backup"]

# Ask before restoring archives larger than this
confirm_restore_above = "1GB"

# zstd compression level (1 = fastest, 19 = smallest)
compression_level = 3
```

To exclude one project, add an empty `.dormantignore` file to its root.

## FAQ

**What if I run `npm install` before Dormant restores the folder?**
Dormant sees the folder already exists, skips the restore, and marks the old archive for `dormant prune`.

**Is it safe?**
Dormant never deletes a folder until its archive is written, synced and checksum-verified. If anything fails partway, the original folder stays where it is. Source files are never touched. See [SECURITY.md](SECURITY.md) to report a problem.

**Can I move an archive to another machine?**
No. Archives are machine-local, and dependency folders often contain platform-specific binaries. On a new machine, use `dormant wake --reinstall`.

**Does it work with pnpm and uv?**
Yes. Both share packages through a global store using hard links, so archiving those folders would free almost nothing. Dormant detects this and purges them instead, reinstalling from the lockfile on wake.

**How is this different from kondo or npkill?**
Those delete folders and you reinstall by hand. Dormant archives them and restores them automatically when you return.

## Roadmap

- [ ] v0.1: scan, hibernate and wake for Node and Python; zsh and bash hooks
- [ ] Remaining 8 ecosystems, Windows hardening, `prune`
- [ ] Textual TUI, fish hook
- [ ] v1.0: PowerShell hook, VS Code extension
- [ ] Scoop, winget and Homebrew packages

## Contributing

Contributions are welcome, and adding an ecosystem is a good first PR. Read [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, then look at issues labelled [`good first issue`](https://github.com/OWNER/dormant/labels/good%20first%20issue).

```bash
git clone https://github.com/OWNER/dormant
cd dormant
uv sync --all-extras
uv run pytest
```

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) © Om Anand
