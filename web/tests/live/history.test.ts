import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  MAX_BYTES,
  MAX_CALLS,
  clearHistory,
  forgetHistory,
  keepRecord,
  readHistory,
  trim,
  type CallRecord,
} from '../../src/features/live/history';
import type { Entry } from '../../src/features/live/transcript';

/** This jsdom build has no `localStorage`: an in-memory one stands in. */
let storage: Map<string, string>;

function stubStorage(): void {
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => void storage.set(key, value),
    removeItem: (key: string) => void storage.delete(key),
  });
}

const say = (text: string): Entry => ({ kind: 'turn', speaker: 'user', text });

function record(n: number, entries: Entry[] = [say(`call ${n}`)]): CallRecord {
  const startedAt = 1_700_000_000_000 + n * 60_000;
  return { id: String(startedAt), startedAt, endedAt: startedAt + 30_000, reason: 'hangup', entries };
}

const sizeOf = (value: unknown): number => new TextEncoder().encode(JSON.stringify(value)).length;

beforeEach(() => {
  forgetHistory();
  storage = new Map();
  stubStorage();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the saved conversations', () => {
  it('keeps a call under the signed-in account and reads it back, newest first', () => {
    keepRecord('admin', record(1));
    keepRecord('admin', record(3));
    keepRecord('admin', record(2));
    expect(readHistory('admin').map((r) => r.entries[0])).toEqual([say('call 3'), say('call 2'), say('call 1')]);
    expect(storage.has('live.history:admin')).toBe(true);
  });

  it('scopes the history to the account: another account sees nothing of it', () => {
    keepRecord('admin', record(1));
    keepRecord('dana', record(2));
    expect(readHistory('admin')).toHaveLength(1);
    expect(readHistory('admin')[0]?.entries[0]).toEqual(say('call 1'));
    expect(readHistory('dana')[0]?.entries[0]).toEqual(say('call 2'));
    expect(readHistory('eve')).toEqual([]);
    expect([...storage.keys()].sort()).toEqual(['live.history:admin', 'live.history:dana']);
  });

  it('replaces the earlier save of the same call instead of adding another', () => {
    keepRecord('admin', record(1, [say('first words')]));
    keepRecord('admin', { ...record(1, [say('first words'), say('more')]), endedAt: 1_700_000_090_000 });
    const kept = readHistory('admin');
    expect(kept).toHaveLength(1);
    expect(kept[0]?.entries).toHaveLength(2);
  });

  it('keeps the newest 20 calls', () => {
    for (let n = 1; n <= MAX_CALLS + 5; n += 1) keepRecord('admin', record(n));
    const kept = readHistory('admin');
    expect(kept).toHaveLength(MAX_CALLS);
    expect(kept[0]?.entries[0]).toEqual(say(`call ${MAX_CALLS + 5}`));
    expect(kept.at(-1)?.entries[0]).toEqual(say('call 6'));
    expect(JSON.parse(storage.get('live.history:admin') ?? '[]')).toHaveLength(MAX_CALLS);
  });

  it('keeps about 200 KB, trimming the oldest text first', () => {
    const turns = (n: number): Entry[] =>
      Array.from({ length: 40 }, (_, i) => say(`call ${n} turn ${i} ${'x'.repeat(500)}`));
    // Ten calls of roughly 22 KB each: a little more than the budget.
    for (let n = 1; n <= 10; n += 1) keepRecord('admin', record(n, turns(n)));
    const kept = readHistory('admin');
    expect(sizeOf(kept)).toBeLessThanOrEqual(MAX_BYTES);
    expect(kept).toHaveLength(10);
    // Only the oldest call lost text, its earliest turns; every newer call is whole.
    expect(kept.slice(0, -1).every((r) => r.entries.length === 40)).toBe(true);
    const oldest = kept.at(-1);
    expect(oldest?.entries.length).toBeLessThan(40);
    expect(oldest?.entries.at(-1)).toEqual(say(`call 1 turn 39 ${'x'.repeat(500)}`));
    // More calls push the oldest out whole, one after another, newest kept.
    for (let n = 11; n <= 15; n += 1) keepRecord('admin', record(n, turns(n)));
    const later = readHistory('admin');
    expect(sizeOf(later)).toBeLessThanOrEqual(MAX_BYTES);
    expect(later.length).toBeLessThan(15);
    expect(later[0]?.entries).toHaveLength(40);
    expect(later[0]?.entries[0]).toEqual(say(`call 15 turn 0 ${'x'.repeat(500)}`));
  });

  it('drops a call whose text is all gone, and never keeps an empty one', () => {
    const big: Entry[] = [say('y'.repeat(150 * 1024)), say('z'.repeat(150 * 1024))];
    const kept = trim([record(2, big), record(1, [say('tiny')])]);
    expect(sizeOf(kept)).toBeLessThanOrEqual(MAX_BYTES);
    expect(kept.every((r) => r.entries.length > 0)).toBe(true);
  });

  it('clears the account and leaves the others', () => {
    keepRecord('admin', record(1));
    keepRecord('dana', record(2));
    clearHistory('admin');
    expect(readHistory('admin')).toEqual([]);
    expect(storage.has('live.history:admin')).toBe(false);
    expect(readHistory('dana')).toHaveLength(1);
  });

  it('forgets what the page holds in memory and finds the rest in the browser', () => {
    keepRecord('admin', record(1));
    forgetHistory();
    expect(readHistory('admin')).toHaveLength(1);
  });

  it('sees the calls another tab saved, and a clear another tab made', () => {
    keepRecord('admin', record(1));
    // Another tab saved a call this page never saw.
    storage.set('live.history:admin', JSON.stringify([record(2), record(1)]));
    expect(readHistory('admin')).toHaveLength(2);
    keepRecord('admin', record(3));
    expect(readHistory('admin')).toHaveLength(3);
    // Another tab cleared everything: a later save does not bring the old calls back.
    storage.delete('live.history:admin');
    keepRecord('admin', record(4));
    expect(readHistory('admin').map((r) => r.id)).toEqual([record(4).id]);
  });

  it('ignores what is not a record: damaged text, wrong shapes, odd entries', () => {
    storage.set('live.history:admin', '{not json');
    expect(readHistory('admin')).toEqual([]);
    storage.set(
      'live.history:admin',
      JSON.stringify([
        record(1),
        { id: 5 },
        { ...record(2), entries: [{ kind: 'turn', speaker: 'robot', text: 'x' }] },
        { ...record(3), entries: [{ kind: 'sent', text: 'no session' }] },
        'text',
        null,
      ]),
    );
    expect(readHistory('admin').map((r) => r.id)).toEqual([record(1).id]);
    storage.set('live.history:admin', '{"not":"a list"}');
    expect(readHistory('admin')).toEqual([]);
  });
});

