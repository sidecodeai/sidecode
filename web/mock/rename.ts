/**
 * A55 — the rules the mock device follows when an app renames a session
 * (`session.set` with `title`), so what `npm run dev:mock` does can be read
 * without a socket: the normalisation only the device owns, the name an agent
 * ends up holding, and the states in which the device refuses.
 *
 * Both refusals an app has words for come from state, as they do on a real
 * device, and not from a name that is typed:
 *
 * - `unsupported` from a Claude session open in a terminal the device cannot
 *   reach ("Nightly perf sweep", "Refactor relay routing"), a Codex thread
 *   with neither a daemon nor a runner ("Bisect the ingest regression") or an
 *   archived one ("Drop the legacy ingest path"), and an agent that does not
 *   list `rename` (the Grok Build on the device whose leader is off);
 * - `conflict` from a Claude session the device types into while its
 *   terminal is busy. "Wire the channel shim" is idle until a message is sent
 *   into it, and then waits on an approval, which keeps it busy until it is
 *   answered.
 *
 * These are pure functions; `server.ts` holds the timers.
 */
import type { AgentInfo, Session } from '../src/protocol/types';
import { TERMINAL_BUSY, terminalBusy, typedSession } from './typing';

/** §6.3: the longest name the device keeps, in code points. */
export const TITLE_LIMIT = 60;

/**
 * §6.3: every character of the categories Cc, Cs, Zl and Zp becomes a space, and
 * so does one of Cf — except U+200C and U+200D, which join emoji sequences and
 * are kept. Runs of white space then collapse to one space and both ends are
 * trimmed. There is no NFKC rewrite.
 */
export const normalizeTitle = (raw: string): string =>
  raw
    .replace(/[\p{Cc}\p{Cs}\p{Zl}\p{Zp}]|(?![\u200C\u200D])\p{Cf}/gu, ' ')
    .split(/\s+/)
    .filter((word) => word !== '')
    .join(' ');

export interface Refusal {
  code: 'bad_request' | 'conflict' | 'unsupported';
  message: string;
}

/** What a device answers when it cannot keep a name, in plain English. */
const bad = (message: string): Refusal => ({ code: 'bad_request', message });
const unsupported = (message: string): Refusal => ({ code: 'unsupported', message });

/** `bad_request` for a name that is nothing, or too long, once normalised. */
function nameRefusal(title: string): Refusal | null {
  if (title === '') return bad('title must not be empty');
  if ([...title].length > TITLE_LIMIT) {
    return bad(`title is too long (max ${TITLE_LIMIT} characters)`);
  }
  return null;
}

/**
 * The states of §6.3's route table in which the device cannot name the session
 * from here: `unsupported` where there is no route at all, and `conflict` where
 * the route is a keystroke into a terminal that is busy and nothing was typed.
 */
export function renameRefusal(session: Session, agent: AgentInfo | undefined): Refusal | null {
  if (agent?.capabilities.includes('rename') !== true) {
    return unsupported('this agent cannot be renamed from an app');
  }
  if (session.agent === 'claude' && session.control === 'terminal') {
    return unsupported(
      'the session is open in a terminal the device cannot reach; rename it there',
    );
  }
  if (session.agent === 'codex') {
    if (session.archived) return unsupported('the thread is archived in Codex');
    // `thread/name/set` needs the daemon unless the device runs the thread.
    if (session.control !== 'remote' && agent.attach_ready !== true) {
      return unsupported('the Codex daemon is not running');
    }
  }
  if (typedSession(session, agent) && terminalBusy(session)) {
    return { code: 'conflict', message: TERMINAL_BUSY };
  }
  return null;
}

/**
 * Claude Code keeps a name unique among the sessions it has live on a machine:
 * a name another one holds gets a suffix, and the reply carries what the agent
 * now holds, which is why an app draws the reply's title and not its own.
 */
export function heldName(title: string, session: Session, all: readonly Session[]): string {
  if (session.agent !== 'claude') return title;
  const taken = new Set(
    all
      .filter(
        (other) =>
          other !== session &&
          other.agent === 'claude' &&
          other.device_id === session.device_id &&
          other.control !== 'none' &&
          !other.archived,
      )
      .map((other) => other.title),
  );
  let name = title;
  for (let n = 2; taken.has(name); n += 1) name = `${title} (${n})`;
  return name;
}

/** What a `session.set` frame asks for in its `title`, once the device has read it. */
export type RenamePlan =
  | { outcome: 'none' }
  | { outcome: 'same' }
  | { outcome: 'refused'; refusal: Refusal }
  | { outcome: 'rename'; title: string };

/**
 * Reads the `title` of a frame. A name equal to the one the session holds is
 * answered at once (`same`), before the states that would refuse a change are
 * looked at; any other is refused for what it is, then for where it is aimed,
 * and otherwise becomes the name the agent will hold.
 */
export function planRename(
  session: Session,
  agent: AgentInfo | undefined,
  frame: Record<string, unknown>,
  all: readonly Session[],
): RenamePlan {
  if (!('title' in frame)) return { outcome: 'none' };
  if (typeof frame.title !== 'string') {
    return { outcome: 'refused', refusal: bad('title must be a string') };
  }
  const title = normalizeTitle(frame.title);
  const refusal = nameRefusal(title);
  if (refusal) return { outcome: 'refused', refusal };
  if (title === session.title) return { outcome: 'same' };
  const blocked = renameRefusal(session, agent);
  if (blocked) return { outcome: 'refused', refusal: blocked };
  return { outcome: 'rename', title: heldName(title, session, all) };
}
