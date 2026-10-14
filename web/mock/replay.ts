/**
 * The two gateway behaviours the mock used to be missing, so that what runs
 * against `npm run dev:mock` can reveal a duplicate send or a resync bug.
 *
 * Both are the contract's, not this file's invention: PROTOCOL §6.2 gives the
 * subscribe reply a bounded replay buffer and a `resync` flag for a cursor the
 * buffer no longer covers, and §8 rule 7 with amendment A12 make a `session.send`
 * idempotent under its request id, which is the only reason a Retry is safe.
 */

/**
 * How many events the mock replays. The real gateway keeps 2 000 events or
 * 4 MiB; this is deliberately small, so a session with any history at all can
 * reach the resync branch in a development run.
 */
export const REPLAY_EVENTS = 200;

/** Whether the buffer still covers `sinceSeq`, or the app must reload history. */
export function needsResync(
  events: readonly { seq: number }[],
  sinceSeq: number | undefined,
  bound: number = REPLAY_EVENTS,
): boolean {
  if (sinceSeq === undefined) return false;
  if (events.length <= bound) return false;
  const oldest = events[events.length - bound];
  if (!oldest) return false;
  // The app asks for everything after `sinceSeq`; the buffer can answer only
  // when its own oldest event is the very next one or older.
  return sinceSeq < oldest.seq - 1;
}

/** The buffered events an app with this cursor is owed. */
export function replayFor<T extends { seq: number }>(
  events: readonly T[],
  sinceSeq: number | undefined,
  bound: number = REPLAY_EVENTS,
): T[] {
  if (sinceSeq === undefined || needsResync(events, sinceSeq, bound)) return [];
  return events.filter((event) => event.seq > sinceSeq);
}

/**
 * What each session has already answered, per request id. A12: a Retry reuses
 * the id, and the device recognises it and answers the same thing rather than
 * giving the agent the message twice.
 */
export class ServedSends {
  private readonly served = new Map<string, Map<string, unknown>>();

  /** The answer this session already gave that request, if it gave one. */
  answerFor(sessionId: string, id: unknown): unknown | undefined {
    return this.served.get(sessionId)?.get(String(id));
  }

  record(sessionId: string, id: unknown, result: unknown): void {
    let session = this.served.get(sessionId);
    if (!session) {
      session = new Map<string, unknown>();
      this.served.set(sessionId, session);
    }
    session.set(String(id), result);
  }
}
