/**
 * A33 — the fresh half of a device page.
 *
 * `hello` and `agents.updated` carry an account without its windows, because
 * they come from files that change rarely. The windows are read on request, so
 * the page asks `device.agents` the moment it opens and keeps the reply here,
 * in the page's own state: nothing with limits in it reaches the devices store,
 * where the list and the new-session drawer would then diff against it every
 * quarter hour.
 *
 * The reply is held under the key of the request that fetched it — the device,
 * whether it was reachable, and which attempt this is — so a device change or a
 * Refresh reads as "Checking…" without the effect having to write that state.
 */
import { useCallback, useEffect, useState } from 'react';
import { errorText } from '../../lib/errors';
import { rpc } from '../../lib/gateway';
import { RequestError } from '../../lib/ws';
import type { AgentAccount } from '../../protocol/types';

/** Where the meters stand: waiting, drawn, or replaced by one line saying why. */
export type QuotaStatus = 'checking' | 'ready' | 'offline' | 'failed';

export interface DeviceQuota {
  status: QuotaStatus;
  /** Accounts with their limits, by agent id. Empty until the reply arrives. */
  accounts: Record<string, AgentAccount[]>;
  /** Why the request failed, when it did. */
  error: string | null;
  refresh: () => void;
}

interface Outcome {
  status: Exclude<QuotaStatus, 'checking'>;
  accounts: Record<string, AgentAccount[]>;
  error: string | null;
}

const checking: Omit<DeviceQuota, 'refresh'> = { status: 'checking', accounts: {}, error: null };

export function useDeviceQuota(deviceId: string | undefined, online: boolean): DeviceQuota {
  const [attempt, setAttempt] = useState(0);
  const [answered, setAnswered] = useState<{ key: string; outcome: Outcome } | null>(null);
  const key = `${deviceId ?? ''}:${online}:${attempt}`;

  useEffect(() => {
    // An offline device has accounts and no meters: asking could only time out.
    if (deviceId === undefined || !online) return;
    let cancelled = false;
    const requested = `${deviceId}:${online}:${attempt}`;
    void rpc('device.agents', { device_id: deviceId })
      .then((result) => {
        if (cancelled) return;
        const accounts: Record<string, AgentAccount[]> = {};
        for (const agent of result.agents) {
          if (agent.accounts) accounts[agent.agent] = agent.accounts;
        }
        setAnswered({ key: requested, outcome: { status: 'ready', accounts, error: null } });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const gone = err instanceof RequestError && err.code === 'device_offline';
        setAnswered({
          key: requested,
          outcome: gone
            ? { status: 'offline', accounts: {}, error: null }
            : { status: 'failed', accounts: {}, error: errorText(err) },
        });
      });
    return () => {
      cancelled = true;
    };
  }, [deviceId, online, attempt]);

  const refresh = useCallback(() => setAttempt((n) => n + 1), []);
  if (!online) return { status: 'offline', accounts: {}, error: null, refresh };
  if (answered?.key !== key) return { ...checking, refresh };
  return { ...answered.outcome, refresh };
}
