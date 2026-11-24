/**
 * A65 — Start talking as the panel offers it. The gateway is asked again first (`startCheck.ts`),
 * and its answer leads anywhere only while the panel that asked is still open: a panel closed
 * before the answer came cancels the start, so no call opens and no permission is asked after the
 * person closed it.
 */
import { useEffect, useRef, useState } from 'react';
import type { LiveStatus } from './api';
import { stepAfterCheck } from './startCheck';

export interface StartTalking {
  /** The gateway is being asked again. */
  checking: boolean;
  /** The service whose permission is being asked; undefined while nothing is asked. */
  asking: string | null | undefined;
  /** Start talking was pressed. */
  begin: () => Promise<void>;
  /** The permission screen closed, agreed or not. */
  stopAsking: () => void;
}

export function useStartTalking(refresh: () => Promise<LiveStatus>, start: () => void): StartTalking {
  const [checking, setChecking] = useState(false);
  const [asking, setAsking] = useState<string | null | undefined>(undefined);
  // The press being answered. Closing the panel clears it, which cancels that press.
  const pending = useRef<object | null>(null);

  useEffect(
    () => () => {
      pending.current = null;
    },
    [],
  );

  const begin = async (): Promise<void> => {
    const press = {};
    pending.current = press;
    setChecking(true);
    const step = await stepAfterCheck(refresh).finally(() => setChecking(false));
    if (pending.current !== press) return;
    pending.current = null;
    if (step.kind === 'call') start();
    else if (step.kind === 'ask') setAsking(step.provider);
  };

  return { checking, asking, begin, stopAsking: () => setAsking(undefined) };
}
