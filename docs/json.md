# JSON output

`--json` output is a stable interface. Changing its shape after 1.0 is a breaking change (see SemVer rules in `CLAUDE.md`). New keys may be added; existing keys keep their name, type and meaning.

JSON goes to stdout. Warnings and errors go to stderr, so `dormant scan --json | jq` is safe.

## `dormant scan --json`

A list of projects, sorted by `reclaimable_bytes`, largest first. A project appears only if it has at least one target of the requested `--kind`; with `--older`, only projects idle at least that long appear.

```json
[
  {
    "root": "/home/me/code/shop",
    "detectors": ["node", "python"],
    "last_activity": "2026-08-02T14:31:07Z",
    "in_use": false,
    "reclaimable_bytes": 734003200,
    "targets": [
      {
        "path": "/home/me/code/shop/node_modules",
        "kind": "dependencies",
        "detector": "node",
        "reclaimable_bytes": 734003200,
        "total_bytes": 734003200,
        "hardlinked": false,
        "tracked": false
      }
    ]
  }
]
```

### Project

| Key | Type | Meaning |
| --- | --- | --- |
| `root` | string | Absolute path of the project folder, in the platform's native form. |
| `detectors` | list of strings | Names of every detector in `detectors.toml` that matched this folder (a monorepo can match several). |
| `last_activity` | string or `null` | Newest of the last git commit touching the folder and the newest file mtime outside target folders, as ISO 8601 UTC ending in `Z`. `null` if neither exists. |
| `in_use` | boolean | A running process has its working directory or executable inside the project. |
| `reclaimable_bytes` | integer | Sum of the targets' `reclaimable_bytes`, excluding `tracked` targets (Dormant never touches those). |
| `targets` | list | Target folders that exist, filtered by `--kind`. |

### Target

| Key | Type | Meaning |
| --- | --- | --- |
| `path` | string | Absolute path of the folder. |
| `kind` | string | `dependencies`, `build` or `cache`. |
| `detector` | string | Detector that claimed the folder. When two detectors name the same folder (Rust and Maven both use `target`), the first in `detectors.toml` wins. |
| `reclaimable_bytes` | integer | Bytes of regular files with exactly one hard link: what deleting the folder frees. |
| `total_bytes` | integer | Bytes of all regular files. Symlinks are not followed or counted. |
| `hardlinked` | boolean | `total_bytes > 0` and less than half of it is reclaimable (pnpm store, uv on some platforms). Hibernating such a folder costs disk rather than saving it. |
| `tracked` | boolean | The folder contains git-tracked files (a committed `dist/`). Reported only; never archived or deleted. Also `true` when git fails inside a repository, since the answer is unknown. |

## Exit codes

`0` success, `1` operation failed, `2` usage error (bad flag value or missing path).
