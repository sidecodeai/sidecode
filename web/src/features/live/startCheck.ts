/**
 * A65 — what a press of Start talking leads to. The gateway is asked again first, because the AI
 * speech service it names may have changed since the panel opened, and the permission held is for
 * the one named before. Then: the call, for a permission that matches; the permission first, for the
 * service named now; or nothing, for an assistant no longer available, a page closed or an account
 * changed while the gateway was being asked.
 */
import { hasConsent } from '../../stores/aiConsent';
import { useAuth } from '../../stores/auth';
import type { LiveStatus } from './api';

export type StartStep = { kind: 'call' } | { kind: 'ask'; provider: string | null } | { kind: 'none' };

export async function stepAfterCheck(refresh: () => Promise<LiveStatus>): Promise<StartStep> {
  const who = useAuth.getState().username;
  let left = false;
  const onHide = (): void => {
    left = true;
  };
  window.addEventListener('pagehide', onHide);
  try {
    const now = await refresh();
    if (left || !who || useAuth.getState().username !== who || !now.available) return { kind: 'none' };
    return hasConsent(who, 'assistant', now.provider) ? { kind: 'call' } : { kind: 'ask', provider: now.provider };
  } finally {
    window.removeEventListener('pagehide', onHide);
  }
}
