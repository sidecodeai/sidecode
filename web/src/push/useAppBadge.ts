import { useEffect } from 'react';
import { useFront } from '../stores/front';
import { countUnseen, useSessions } from '../stores/sessions';
import { showAppBadge } from './appBadge';

/**
 * A47: while the page runs, the app's icon carries the number of red dots the
 * app draws — the unarchived sessions with the mark, less the conversation in
 * front of the person. Nothing is written before the account's list has
 * arrived, so a badge the service worker set from a push stands until the page
 * knows better; signing out clears it (`signOut`).
 */
export function useAppBadge(): void {
  const loaded = useSessions((s) => s.loaded);
  const front = useFront((s) => s.key);
  const count = useSessions((s) => countUnseen(s.sessions, front));

  useEffect(() => {
    if (loaded) showAppBadge(count);
  }, [loaded, count]);
}
