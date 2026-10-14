/**
 * The two gateway behaviours the development mock used to be missing, so that
 * nothing run against `npm run dev:mock` could reveal a duplicate send or a
 * resync bug: the bounded replay buffer of `session.subscribe` (PROTOCOL §6.2)
 * and the idempotency of `session.send` under its request id (§8 rule 7, A12).
 */
import { describe, expect, it } from 'vitest';
import { REPLAY_EVENTS, ServedSends, needsResync, replayFor } from '../mock/replay';

const events = (count: number): { seq: number }[] =>
  Array.from({ length: count }, (_, i) => ({ seq: i + 1 }));

describe('the replay bound', () => {
  it('answers a cursor the buffer still covers', () => {
    const all = events(REPLAY_EVENTS + 10);

    expect(needsResync(all, all.length - 5)).toBe(false);
    expect(replayFor(all, all.length - 5)).toHaveLength(5);
  });

  it('asks for a resync when the cursor has fallen out of the buffer', () => {
    const all = events(REPLAY_EVENTS + 10);

    expect(needsResync(all, 1)).toBe(true);
    // A resync carries no events: the app blanks the timeline and reloads.
    expect(replayFor(all, 1)).toEqual([]);
  });

  it('never resyncs a session shorter than the bound, or a fresh subscribe', () => {
    expect(needsResync(events(20), 1)).toBe(false);
    expect(needsResync(events(REPLAY_EVENTS + 10), undefined)).toBe(false);
    expect(replayFor(events(20), undefined)).toEqual([]);
  });
});

describe('a resent request id', () => {
  it('is answered with what it was answered before', () => {
    const served = new ServedSends();
    expect(served.answerFor('ses-1', 'req-1')).toBeUndefined();

    served.record('ses-1', 'req-1', { accepted: 'sent' });

    expect(served.answerFor('ses-1', 'req-1')).toEqual({ accepted: 'sent' });
    // Per session: a different conversation has answered nothing yet.
    expect(served.answerFor('ses-2', 'req-1')).toBeUndefined();
  });
});
