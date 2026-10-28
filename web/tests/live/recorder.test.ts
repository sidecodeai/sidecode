/**
 * The call's record while it runs: written at most every 2 s when the words changed, at once and
 * for good when the call ends, with nothing left pending after that.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { forgetHistory, readHistory } from '../../src/features/live/history';
import { CallRecorder, SAVE_EVERY_MS } from '../../src/features/live/recorder';
import type { Entry } from '../../src/features/live/transcript';

let storage: Map<string, string>;
let writes: number;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(1_700_000_000_000);
  forgetHistory();
  storage = new Map();
  writes = 0;
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => {
      writes += 1;
      storage.set(key, value);
    },
    removeItem: (key: string) => void storage.delete(key),
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const said = (text: string): Entry[] => [{ kind: 'turn', speaker: 'user', text }];
const START = 1_700_000_000_000;

describe('the recorder', () => {
  it('writes within two seconds of the first change, and once for all the changes in between', () => {
    const recorder = new CallRecorder('admin', START);
    recorder.note(said('one'));
    vi.advanceTimersByTime(500);
    recorder.note(said('one two'));
    vi.advanceTimersByTime(500);
    recorder.note(said('one two three'));
    expect(writes).toBe(0);
    vi.advanceTimersByTime(SAVE_EVERY_MS);
    expect(writes).toBe(1);
    expect(readHistory('admin')[0]?.entries).toEqual(said('one two three'));
    expect(readHistory('admin')[0]).toMatchObject({ reason: null });
  });

  it('writes nothing more while nothing changes, and again after the next change', () => {
    const recorder = new CallRecorder('admin', START);
    recorder.note(said('one'));
    vi.advanceTimersByTime(SAVE_EVERY_MS);
    vi.advanceTimersByTime(20_000);
    expect(writes).toBe(1);
    recorder.note(said('one two'));
    vi.advanceTimersByTime(SAVE_EVERY_MS);
    expect(writes).toBe(2);
  });

  it('writes at once on finish, with the reason, and leaves no save pending', () => {
    const recorder = new CallRecorder('admin', START);
    recorder.note(said('one'));
    expect(vi.getTimerCount()).toBe(1);
    const kept = recorder.finish(said('one two'), 'hangup');
    expect(writes).toBe(1);
    expect(kept?.[0]).toMatchObject({ reason: 'hangup', entries: said('one two') });
    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(60_000);
    expect(writes).toBe(1);
    expect(readHistory('admin')[0]?.reason).toBe('hangup');
  });

  it('clears the pending save on finish even when there is nothing to write', () => {
    const recorder = new CallRecorder('admin', START);
    recorder.note([]);
    expect(vi.getTimerCount()).toBe(1);
    expect(recorder.finish([], 'idle')).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    expect(writes).toBe(0);
    expect(storage.size).toBe(0);
  });

  it('replaces its own earlier save instead of adding a second call', () => {
    const recorder = new CallRecorder('admin', START);
    recorder.note(said('one'));
    vi.advanceTimersByTime(SAVE_EVERY_MS);
    recorder.finish(said('one two'), 'idle');
    expect(readHistory('admin')).toHaveLength(1);
  });

  it('never writes a record that ends before it began, when the clock was set back in the call', () => {
    const recorder = new CallRecorder('admin', START);
    vi.setSystemTime(START - 3_600_000);
    recorder.finish(said('one'), 'hangup');
    const [kept] = readHistory('admin');
    expect(kept?.endedAt).toBe(START);
    // And it is a record the next read keeps.
    forgetHistory();
    expect(readHistory('admin')).toHaveLength(1);
  });
});
