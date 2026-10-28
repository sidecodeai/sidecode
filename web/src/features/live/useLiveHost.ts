import { useEffect } from 'react';
import { useMatch } from 'react-router';
import { setFocus, watchLifecycle } from './liveCall';

/**
 * The call belongs to the page, so nothing here holds it. `App` calls this once, inside the
 * router, and the call learns from it the two things only its surroundings can tell it: which
 * session's chat is on screen, and when the account or the page goes away. Called twice, as
 * StrictMode does, it only installs and removes a listener.
 */
export function useLiveHost(): void {
  const match = useMatch('/sessions/:deviceId/:sessionId');
  const deviceId = match?.params.deviceId ?? null;
  const sessionId = match?.params.sessionId ?? null;

  useEffect(() => watchLifecycle(), []);
  useEffect(() => {
    setFocus(deviceId, sessionId);
  }, [deviceId, sessionId]);
}
