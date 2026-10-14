/**
 * A47: the conversation the person has in front of them — open on a page that
 * is visible, in a window that has the focus — or none. It is being looked at,
 * so its row draws no red dot and the app's icon does not count it while the
 * `session.seen` that clears the mark on every app is on its way.
 */
import { create } from 'zustand';
import type { SessionKey } from './sessions';

interface FrontState {
  key: SessionKey | null;
  /** Sign-out: nothing of the previous account stays in the tab (`signOut`). */
  reset: () => void;
}

export const useFront = create<FrontState>((set) => ({
  key: null,
  reset: () => set({ key: null }),
}));