describe('records that cannot be real', () => {
  const read = (...records: unknown[]): CallRecord[] => {
    storage.set('live.history:admin', JSON.stringify(records));
    return readHistory('admin');
  };
  const ids = (records: CallRecord[]): string[] => records.map((r) => r.id);

  it.each([
    ['a start beyond what a Date holds', { startedAt: 1e20, endedAt: 1e20 + 1e10 }],
    ['an end beyond what a Date holds', { endedAt: 1e20 }],
    ['a start just past the limit', { startedAt: 8.64e15 + 1000, endedAt: 8.64e15 + 2000 }],
    ['a start far before the epoch', { startedAt: -1e20, endedAt: 0 }],
    ['an end before its start', { startedAt: 1_700_000_060_000, endedAt: 1_700_000_000_000 }],
    ['times written as text', { startedAt: '1700000000000', endedAt: '1700000060000' }],
    ['an end that is null, which is what JSON makes of a number that is not one', { endedAt: null }],
    ['no times at all', { startedAt: undefined, endedAt: undefined }],
  ])('drops a record with %s, and keeps the sound ones around it', (_name, bad) => {
    const kept = read(record(3), { ...record(2), ...bad }, record(1));
    expect(ids(kept)).toEqual([record(3).id, record(1).id]);
  });

  it('keeps a record at the very edge of what a Date holds', () => {
    const edge = { ...record(1), startedAt: 8.64e15 - 1000, endedAt: 8.64e15 };
    expect(ids(read(edge))).toEqual([edge.id]);
    const before = { ...record(2), startedAt: -8.64e15, endedAt: -8.64e15 + 1000 };
    expect(ids(read(before))).toEqual([before.id]);
  });

  it('keeps a call that ended the moment it began', () => {
    const instant = { ...record(1), endedAt: record(1).startedAt };
    expect(ids(read(instant))).toEqual([instant.id]);
  });

  it('draws every record it keeps as a date without raising', () => {
    const kept = read(
      record(1),
      { ...record(2), startedAt: 1e20, endedAt: 1e20 },
      { ...record(3), startedAt: 8.64e15, endedAt: 8.64e15 },
      { ...record(4), startedAt: -8.64e15, endedAt: -8.64e15 },
    );
    for (const call of kept) {
      expect(() => new Intl.DateTimeFormat('en').format(new Date(call.startedAt))).not.toThrow();
    }
  });
});

