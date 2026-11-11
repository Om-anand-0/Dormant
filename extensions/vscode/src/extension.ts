import { execFile } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { findProjectRoot } from './findRoot';

const INSTALL_URL = 'https://github.com/Om-anand-0/Dormant#installation';

let output: vscode.OutputChannel;
let warnedMissing = false;
const inFlight = new Set<string>();

export function activate(context: vscode.ExtensionContext): void {
  output = vscode.window.createOutputChannel('Dormant');
  context.subscriptions.push(
    output,
    vscode.commands.registerCommand('dormant.wake', async () => {
      const folders = vscode.workspace.workspaceFolders ?? [];
      const found = await Promise.all(folders.map((f) => wakeFolder(f)));
      if (!found.includes(true)) {
        void vscode.window.showInformationMessage('Dormant: nothing hibernated in this workspace.');
      }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders((e) => autoWake(e.added)),
  );
  autoWake(vscode.workspace.workspaceFolders ?? []);
}

export function deactivate(): void {}

function autoWake(folders: readonly vscode.WorkspaceFolder[]): void {
  if (vscode.workspace.getConfiguration('dormant').get<boolean>('autoWake', true)) {
    for (const f of folders) {
      void wakeFolder(f);
    }
  }
}

/** Wakes the project containing `folder`. Returns false if no `.dormant/state` marker was found. */
async function wakeFolder(folder: vscode.WorkspaceFolder): Promise<boolean> {
  if (folder.uri.scheme !== 'file') {
    return false;
  }
  const root = findProjectRoot(folder.uri.fsPath, fs.existsSync);
  if (!root) {
    return false;
  }
  if (inFlight.has(root)) {
    return true;
  }
  inFlight.add(root);
  try {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Dormant: waking ${path.basename(root)}…` },
      () => runWake(root),
    );
  } finally {
    inFlight.delete(root);
  }
  return true;
}

function runWake(root: string): Promise<void> {
  const exe = vscode.workspace.getConfiguration('dormant').get<string>('executablePath') || 'dormant';
  output.appendLine(`$ ${exe} wake --quiet ${root}`);
  return new Promise((resolve) => {
    execFile(exe, ['wake', '--quiet', root], { cwd: root, windowsHide: true }, (err, stdout, stderr) => {
      if (stdout) output.append(stdout);
      if (stderr) output.append(stderr);
      resolve();
      if (!err) {
        vscode.window.setStatusBarMessage(`Dormant: woke ${path.basename(root)}`, 5000);
      } else if ((err as NodeJS.ErrnoException).code === 'ENOENT') {
        output.appendLine(`Executable not found: ${exe}`);
        if (!warnedMissing) {
          warnedMissing = true;
          void vscode.window
            .showWarningMessage(`Dormant: '${exe}' was not found. Install the Dormant CLI or set dormant.executablePath.`, 'Install instructions')
            .then((pick) => pick && vscode.env.openExternal(vscode.Uri.parse(INSTALL_URL)));
        }
      } else {
        output.appendLine(`Exited with code ${String(err.code ?? err.message)}`);
        void vscode.window
          .showErrorMessage(`Dormant: wake failed for ${path.basename(root)}. ${stderr.trim() || err.message}`, 'Show output')
          .then((pick) => pick && output.show());
      }
    });
  });
}
