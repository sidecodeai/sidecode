/**
 * Where a conversation opens (`docs/DESIGN.md` § "Reading position"): on one of
 * the person's own messages, read from the rows this app holds and the reading
 * position it saved for the conversation.
 * Pure; `useOpening` reads the timeline into it and the scroller places what it
 * answers.
 */
import type { TimelineState } from '../../stores/timeline';

/** One of the person's delivered messages: its row and where it stands. */
export interface OwnMessage {
  key: string;
  /** The row's place in the transcript, `first_seq ?? seq`. */
  seq: number;
}

/** A row to put at the top, the tail as before, or an older page to read first. */
export type AnchorChoice = { kind: 'row'; key: string } | { kind: 'tail' } | { kind: 'older' };

export interface AnchorInput {
  /** The person's delivered messages among the rows held, in any order. */
  messages: readonly OwnMessage[];
  /** The furthest row read on this device, or null when it was never read here. */
  position: number | null;
  /** The lowest `seq` the rows held reach, or null when none are held. */
  from: number | null;
  /** Whether an older page may still be read to look further back. */
  canLookOlder: boolean;
}

/**
 * The person's own messages in the rows held: a `user_message` nobody else
 * filed (A34 puts another agent's words on the agent's side) that reached the
 * agent — not one the device still holds back (`delivery: "pending"` from a
 * device before A19) or one the CLI absorbed to send again. A send this app has
 * not had confirmed is no row yet.
 */
export function ownMessages(state: TimelineState): OwnMessage[] {
  const found: OwnMessage[] = [];
  for (const key of state.order) {
    const item = state.items[key];
    const event = item?.event;
    if (!item || event?.kind !== 'user_message' || event.source === 'agent') continue;
    const delivery: string | undefined = event.delivery;
    if (delivery === undefined || delivery === 'delivered') found.push({ key, seq: item.seq });
  }
  return found;
}

function newest(messages: readonly OwnMessage[]): AnchorChoice | null {
  let best: OwnMessage | null = null;
  for (const message of messages) {
    if (best === null || message.seq > best.seq) best = message;
  }
  return best === null ? null : { kind: 'row', key: best.key };
}

/**
 * The anchor, rule by rule:
 *
 * 1. a message of theirs after the saved position — they wrote since, so the
 *    position is stale — gives the newest such message;
 * 2. otherwise, with a saved position, the newest of their messages at or
 *    before it;
 * 3. never read on this device, their newest message;
 * 4. none of theirs among the rows held, while an older page may still hold
 *    one, asks for that page first; and with no page left to read, the tail.
 *
 * Rule 1 cannot be ruled out while rows between the position and the held ones
 * are unread, so those are looked through first.
 */
export function chooseAnchor({ messages, position, from, canLookOlder }: AnchorInput): AnchorChoice {
  const further: AnchorChoice = canLookOlder ? { kind: 'older' } : { kind: 'tail' };
  if (position !== null) {
    const wrote = newest(messages.filter((message) => message.seq > position));
    if (wrote) return wrote;
    if (from === null || from > position + 1) return further;
    return newest(messages.filter((message) => message.seq <= position)) ?? further;
  }
  return newest(messages) ?? further;
}
