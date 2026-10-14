/**
 * A34 — words another agent put into the conversation: a teammate session's
 * report, a background task's notification. Claude Code files them as user
 * turns, but nobody typed them, so they are not the person's side of the
 * conversation: they sit on the left with the agent's own output, as a muted
 * block on the quiet surface captioned "from another agent".
 * `docs/DESIGN.md` § "The timeline".
 *
 * The device has already reduced the text to who reported and what they said,
 * with the envelope and every `<system-reminder>` removed (A30), so the row
 * prints exactly what it was given.
 */
import { strings } from '../../../strings';
import type { UserMessageEvent } from '../../../protocol/types';

export function AgentMessageRow({ event }: { event: UserMessageEvent }) {
  return (
    <div className="agent-message">
      <span className="agent-message-origin">{strings.chat.fromAgent}</span>
      <p>{event.text}</p>
    </div>
  );
}
