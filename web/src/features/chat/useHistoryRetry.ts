import { useEffect, useRef, useState } from 'react';

/**
 * How long to wait before each automatic retry of a failed load while the
 * device is online: a pause, not a hammer. After the last one the app waits for
 * the device to return, or for the person to press Retry.
 */
export const RETRY_DELAYS_MS = [2_000, 4_000, 8_000, 15_000, 15_000];

/**
 * Asks again for a conversation that could not be read, by itself: at once when
 * its device comes back online — after a gateway restart that is the moment
 * the answer exists — and after a short, growing pause while the device is
 * online and the load keeps failing. An offline device is not asked at all.
 */
export function useHistoryRetry(
  key: string,
  failed: boolean,
  deviceOnline: boolean,
  retry: (key: string) => void,
): void {
  const [episode, setEpisode] = useState({ key, failed, deviceOnline, attempt: 0 });
  // A new failure, another conversation (the page keeps one instance across a
  // move between them), or the device coming or going starts the schedule over.
  if (
    episode.key !== key ||
    episode.failed !== failed ||
    episode.deviceOnline !== deviceOnline
  ) {
    setEpisode({ key, failed, deviceOnline, attempt: 0 });
  }

  // A device is back only if it was this conversation's device that was away.
  const seen = useRef({ key, deviceOnline });
  useEffect(() => {
    const returned = seen.current.key === key && deviceOnline && !seen.current.deviceOnline;
    seen.current = { key, deviceOnline };
    if (returned && failed) retry(key);
  }, [deviceOnline, failed, key, retry]);

  useEffect(() => {
    if (!episode.failed || !episode.deviceOnline) return;
    const delay = RETRY_DELAYS_MS[episode.attempt];
    if (delay === undefined) return;
    const timer = setTimeout(() => {
      retry(key);
      setEpisode((current) => ({ ...current, attempt: current.attempt + 1 }));
    }, delay);
    return () => clearTimeout(timer);
  }, [episode, key, retry]);
}
