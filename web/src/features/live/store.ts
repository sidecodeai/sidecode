/**
 * What the app knows of the voice assistant, held outside every screen: the call's view, the
 * account's saved conversations, and what the panel was last brought up for. The screens come and
 * go with the route; this does not, which is what lets a call outlive a page.
 */
import { create } from 'zustand';
import { initialView, reduce, type CallAction, type CallView } from './callState';
import type { CallRecord } from './history';

interface LiveState {
  view: CallView;
  /** The saved calls of `owner`, newest first. */
  saved: { owner: string | null; records: CallRecord[] };
  /**
   * What the panel was last brought up for (`announcement` in `callState.ts`: a draft, or a call
   * the account ended), so a change of page does not bring it up again.
   */
  announced: string | null;
  dispatch: (action: CallAction) => void;
  keep: (owner: string, records: CallRecord[]) => void;
  announce: (key: string) => void;
  /** The account changed: nothing of the last one stays. */
  reset: () => void;
}

const none = { owner: null, records: [] };

export const useLive = create<LiveState>((set) => ({
  view: initialView,
  saved: none,
  announced: null,
  dispatch: (action) => set((state) => ({ view: reduce(state.view, action) })),
  keep: (owner, records) => set({ saved: { owner, records } }),
  announce: (announced) => set({ announced }),
  reset: () => set({ view: initialView, saved: none, announced: null }),
}));
