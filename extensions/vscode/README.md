# Dormant for VS Code

Restores hibernated dependency and build folders (`node_modules`, `.venv`, `target/`, ...) when you open a project in VS Code.

On startup, and whenever a folder is added to the workspace, the extension looks for a `.dormant/state` marker in each workspace folder or any parent. If it finds one, it runs `dormant wake --quiet <project root>`. Folders without a marker cost a few file existence checks and nothing else.

## Requirements

The [Dormant CLI](https://github.com/Om-anand-0/Dormant#installation) must be installed and on your `PATH` (or set `dormant.executablePath`). The extension contains no restore logic of its own.

The extension is disabled in untrusted workspaces, because it runs an executable in the workspace folder.

## Settings

| Setting | Default | Description |
| --- | --- | --- |
| `dormant.executablePath` | `"dormant"` | Path to the `dormant` executable. |
| `dormant.autoWake` | `true` | Wake hibernated projects automatically when they are opened. |

## Commands

- **Dormant: Wake this workspace** runs the wake manually for every workspace folder.

All CLI output goes to the **Dormant** output channel.
