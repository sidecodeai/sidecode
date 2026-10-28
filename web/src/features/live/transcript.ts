/**
 * The record of one call: what each side said, in order, and what the assistant sent to a
 * session. The call's view and the saved history share this shape, so a stored call opens in
 * the same list the live one was drawn in. Pure.
 */

export interface Turn {
  kind: 'turn';
  speaker: 'user' | 'assistant';
  text: string;
}

export interface SentMessage {
  kind: 'sent';
  /** The target session's label, as the gateway names it. */
  session: string;
  text: string;
}

export type Entry = Turn | SentMessage;

/**
 * A transcript fragment. It joins the last turn while the same side is still talking; a change
 * of speaker, or a sent message in between, opens a new turn.
 */
export function appendDelta(entries: Entry[], speaker: Turn['speaker'], delta: string): Entry[] {
  const last = entries[entries.length - 1];
  if (last?.kind === 'turn' && last.speaker === speaker) {
    return delta === '' ? entries : [...entries.slice(0, -1), { ...last, text: last.text + delta }];
  }
  // A fragment begins with the space that parted it from the word before, which no turn needs.
  const text = delta.trimStart();
  return text === '' ? entries : [...entries, { kind: 'turn', speaker, text }];
}

export function appendSent(entries: Entry[], session: string, text: string): Entry[] {
  return [...entries, { kind: 'sent', session, text }];
}
