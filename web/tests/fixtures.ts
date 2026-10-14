/**
 * Loader for the canonical fixtures produced by the contract agent
 * (`protocol/fixtures/`). The web package must stay buildable on its own, so
 * every helper degrades to an empty list when the directory is absent.
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

// Vitest runs from the package root, and `import.meta.url` is an http URL under
// the jsdom environment, so resolve the sibling package from the cwd instead.
export const FIXTURE_ROOT = join(process.cwd(), '..', 'protocol', 'fixtures');

export const fixturesAvailable = (): boolean => existsSync(FIXTURE_ROOT);

/** Every `*.json` file under a fixture subdirectory, relative path included. */
export function listFixtures(subdir = ''): { path: string; name: string }[] {
  const root = join(FIXTURE_ROOT, subdir);
  if (!existsSync(root)) return [];
  const out: { path: string; name: string }[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) walk(full);
      else if (entry.endsWith('.json')) out.push({ path: full, name: relative(FIXTURE_ROOT, full) });
    }
  };
  walk(root);
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

export function readFixture<T>(name: string): T {
  return JSON.parse(readFileSync(join(FIXTURE_ROOT, name), 'utf8')) as T;
}

/**
 * A fixture the suite reads at module scope. Returns an empty object when
 * `protocol/fixtures/` is absent, so the package still runs on its own; every
 * suite that uses one is gated on `fixturesAvailable()`.
 */
export function fixtureOrEmpty<T>(name: string): T {
  return fixturesAvailable() ? readFixture<T>(name) : ({} as T);
}
