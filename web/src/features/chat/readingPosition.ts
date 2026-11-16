/**
 * The reading position (`docs/DESIGN.md` § "Reading position"): for each
 * conversation shown in this browser, where the person was reading when it was
 * last saved — the lowest row whose top was on screen — and the newest event the
 * app held then. It is kept in local storage under the signed-in account's
 * name, beside that account's settings — the page's origin is its gateway, so
 * the browser already keeps it per gateway — and goes nowhere else: not to the
 * gateway, not to another device. A browser that cannot store keeps it for the
 * tab's life; nothing here throws.
 */

const KEY_PREFIX = 'rc.reading.';

/** The conversations kept per account; past it, the ones read longest ago go first. */
export const MAX_POSITIONS = 500;

/** Where a conversation was being read when it was last saved. */
export interface ReadingPosition {
  /**
   * The lowest row whose top was on screen, by its place in display order: the
   * `seq` of its block's first event (`first_seq`), never a later one, so a long
   * row that finished after a message drawn below it still sorts above it.
   */
  seq: number;
  /**
   * The newest event the app held then. A message of the person's numbered
   * after it was written since: from a terminal, or on another device.
   */
  held: number;
}

/** Stored as `[seq, held]`, one pair per conversation. */
type Stored = Record<string, [number, number]>;

/** What a browser that cannot store keeps instead, per account. */
const memory = new Map<string, Stored>();

const storageKey = (account: string): string => `${KEY_PREFIX}${account}`;

const isSeq = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;

const isPair = (value: unknown): value is [number, number] =>
  Array.isArray(value) && value.length === 2 && isSeq(value[0]) && isSeq(value[1]);

/** What the browser holds for the account, or null when it cannot be read at all. */
function readStored(account: string): Stored | null {
  try {
    const store = globalThis.localStorage;
    if (!store) return null;
    const text = store.getItem(storageKey(account));
    if (text === null) return {};
    const value: unknown = JSON.parse(text);
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, pair]) => isPair(pair))) as Stored;
  } catch {
    return null;
  }
}

function writeStored(account: string, positions: Stored): boolean {
  try {
    const store = globalThis.localStorage;
    if (!store) return false;
    store.setItem(storageKey(account), JSON.stringify(positions));
    return true;
  } catch {
    return false;
  }
}

const positionsOf = (account: string): Stored =>
  readStored(account) ?? memory.get(account) ?? {};

/** The position saved for a conversation, or null when it was never read here. */
export function readPosition(account: string, session: string): ReadingPosition | null {
  const pair = positionsOf(account)[session];
  return pair ? { seq: pair[0], held: pair[1] } : null;
}

/**
 * Keeps where the conversation is being read now, wherever that is: a reader
 * who scrolled back into an older turn comes back to that turn. The
 * conversation saved last moves to the end, so the cap drops the ones read
 * longest ago.
 */
export function savePosition(account: string, session: string, position: ReadingPosition): void {
  if (!isSeq(position.seq) || !isSeq(position.held)) return;
  const next: Stored = { ...positionsOf(account) };
  delete next[session];
  next[session] = [position.seq, position.held];
  const sessions = Object.keys(next);
  for (const old of sessions.slice(0, Math.max(0, sessions.length - MAX_POSITIONS))) delete next[old];
  if (writeStored(account, next)) memory.delete(account);
  else memory.set(account, next);
}

/** Removes every position this browser keeps for an account. */
export function forgetPositions(account: string): void {
  memory.delete(account);
  try {
    globalThis.localStorage?.removeItem(storageKey(account));
  } catch {
    // Nothing was stored, or nothing can be: either way nothing is left.
  }
}
