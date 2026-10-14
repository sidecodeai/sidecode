/**
 * The development mock's half of A37: `npm run dev:mock` must answer
 * `device.mkdir` the way a device does, or the picker's flow cannot be driven
 * against it. PROTOCOL §6.3: one directory, the new listing as the reply,
 * `bad_request` for the name, `conflict` for a name already taken.
 */
import { describe, expect, it } from 'vitest';
import { dirEntries, makeDir, validFolderName } from '../mock/dirs';

const HOME = '/Users/me';

describe("the mock device's mkdir", () => {
  it('answers with the new directory, empty, and lists it in its parent', () => {
    const made = makeDir(`${HOME}/dev`, HOME, 'round41');

    expect(made).toEqual({
      path: `${HOME}/dev/round41`,
      parent: `${HOME}/dev`,
      entries: [],
    });
    expect(dirEntries(`${HOME}/dev`, HOME).map((e) => e.name)).toContain('round41');
    expect(dirEntries(`${HOME}/dev/round41`, HOME)).toEqual([]);
  });

  it('refuses a name already taken', () => {
    expect(makeDir(`${HOME}/dev`, HOME, 'remote-control')).toEqual({
      error: { code: 'conflict', message: expect.any(String) },
    });
  });

  it('refuses a name that is not one path component', () => {
    for (const name of ['', '.', '.hidden', 'a/b', 'x'.repeat(256)]) {
      expect(validFolderName(name)).toBe(false);
      expect(makeDir(`${HOME}/work`, HOME, name)).toMatchObject({
        error: { code: 'bad_request' },
      });
    }
    expect(validFolderName('x'.repeat(255))).toBe(true);
    // 255 bytes, not 255 characters.
    expect(validFolderName('好'.repeat(85))).toBe(true);
    expect(validFolderName('好'.repeat(86))).toBe(false);
  });

  it('refuses a parent outside the tree it knows', () => {
    expect(makeDir('/etc', HOME, 'round41')).toEqual({
      error: { code: 'not_found', message: expect.any(String) },
    });
  });
});
