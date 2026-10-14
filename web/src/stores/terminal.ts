/**
 * A38 — what the app keeps about terminals, outside React.
 *
 * Two small things live here. The first is the feed: `terminal.output` and
 * `terminal.exited` arrive on the app socket like any push frame, and the
 * connection store hands them here rather than into a React tree it knows
 * nothing about. The second is the id of the terminal each device is running
 * for this tab, in `sessionStorage`: a reload or a lost socket is the only
 * reason a page ever asks for it, and the device keeps the shell ten minutes
 * for exactly that.
 *
 * Nothing that travels through a terminal is kept or logged — only the id.
 */
import type { PushFrame } from '../protocol/frames';

export type TerminalFrame = Extract<
  PushFrame,
  { type: 'terminal.output' } | { type: 'terminal.exited' }
>;

type Listener = (frame: TerminalFrame) => void;

const listeners = new Set<Listener>();

/** The open terminal page listens; nothing else does. */
export function onTerminalFrame(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function pushTerminalFrame(frame: TerminalFrame): void {
  for (const listener of [...listeners]) listener(frame);
}

const KEY_PREFIX = 'rc.terminal.';

const storage = (): Storage | null => {
  try {
    return globalThis.sessionStorage ?? null;
  } catch {
    // A browser with storage denied still gets a terminal; it just cannot
    // re-attach to one after a reload.
    return null;
  }
};

export function rememberTerminal(deviceId: string, terminalId: string): void {
  try {
    storage()?.setItem(`${KEY_PREFIX}${deviceId}`, terminalId);
  } catch {
    /* full or denied: the id is a convenience, never a requirement */
  }
}

export function rememberedTerminal(deviceId: string): string | null {
  try {
    return storage()?.getItem(`${KEY_PREFIX}${deviceId}`) ?? null;
  } catch {
    return null;
  }
}

export function forgetTerminal(deviceId: string): void {
  try {
    storage()?.removeItem(`${KEY_PREFIX}${deviceId}`);
  } catch {
    /* nothing to forget */
  }
}

/** Sign-out: the next account attaches to nothing of this one's (`signOut`). */
export function resetTerminals(): void {
  const store = storage();
  if (!store) return;
  try {
    const keys: string[] = [];
    for (let i = 0; i < store.length; i += 1) {
      const key = store.key(i);
      if (key?.startsWith(KEY_PREFIX)) keys.push(key);
    }
    for (const key of keys) store.removeItem(key);
  } catch {
    /* denied: nothing was ever written either */
  }
}
