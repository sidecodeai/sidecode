/**
 * `docs/DESIGN.md` § "Reading position": a conversation opens on one of
 * the person's own messages, chosen from the rows held and the reading position
 * saved for it, and on the tail only when none of theirs can be found within
 * the newest page and two older ones.
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
  describe('rule 1: they wrote after the saved position', () => {
    it('opens on the newest message after it', () => {
      expect(
        chooseAnchor({ messages: mine(10, 50, 70), position: 40, from: 1, canLookOlder: false }),
      ).toEqual(row('u70'));
    });

    it('opens on it even when it is the only one', () => {
      expect(
        chooseAnchor({ messages: mine(10, 50), position: 49, from: 1, canLookOlder: true }),
      ).toEqual(row('u50'));
    });
  });

  describe('rule 2: nothing of theirs after the saved position', () => {
    it('opens on the newest of their messages at or before it', () => {
      expect(
        chooseAnchor({ messages: mine(10, 30, 40), position: 60, from: 1, canLookOlder: false }),
      ).toEqual(row('u40'));
    });

    it('counts a message right at the position', () => {
      expect(
        chooseAnchor({ messages: mine(10, 60), position: 60, from: 1, canLookOlder: false }),
      ).toEqual(row('u60'));
    });

    it('looks in an older page while the rows held do not reach back to the position', () => {
      // Held from 200: a message of theirs between 41 and 199 would be rule 1's.
      expect(
        chooseAnchor({ messages: [], position: 40, from: 200, canLookOlder: true }),
      ).toEqual(OLDER);
    });

    it('looks in an older page when nothing of theirs at or before it is held', () => {
      // Held from 50, so nothing after the position is unread, and nothing of theirs is held.
      expect(
        chooseAnchor({ messages: [], position: 60, from: 50, canLookOlder: true }),
      ).toEqual(OLDER);
      expect(
        chooseAnchor({ messages: [], position: 60, from: 50, canLookOlder: false }),
      ).toEqual(TAIL);
    });
  });

  describe('rule 3: never read on this device', () => {
    it('opens on their newest message', () => {
      expect(
        chooseAnchor({ messages: mine(70, 10, 50), position: null, from: 1, canLookOlder: true }),
      ).toEqual(row('u70'));
    });
  });

  describe('rule 4: nothing of theirs to be found', () => {
    it('asks for an older page while one may be read', () => {
      expect(chooseAnchor({ messages: [], position: null, from: 500, canLookOlder: true })).toEqual(
        OLDER,
      );
    });

    it('opens on the tail once no page is left to look in', () => {
      expect(chooseAnchor({ messages: [], position: null, from: 500, canLookOlder: false })).toEqual(
        TAIL,
      );
      expect(chooseAnchor({ messages: [], position: 40, from: 500, canLookOlder: false })).toEqual(
        TAIL,
      );
    });

    it('opens on the tail of a conversation with no rows at all', () => {
      expect(chooseAnchor({ messages: [], position: null, from: null, canLookOlder: false })).toEqual(
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
