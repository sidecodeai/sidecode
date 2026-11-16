/**
 * `docs/DESIGN.md` § "Reading position": the position is kept in this browser
 * alone, per account and conversation, moves only forward, and survives a
 * browser that cannot store.
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

  it('is kept per account and per conversation', () => {
    savePosition(ME, KEY, 40);
    savePosition(ME, 'dev-mac/ses-2', 7);
    savePosition('someone-else', KEY, 90);

    expect(readPosition(ME, KEY)).toBe(40);
    expect(readPosition(ME, 'dev-mac/ses-2')).toBe(7);
    expect(readPosition('someone-else', KEY)).toBe(90);
  });

  it('only moves forward', () => {
    savePosition(ME, KEY, 40);
    savePosition(ME, KEY, 12);
    expect(readPosition(ME, KEY)).toBe(40);
    savePosition(ME, KEY, 41);
    expect(readPosition(ME, KEY)).toBe(41);
  });

  it('keeps the most recently read conversations past the cap', () => {
    for (let n = 0; n <= MAX_POSITIONS; n += 1) savePosition(ME, `dev/ses-${n}`, n + 1);

    expect(readPosition(ME, 'dev/ses-0')).toBeNull();
    expect(readPosition(ME, 'dev/ses-1')).toBe(2);
    expect(readPosition(ME, `dev/ses-${MAX_POSITIONS}`)).toBe(MAX_POSITIONS + 1);
  });

  it('reads past what it cannot understand', () => {
    stored.set(`rc.reading.${ME}`, '{"dev-mac/ses-1": "forty", "dev-mac/ses-2": 3}');
    expect(readPosition(ME, KEY)).toBeNull();
    expect(readPosition(ME, 'dev-mac/ses-2')).toBe(3);

    stored.set(`rc.reading.${ME}`, 'not json');
    expect(readPosition(ME, KEY)).toBeNull();
  });

  it('is kept for the tab when the browser cannot store', () => {
    const denied = () => {
      throw new Error('denied');
    };
    vi.stubGlobal('localStorage', { getItem: denied, setItem: denied, removeItem: denied });

    expect(() => savePosition(ME, KEY, 40)).not.toThrow();
    expect(readPosition(ME, KEY)).toBe(40);
    expect(() => forgetPositions(ME)).not.toThrow();
    expect(readPosition(ME, KEY)).toBeNull();
  });

  it('is kept for the tab when there is no storage at all', () => {
    vi.stubGlobal('localStorage', undefined);

    savePosition(ME, KEY, 40);

    expect(readPosition(ME, KEY)).toBe(40);
    forgetPositions(ME);
  });

  it('is gone with the account', () => {
    savePosition(ME, KEY, 40);
    forgetPositions(ME);
    expect(readPosition(ME, KEY)).toBeNull();
    expect(stored.has(`rc.reading.${ME}`)).toBe(false);
  });
});
