/**
 * Unconfirmed `session.send` requests. The contract forbids automatic resends,
 * so this only remembers enough for the user to press Retry with the same id.
 */
import { create } from 'zustand';
import type { OutgoingAttachment } from '../protocol/types';
import type { SendMode } from '../protocol/frames';

export interface PendingSend {
  id: string;
  sessionKey: string;
  sessionId: string;
  text: string;
  attachments: OutgoingAttachment[];
  mode: SendMode;
  /** A43: where an edited queued message goes back into the line; a Retry keeps it. */
  queueTs?: number;
  at: number;
  error: string | null;
}

interface OutboxState {
  pending: Record<string, PendingSend>;
  add: (send: PendingSend) => void;
  fail: (id: string, error: string) => void;
  clear: (id: string) => void;
  forSession: (key: string) => PendingSend[];
  /** Sign-out: nothing of the previous account stays in the tab (`signOut`). */
  reset: () => void;
}

export const useOutbox = create<OutboxState>((set, get) => ({
  pending: {},
  add: (send) => set((s) => ({ pending: { ...s.pending, [send.id]: send } })),
  fail: (id, error) =>
    set((s) => {
      const entry = s.pending[id];
      if (!entry) return s;
      return { pending: { ...s.pending, [id]: { ...entry, error } } };
    }),
  clear: (id) =>
    set((s) => {
      if (!s.pending[id]) return s;
      const next = { ...s.pending };
      delete next[id];
      return { pending: next };
    }),
  forSession: (key) => Object.values(get().pending).filter((p) => p.sessionKey === key),
  reset: () => set({ pending: {} }),
}));
