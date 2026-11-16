/**
 * `docs/DESIGN.md` § "Reading position": a conversation opens on one of
 * the person's own messages — their newest when they wrote since they left,
 * otherwise the one nearest above where they were reading — and on the tail
 * only when none of theirs can be found within the newest page and two older
 * ones.
 */
import { describe, expect, it } from 'vitest';
import { chooseAnchor, ownMessages, type OwnMessage } from '../src/features/chat/anchor';
import { applyEvent, emptyTimeline, mergeNewestPage } from '../src/stores/timeline';
import type { SessionEvent, Trigger } from '../src/protocol/types';

const mine = (...seqs: number[]): OwnMessage[] => seqs.map((seq) => ({ key: `u${seq}`, seq }));

const row = (key: string) => ({ kind: 'row', key });
const TAIL = { kind: 'tail' };
const OLDER = { kind: 'older' };

describe('choosing the anchor', () => {
  /** Read up to row 40 and left while the app held events up to 60. */
  const saved = { seq: 40, held: 60 };

  describe('rule 1: they wrote since they left', () => {
    it('opens on their newest message, written after what the app held', () => {
      expect(
        chooseAnchor({ messages: mine(10, 30, 50, 70), saved, from: 1, canLookOlder: false }),
      ).toEqual(row('u70'));
    });

    it('does not count a message they already had, below where they were reading', () => {
      // 50 is after the row they were reading but was there when they left.
      expect(
        chooseAnchor({ messages: mine(10, 30, 50), saved, from: 1, canLookOlder: false }),
      ).toEqual(row('u30'));
    });

    it('looks in an older page while rows after what was held are unread', () => {
      // Held from 200: a message of theirs between 61 and 199 would be rule 1's.
      expect(chooseAnchor({ messages: [], saved, from: 200, canLookOlder: true })).toEqual(OLDER);
    });
  });

  describe('rule 2: their message nearest above where they were reading', () => {
    it('opens on the newest of their messages at or before the row they were reading', () => {
      expect(
        chooseAnchor({ messages: mine(10, 30, 40), saved, from: 1, canLookOlder: false }),
      ).toEqual(row('u40'));
    });

    it('brings back the older turn they had scrolled into', () => {
      expect(
        chooseAnchor({
          messages: mine(10, 30, 80),
          saved: { seq: 15, held: 100 },
          from: 1,
          canLookOlder: false,
        }),
      ).toEqual(row('u10'));
    });

    it('looks in an older page when nothing of theirs at or before it is held', () => {
      expect(chooseAnchor({ messages: mine(50), saved, from: 35, canLookOlder: true })).toEqual(
        OLDER,
      );
      expect(chooseAnchor({ messages: mine(50), saved, from: 35, canLookOlder: false })).toEqual(
        TAIL,
      );
    });
  });

  describe('rule 3: never read on this device', () => {
    it('opens on their newest message', () => {
      expect(
        chooseAnchor({ messages: mine(70, 10, 50), saved: null, from: 1, canLookOlder: true }),
      ).toEqual(row('u70'));
    });
  });

  describe('rule 4: nothing of theirs to be found', () => {
    it('asks for an older page while one may be read', () => {
      expect(chooseAnchor({ messages: [], saved: null, from: 500, canLookOlder: true })).toEqual(
        OLDER,
      );
    });

    it('opens on the tail once no page is left to look in', () => {
      expect(chooseAnchor({ messages: [], saved: null, from: 500, canLookOlder: false })).toEqual(
        TAIL,
      );
      expect(chooseAnchor({ messages: [], saved, from: 500, canLookOlder: false })).toEqual(TAIL);
    });

    it('opens on the tail of a conversation with no rows at all', () => {
      expect(chooseAnchor({ messages: [], saved: null, from: null, canLookOlder: false })).toEqual(
        TAIL,
      );
    });
  });
});

describe('the person’s own messages', () => {
  const message = (
    seq: number,
    source: Trigger,
    delivery?: string,
  ): SessionEvent =>
    ({
      seq,
      ts: seq,
      kind: 'user_message',
      block_id: `u${seq}`,
      text: `message ${seq}`,
      source,
      ...(delivery === undefined ? {} : { delivery }),
    }) as SessionEvent;

  const text = (seq: number): SessionEvent =>
    ({ seq, ts: seq, kind: 'assistant_text', block_id: `a${seq}`, text: 'ok', done: true }) as SessionEvent;

  it('are the messages typed here, in a terminal, or sent from the queue', () => {
    const state = [message(1, 'remote'), text(2), message(3, 'terminal'), message(4, 'queue')].reduce(
      applyEvent,
      emptyTimeline(),
    );
    expect(ownMessages(state)).toEqual(mine(1, 3, 4));
  });

  it('leave out what another agent filed (A34)', () => {
    const state = [message(1, 'remote'), message(2, 'agent')].reduce(applyEvent, emptyTimeline());
    expect(ownMessages(state)).toEqual(mine(1));
  });

  it('leave out a message that did not reach the agent', () => {
    const state = [
      message(1, 'remote', 'delivered'),
      message(2, 'remote', 'absorbed'),
      message(3, 'remote', 'pending'),
    ].reduce(applyEvent, emptyTimeline());
    expect(ownMessages(state)).toEqual(mine(1));
  });

  it('leave out a send not confirmed yet, which is no row', () => {
    const state = {
      ...applyEvent(emptyTimeline(), message(1, 'remote')),
      optimistic: [{ id: 'pending-send', text: 'on its way', attachments: [], at: 5 }],
    };
    expect(ownMessages(state)).toEqual(mine(1));
  });

  it('stand where the block began', () => {
    const state = mergeNewestPage(emptyTimeline(), [
      { ...message(9, 'remote'), first_seq: 4 } as SessionEvent,
    ]);
    expect(ownMessages(state)).toEqual([{ key: 'u9', seq: 4 }]);
  });
});
