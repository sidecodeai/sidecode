/**
 * `docs/DESIGN.md` § "Reading position": where a conversation was being
 * read, and what the app held then, is kept in this browser alone, per account
 * and conversation, and survives a browser that cannot store.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  MAX_POSITIONS,
  forgetPositions,
  readPosition,
  savePosition,
} from '../src/features/chat/readingPosition';

const ME = 'me@example.com';
const KEY = 'dev-mac/ses-1';

/** This jsdom build has no `localStorage`: an in-memory one stands in. */
let stored: Map<string, string>;

beforeEach(() => {
  stored = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => void stored.set(key, value),
    removeItem: (key: string) => void stored.delete(key),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the reading position', () => {
  it('is nothing for a conversation never read here', () => {
    expect(readPosition(ME, KEY)).toBeNull();
  });

  it('is kept per account and per conversation, with what the app held', () => {
    savePosition(ME, KEY, { seq: 40, held: 60 });
    savePosition(ME, 'dev-mac/ses-2', { seq: 7, held: 7 });
    savePosition('someone-else', KEY, { seq: 90, held: 95 });

    expect(readPosition(ME, KEY)).toEqual({ seq: 40, held: 60 });
    expect(readPosition(ME, 'dev-mac/ses-2')).toEqual({ seq: 7, held: 7 });
    expect(readPosition('someone-else', KEY)).toEqual({ seq: 90, held: 95 });
  });

  it('follows the reader back up into an older turn', () => {
    savePosition(ME, KEY, { seq: 40, held: 60 });
    savePosition(ME, KEY, { seq: 12, held: 60 });
    expect(readPosition(ME, KEY)).toEqual({ seq: 12, held: 60 });
  });

  it('keeps the conversations read most recently past the cap', () => {
    for (let n = 0; n <= MAX_POSITIONS; n += 1) {
      savePosition(ME, `dev/ses-${n}`, { seq: n + 1, held: n + 1 });
    }

    expect(readPosition(ME, 'dev/ses-0')).toBeNull();
    expect(readPosition(ME, 'dev/ses-1')).toEqual({ seq: 2, held: 2 });
    expect(readPosition(ME, `dev/ses-${MAX_POSITIONS}`)?.seq).toBe(MAX_POSITIONS + 1);
  });

  it('reads past what it cannot understand', () => {
    stored.set(`rc.reading.${ME}`, '{"dev-mac/ses-1": "forty", "dev-mac/ses-2": [3, 5], "x": [1]}');
    expect(readPosition(ME, KEY)).toBeNull();
    expect(readPosition(ME, 'dev-mac/ses-2')).toEqual({ seq: 3, held: 5 });
    expect(readPosition(ME, 'x')).toBeNull();

    stored.set(`rc.reading.${ME}`, 'not json');
    expect(readPosition(ME, KEY)).toBeNull();
  });

  it('is kept for the tab when the browser cannot store', () => {
    const denied = () => {
      throw new Error('denied');
    };
    vi.stubGlobal('localStorage', { getItem: denied, setItem: denied, removeItem: denied });

    expect(() => savePosition(ME, KEY, { seq: 40, held: 60 })).not.toThrow();
    expect(readPosition(ME, KEY)).toEqual({ seq: 40, held: 60 });
    expect(() => forgetPositions(ME)).not.toThrow();
    expect(readPosition(ME, KEY)).toBeNull();
  });

  it('is kept for the tab when there is no storage at all', () => {
    vi.stubGlobal('localStorage', undefined);

    savePosition(ME, KEY, { seq: 40, held: 60 });

    expect(readPosition(ME, KEY)).toEqual({ seq: 40, held: 60 });
    forgetPositions(ME);
  });

  it('is gone with the account', () => {
    savePosition(ME, KEY, { seq: 40, held: 60 });
    forgetPositions(ME);
    expect(readPosition(ME, KEY)).toBeNull();
    expect(stored.has(`rc.reading.${ME}`)).toBe(false);
  });
});
