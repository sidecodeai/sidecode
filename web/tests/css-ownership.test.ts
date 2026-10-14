/**
 * Class names are global: two features that pick the same one style each
 * other's markup. The accounts screen and the chat both called a row
 * `.user-row`, so hovering a message bubble drew the account list's hover tint.
 * Every feature therefore owns its root classes alone — the first class of each
 * selector, which is what a rule applies to when nothing scopes it.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const FEATURES = join(process.cwd(), 'src', 'features');

/** Every `*.css` under `src/features`, as `[feature, path]`. */
function featureStylesheets(): [string, string][] {
  const out: [string, string][] = [];
  for (const feature of readdirSync(FEATURES)) {
    const dir = join(FEATURES, feature);
    if (!statSync(dir).isDirectory()) continue;
    for (const entry of readdirSync(dir)) {
      if (entry.endsWith('.css')) out.push([feature, join(dir, entry)]);
    }
  }
  return out;
}

/**
 * The classes a stylesheet's rules hang off: for `.a.b .c` that is `a`, since
 * `.c` is only reached through a rule the feature already owns. At-rules are
 * containers, and their inner rules are read on their own.
 */
function rootClasses(css: string): Set<string> {
  const found = new Set<string>();
  for (const match of css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]*)\{/g)) {
    const group = (match[1] ?? '').trim();
    if (group === '' || group.startsWith('@')) continue;
    for (const selector of group.split(',')) {
      const head = selector.trim().split(/[\s>+~]+/)[0] ?? '';
      const hit = /^\.(-?[_a-zA-Z][\w-]*)/.exec(head);
      if (hit?.[1]) found.add(hit[1]);
    }
  }
  return found;
}

describe('stylesheet ownership', () => {
  it('gives every root class to one feature', () => {
    const owners = new Map<string, Set<string>>();
    for (const [feature, path] of featureStylesheets()) {
      for (const name of rootClasses(readFileSync(path, 'utf8'))) {
        const seen = owners.get(name) ?? new Set<string>();
        seen.add(feature);
        owners.set(name, seen);
      }
    }

    const shared = [...owners]
      .filter(([, features]) => features.size > 1)
      .map(([name, features]) => `.${name}: ${[...features].sort().join(', ')}`);

    expect(shared).toEqual([]);
    expect(owners.size).toBeGreaterThan(100);
  });
});
