import { useMemo } from 'react';
import { useAuth } from '../../stores/auth';
import { draftState as stateOfDraft, type CallView, type DraftState } from './callState';
import type { CallRecord } from './history';
import { cancelDraft, hangUp, sendDraft, setMuted, startCall } from './liveCall';
import { useLive } from './store';

export interface LiveCall {
  view: CallView;
  draftState: DraftState;
  /** The signed-in account's saved conversations, newest first. */
  records: CallRecord[];
  start: () => Promise<void>;
  hangUp: () => void;
  sendDraft: () => void;
  cancelDraft: () => void;
  setMuted: (on: boolean) => void;
}

const NO_RECORDS: CallRecord[] = [];

/**
 * The call, as a screen sees it. The call itself is the page's (`liveCall.ts`), so any number of
 * screens, one after another as the route changes, show the same one.
 */
export function useLiveCall(): LiveCall {
  const username = useAuth((s) => s.username) ?? '';
  const view = useLive((s) => s.view);
  const saved = useLive((s) => s.saved);
  const records = saved.owner === username ? saved.records : NO_RECORDS;

  return useMemo(
    () => ({
      view,
      draftState: stateOfDraft(view),
      records,
      start: startCall,
      hangUp,
      sendDraft,
      cancelDraft,
      setMuted,
    }),
    [view, records],
  );
}
