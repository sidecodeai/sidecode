import type { StateStorage } from 'zustand/middleware';

/**
 * `localStorage` is missing or throws in private windows, in embedded webviews
 * and under strict cookie policies. What the app remembers about a browser is
 * a convenience, so it falls back to memory rather than letting the app fail
 * to boot. One instance, shared by everything that remembers.
 */
function browserStorage(): StateStorage | null {
  try {
    const store = globalThis.localStorage;
    if (!store) return null;
    const probe = '__rc_probe__';
    store.setItem(probe, '1');
    store.removeItem(probe);
    return store;
  } catch {
    return null;
  }
}

function memoryStorage(): StateStorage {
  const map = new Map<string, string>();
  return {
    getItem: (name) => map.get(name) ?? null,
    setItem: (name, value) => void map.set(name, value),
    removeItem: (name) => void map.delete(name),
  };
}

export const storage: StateStorage = browserStorage() ?? memoryStorage();

/** A value read now, or nothing: the storages above answer at once. */
export function readStored(name: string): string | null {
  const value = storage.getItem(name);
  return typeof value === 'string' ? value : null;
}
