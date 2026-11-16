/**
 * The reading position (`docs/DESIGN.md` § "Reading position"): for each
 * conversation shown in this browser, the `seq` of the furthest row whose top
 * edge has been inside the transcript. It is kept in local storage under the
 * signed-in account's name — the page's origin is its gateway, so the browser
 * already keeps it per gateway — and goes nowhere else: not to the gateway, not
 * to another device. A browser that cannot store keeps it for the tab's life;
 * nothing here throws.
 */

const KEY_PREFIX = 'rc.reading.';

/** The conversations kept per account; past it, the longest unread go first. */
export const MAX_POSITIONS = 500;

/** What a browser that cannot store keeps instead, per account. */
const memory = new Map<string, Record<string, number>>();

const storageKey = (account: string): string => `${KEY_PREFIX}${account}`;

const isPosition = (value: unknown): value is number =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;

/** What the browser holds for the account, or null when it cannot be read at all. */
function readStored(account: string): Record<string, number> | null {
  try {
    const store = globalThis.localStorage;
    if (!store) return null;
    const text = store.getItem(storageKey(account));
    if (text === null) return {};
    const value: unknown = JSON.parse(text);
    if (typeof value !== 'object' || value === null || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([, seq]) => isPosition(seq)));
  } catch {
    return null;
  }
}

function writeStored(account: string, positions: Record<string, number>): boolean {
  try {
    const store = globalThis.localStorage;
    if (!store) return false;
    store.setItem(storageKey(account), JSON.stringify(positions));
    return true;
  } catch {
    return false;
  }
}

const positionsOf = (account: string): Record<string, number> =>
  readStored(account) ?? memory.get(account) ?? {};

/** The position saved for a conversation, or null when it was never read here. */
export function readPosition(account: string, session: string): number | null {
  return positionsOf(account)[session] ?? null;
}

/**
 * Keeps `seq` as the conversation's position when it is further than the one
 * held: a position only moves forward. The conversation written last moves to
 * the end, so the cap drops the ones read longest ago.
 */
export function savePosition(account: string, session: string, seq: number): void {
  if (!isPosition(seq)) return;
  const held = positionsOf(account);
  if ((held[session] ?? -1) >= seq) return;
  const next: Record<string, number> = { ...held };
  delete next[session];
  next[session] = seq;
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
