import { useEffect } from 'react';
import { useInFront } from '../../lib/useInFront';
import { useConnection } from '../../stores/connection';
import { useFront } from '../../stores/front';
import { sessionKey, useSessions } from '../../stores/sessions';

/**
 * A47 (docs/DESIGN.md § "A red dot for a session that stopped and waits for
 * you"): while this page is in front of the person, the conversation it shows
 * is the one they are looking at. It is published as that — its row draws no
 * dot and the app's icon does not count it — and `session.seen` goes for it:
 * when it opens on a page in front, when the page comes to the front with it
 * open, when a `session.updated` marks it while it is on screen, and when the
 * socket comes back with it marked. The store sends only while its copy says
 * `unseen`, so each of those moments costs one request at most.
 */
export function useFrontConversation(deviceId: string, sessionId: string): void {
  const key = sessionKey(deviceId, sessionId);
  const inFront = useInFront();
  const unseen = useSessions((s) => s.sessions[key]?.unseen === true);
  const connected = useConnection((s) => s.status === 'open');
  const markSeen = useSessions((s) => s.markSeen);

  useEffect(() => {
    if (!inFront) return;
    useFront.setState({ key });
    return () => useFront.setState({ key: null });
  }, [inFront, key]);

  useEffect(() => {
    if (inFront && unseen && connected) markSeen(deviceId, sessionId);
  }, [deviceId, sessionId, inFront, unseen, connected, markSeen]);
}
