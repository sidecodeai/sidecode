/**
 * A43 — how a device holds queued messages, so the mock holds them the same
 * way (PROTOCOL §6.3). A queue is always in `ts` order, which is the order it is
 * delivered in, and no two entries share a `ts`:
 *
 * - a message sent back with `queue_ts` — an edited one — is held under that
 *   `ts`, in front of the first entry with a greater one, which is the place it
 *   left however the queue moved meanwhile;
 * - any other message joins the end, under the current time or one past the
 *   last entry's `ts`, whichever is later, so two sends in one millisecond still
 *   have an order an edit can go back into.
 */

/** `queue_ts` as §6.3 takes it: absent, a non-negative integer, or refused (`null`). */
export function readQueueTs(value: unknown): number | undefined | null {
  if (value === undefined || value === null) return undefined;
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
}

/** The `ts` a message held now gets: `queue_ts` when it came back with one. */
export function heldTs(
  queue: readonly { ts: number }[],
  queueTs: number | undefined,
  now: number,
): number {
  if (queueTs !== undefined) return queueTs;
  const last = queue.at(-1);
  return last === undefined ? now : Math.max(now, last.ts + 1);
}

/** Hold `entry`, whose `ts` came from `heldTs`, where §6.3 puts it. */
export function hold<T extends { ts: number }>(queue: readonly T[], entry: T): T[] {
  const at = queue.findIndex((held) => held.ts > entry.ts);
  return at === -1 ? [...queue, entry] : [...queue.slice(0, at), entry, ...queue.slice(at)];
}

/** A43: how many files a held message carries, only when it carries any. */
export function filesField(attachments: unknown): { attachments?: number } {
  return Array.isArray(attachments) && attachments.length > 0
    ? { attachments: attachments.length }
    : {};
}
