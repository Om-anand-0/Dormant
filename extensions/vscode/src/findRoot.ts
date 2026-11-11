import * as path from 'node:path';

/** Walk up from `start` to the first directory holding `.dormant/state`. Pure: `exists` is injected. */
export function findProjectRoot(start: string, exists: (p: string) => boolean): string | undefined {
  let dir = path.resolve(start);
  for (;;) {
    if (exists(path.join(dir, '.dormant', 'state'))) {
      return dir;
    }
    const parent = path.dirname(dir);
    if (parent === dir) {
      return undefined;
    }
    dir = parent;
  }
}
