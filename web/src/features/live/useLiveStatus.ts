import { useCallback, useEffect, useState } from 'react';
import { fetchLiveStatus, knownLiveStatus, refreshLiveStatus, type LiveStatus } from './api';

interface Answer {
  username: string;
  status: LiveStatus;
}

export interface LiveStatusHook {
  /** What the gateway says of the assistant for this account; null until it has said. */
  status: LiveStatus | null;
  /** Ask the gateway again now; a failure keeps the answer given before. */
  refresh: () => void;
}

/**
 * What the gateway says of the assistant for this account: nothing until it has said, and an
 * answer given for another account is never taken for this one. The gateway is asked once per
 * page load, so remounting the top bar shows the button at once and asks nothing. The exceptions
 * are a "not now", which opening the panel asks about again so an account change made elsewhere
 * or a month that turned needs no reload, and whatever the caller asks `refresh` for.
 */
export function useLiveStatus(username: string, open: boolean): LiveStatusHook {
  const [answer, setAnswer] = useState<Answer | null>(() => {
    const known = knownLiveStatus(username);
    return known === undefined ? null : { username, status: known };
  });

  const refresh = useCallback(() => {
    void refreshLiveStatus(username).then((status) => setAnswer({ username, status }));
  }, [username]);

  useEffect(() => {
    let cancelled = false;
    void fetchLiveStatus(username).then((status) => {
      if (!cancelled) setAnswer({ username, status });
    });
    return () => {
      cancelled = true;
    };
  }, [username]);

  useEffect(() => {
    const known = knownLiveStatus(username);
    if (open && known?.enabled && !known.available) refresh();
  }, [open, username, refresh]);

  const status = answer !== null && answer.username === username ? answer.status : null;
  return { status, refresh };
}
