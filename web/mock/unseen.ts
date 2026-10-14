/**
 * A47 — the gateway's mark on a session that stopped working and waits for the
 * person (PROTOCOL §4.4), kept the way the gateway keeps it, so what
 * `npm run dev:mock` shows is what a gateway would.
 *
 * A session works while its state is `running` — a turn is under way; one that
 * only started has done nothing to look at — and waits for the person while it
 * is `needs_approval` or `needs_input`, or `idle` or `readonly` with a control
 * other than `none`: the status dot's amber. The mark
 * is set when a session moves from the one to the other, read from the states
 * alone, and cleared when it works again, when an app says the person has
 * looked (`session.seen`) and when it is archived. Pure, so the rule can be read
 * and tested without a socket; `server.ts` sends the frames.
 */
import type { ControlOwner, Session, SessionState } from '../src/protocol/types';

export const isWorking = (state: SessionState): boolean => state === 'running';

export const isWaiting = (state: SessionState, control: ControlOwner): boolean =>
  state === 'needs_approval' ||
  state === 'needs_input' ||
  ((state === 'idle' || state === 'readonly') && control !== 'none');

/** Every state change goes through here, so the mark follows the move. */
export function moveState(session: Session, state: SessionState): void {
  const was = session.state;
  session.state = state;
  if (isWorking(state)) setMark(session, false);
  else if (isWorking(was) && isWaiting(state, session.control)) setMark(session, true);
}

/**
 * `session.seen`, and an archive: the mark goes. Says whether there was one,
 * because only then does every socket of the account hear about it.
 */
export function clearMark(session: Session): boolean {
  const marked = session.unseen === true;
  setMark(session, false);
  return marked;
}

/** The field travels only while it is true; absent is false (§4.4). */
function setMark(session: Session, marked: boolean): void {
  if (marked) session.unseen = true;
  else delete session.unseen;
}
