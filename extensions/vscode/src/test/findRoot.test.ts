import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as path from 'node:path';
import { findProjectRoot } from '../findRoot';

const root = path.resolve('/projects/app');
const marker = path.join(root, '.dormant', 'state');
const exists = (p: string): boolean => p === marker;

test('finds the marker in the start directory', () => {
  assert.equal(findProjectRoot(root, exists), root);
});

test('walks up from a nested folder', () => {
  assert.equal(findProjectRoot(path.join(root, 'src', 'lib'), exists), root);
});

test('returns undefined when no marker exists up to the filesystem root', () => {
  assert.equal(findProjectRoot(path.resolve('/elsewhere/deep'), exists), undefined);
});