describe('a browser that cannot keep them', () => {
  const failing = (over: Partial<Storage>): void => {
    vi.stubGlobal('localStorage', {
      getItem: () => null,
      setItem: () => undefined,
      removeItem: () => undefined,
      ...over,
    });
  };

  it('keeps the call in memory when the quota is full, and does not raise', () => {
    failing({
      setItem: () => {
        throw new DOMException('full', 'QuotaExceededError');
      },
    });
    expect(() => keepRecord('admin', record(1))).not.toThrow();
    expect(readHistory('admin')).toHaveLength(1);
    keepRecord('admin', record(2));
    expect(readHistory('admin').map((r) => r.entries[0])).toEqual([say('call 2'), say('call 1')]);
  });

  it('keeps the call in memory when the storage cannot be read or written at all', () => {
    failing({
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      },
      removeItem: () => {
        throw new Error('denied');
      },
    });
    expect(() => keepRecord('admin', record(1))).not.toThrow();
    expect(readHistory('admin')).toHaveLength(1);
    expect(() => clearHistory('admin')).not.toThrow();
    expect(readHistory('admin')).toEqual([]);
  });

  it('survives an accessor that throws when `localStorage` itself is touched', () => {
    vi.unstubAllGlobals();
    Object.defineProperty(globalThis, 'localStorage', {
      configurable: true,
      get: () => {
        throw new DOMException('blocked', 'SecurityError');
      },
    });
    try {
      expect(() => keepRecord('admin', record(1))).not.toThrow();
      expect(readHistory('admin')).toHaveLength(1);
      expect(() => clearHistory('admin')).not.toThrow();
    } finally {
      Reflect.deleteProperty(globalThis, 'localStorage');
    }
  });

  it('survives a browser with no storage at all', () => {
    vi.stubGlobal('localStorage', undefined);
    expect(() => keepRecord('admin', record(1))).not.toThrow();
    expect(readHistory('admin')).toHaveLength(1);
  });

  it('writes what it held back once the storage works again', () => {
    let full = true;
    vi.stubGlobal('localStorage', {
      getItem: (key: string) => storage.get(key) ?? null,
      setItem: (key: string, value: string) => {
        if (full) throw new DOMException('full', 'QuotaExceededError');
        storage.set(key, value);
      },
      removeItem: (key: string) => void storage.delete(key),
    });
    keepRecord('admin', record(1));
    full = false;
    keepRecord('admin', record(2));
    expect(readHistory('admin')).toHaveLength(2);
    expect(JSON.parse(storage.get('live.history:admin') ?? '[]')).toHaveLength(2);
  });
});
