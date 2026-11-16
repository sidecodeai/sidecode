/**
 * Close on a session row (`docs/DESIGN.md` § "Close, then the Archive"): when
 * it is offered, what the question before it says, and the line a refusal
 * becomes. A39 closes a session the device drives; A61 adds a session a
 * terminal holds, whose agent the device can quit in that terminal. The
 * request is the same for both, `session.archive` with `archived: true`.
 */
import { errorText } from '../../lib/errors';
import { RequestError } from '../../lib/ws';
import { agentLabel, strings } from '../../strings';
import type { AgentInfo, Device, Session } from '../../protocol/types';

/** Whether a terminal holds the session: its own CLI, or one the device is attached to. */
export const heldByTerminal = (session: Session): boolean =>
  session.control === 'terminal' || session.control === 'shared';

/**
 * A61: whether the device can quit this agent's CLI in the terminal that holds
 * one of its sessions. The device lists `close_terminal` where it can, and one
 * from before A61 does not, so the capability is the only question, never a
 * version. An offline device cannot be asked.
 */
export function canCloseTerminal(agent: AgentInfo | null | undefined, online: boolean): boolean {
  return online && (agent?.capabilities.includes('close_terminal') ?? false);
}

/**
 * Whether the row offers Close: a session the device drives (A39), or one a
 * terminal holds when its agent can be quit there (A61). A row in the Archive
 * offers none: writing to it is what brings it back.
 */
export function offersClose(session: Session, device: Device): boolean {
  if (session.archived) return false;
  if (session.control === 'remote') return true;
  if (!heldByTerminal(session)) return false;
  return canCloseTerminal(
    device.agents.find((agent) => agent.agent === session.agent),
    device.online,
  );
}

/**
 * What Close asks before it goes, one sentence a paragraph, or nothing for a
 * session the device drives that is idle, which closes on the tap. A terminal
 * is always warned about, because what is typed there and not sent goes with
 * it; the work an agent has not finished is told while it is working.
 */
export function closeQuestion(session: Session, deviceName: string, working: boolean): string[] {
  const said: string[] = [];
  if (heldByTerminal(session)) {
    said.push(strings.sessions.closeTerminalBody(agentLabel(session.agent), deviceName));
  }
  if (working) said.push(strings.sessions.closeBody);
  return said;
}

/**
 * The one line under the question after a refusal. `unsupported` is a session
 * the device cannot end precisely and `conflict` one it could not end in time,
 * so each has a sentence of its own; every other code keeps the app's words for
 * it — offline, timeout, or the device's own.
 */
export function closeFailure(error: unknown): string {
  if (error instanceof RequestError) {
    if (error.code === 'unsupported') return strings.sessions.closeUnsupported;
    if (error.code === 'conflict') return strings.sessions.busy;
  }
  return errorText(error);
}
