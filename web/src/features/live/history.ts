/**
 * Saved conversations: each call's record, kept in this browser under the signed-in account's
 * name and nowhere else. A browser that cannot store (private mode, a full quota, an accessor
 * that throws) costs the keeping and nothing more: no function here raises, and the page holds
 * what it has in memory until it closes.
 */
import type { Entry } from './transcript';

export interface CallRecord {
  /** The call's start time as text: an account has one call at a time. */
  id: string;
  startedAt: number;
  /** When the record was last written: the call's end, or the last save of one cut short. */
  endedAt: number;
  /** Why it ended, as the gateway or this page said it; null when nothing got to say. */
  reason: string | null;
  entries: Entry[];
}

export const MAX_CALLS = 20;
export const MAX_BYTES = 200 * 1024;
const KEY_PREFIX = 'live.history:';

/** What this page holds for each account: the fallback for a browser that cannot store. */
const memory = new Map<string, CallRecord[]>();
/** Accounts whose last write failed: the page then knows more than the browser kept. */
const unsaved = new Set<string>();

const encoder = new TextEncoder();
const bytesOf = (value: unknown): number => encoder.encode(JSON.stringify(value)).length;
const keyOf = (username: string): string => `${KEY_PREFIX}${username}`;

function isEntry(value: unknown): value is Entry {
  if (typeof value !== 'object' || value === null) return false;
  const entry = value as Record<string, unknown>;
  if (typeof entry.text !== 'string') return false;
  if (entry.kind === 'turn') return entry.speaker === 'user' || entry.speaker === 'assistant';
  return entry.kind === 'sent' && typeof entry.session === 'string';
}

/** The furthest a `Date` reaches from the epoch, in ms: a time beyond it cannot be drawn as a date. */
const MAX_TIME = 8.64e15;
const isTime = (value: unknown): value is number =>
  typeof value === 'number' && Math.abs(value) <= MAX_TIME;

function isRecord(value: unknown): value is CallRecord {
  if (typeof value !== 'object' || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === 'string' &&
    isTime(record.startedAt) &&
    isTime(record.endedAt) &&
    record.endedAt >= record.startedAt &&
    (record.reason === null || typeof record.reason === 'string') &&
    Array.isArray(record.entries) &&
    record.entries.every(isEntry)
  );
}

/** What the browser keeps for the account: a list, or null when it cannot be read at all. */
function readStored(username: string): CallRecord[] | null {
  try {
    const store = globalThis.localStorage;
    if (!store) return null;
    const text = store.getItem(keyOf(username));
    if (text === null) return [];
    const value: unknown = JSON.parse(text);
    return Array.isArray(value) ? value.filter(isRecord) : [];
  } catch {
    return null;
  }
}

function writeStored(username: string, records: CallRecord[]): void {
  try {
    const store = globalThis.localStorage;
    if (!store) throw new Error('no storage');
    store.setItem(keyOf(username), JSON.stringify(records));
    unsaved.delete(username);
  } catch {
    unsaved.add(username);
  }
}

/** The calls of every list, once each (the later write of one wins), newest first. */
function merge(...lists: CallRecord[][]): CallRecord[] {
  const byId = new Map<string, CallRecord>();
  for (const record of lists.flat()) {
    const known = byId.get(record.id);
    if (!known || record.endedAt >= known.endedAt) byId.set(record.id, record);
  }
  return [...byId.values()].sort((a, b) => b.startedAt - a.startedAt);
}

/** The entries left once at least `over` bytes of the oldest have gone. */
function dropOldest(entries: Entry[], over: number): Entry[] {
  let freed = 0;
  let count = 0;
  while (count < entries.length && freed < over) {
    freed += bytesOf(entries[count]) + 1;
    count += 1;
  }
  return entries.slice(count);
}

/**
 * The newest `MAX_CALLS` calls in at most `MAX_BYTES`. When the text is too much, the oldest
 * call's oldest turns go first, and a call with no text left goes with them.
 */
export function trim(records: CallRecord[]): CallRecord[] {
  const kept = records.slice(0, MAX_CALLS);
  for (let at = kept.length - 1; at >= 0; at -= 1) {
    const over = bytesOf(kept) - MAX_BYTES;
    if (over <= 0) break;
    const oldest = kept[at] as CallRecord;
    const entries = dropOldest(oldest.entries, over);
    if (entries.length === 0) kept.splice(at, 1);
    else kept[at] = { ...oldest, entries };
  }
  return kept;
}

/**
 * The account's saved calls, newest first. The browser is read afresh each time, so a call another
 * tab saved, or a clear it made, is seen; what only this page holds is added back when the browser
 * refused it.
 */
export function readHistory(username: string): CallRecord[] {
  const kept = readStored(username);
  const held = memory.get(username) ?? [];
  const list = kept === null ? held : unsaved.has(username) ? merge(kept, held) : kept;
  memory.set(username, list);
  return list;
}

/** Add a call's record, or replace its earlier save. Returns the account's calls now kept. */
export function keepRecord(username: string, record: CallRecord): CallRecord[] {
  const list = trim(merge(readHistory(username), [record]));
  memory.set(username, list);
  writeStored(username, list);
  return list;
}

export function clearHistory(username: string): void {
  memory.delete(username);
  unsaved.delete(username);
  try {
    globalThis.localStorage?.removeItem(keyOf(username));
  } catch {
    /* a browser that cannot be written to keeps nothing to clear */
  }
}

/** Drop what this page holds in memory; the browser's own copy stays. */
export function forgetHistory(): void {
  memory.clear();
  unsaved.clear();
}
