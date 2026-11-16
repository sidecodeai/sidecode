/**
 * A61 — what the mock device answers when an app closes a session a terminal
 * holds (`session.archive {archived: true}` on a `terminal` or `shared` row),
 * so what `npm run dev:mock` does can be read without a socket. A real device
 * quits the agent's CLI in that terminal, waits at most 10 s for it to exit,
 * and only then closes the session as A39 closes one; the mock takes a moment
 * where a CLI would.
 *
 * Claude Code and Codex list `close_terminal` on the Mac. Grok Build and pi
 * list it only once `protocol/fixtures/objects/agent.grok.json` and
 * `agent.pi.json` do, because `tests/agents.test.tsx` holds the mock's two
 * agents to those files.
 *
 * Both refusals an app has words for come from state, as they do on a device:
 *
 * - `unsupported` from an agent that does not list `close_terminal` — every
 *   agent on the CI runner, whose client predates A61 — and from "Typecheck the
 *   web app", a thread on the shared Codex daemon whose folder has two Codex
 *   terminals open, so the device cannot tell which one holds it;
 * - `conflict` from "Refactor relay routing", whose `claude` does not exit in
 *   time. The device then publishes nothing, and the row stays as it was.
 *
 * Pure; `server.ts` holds the timers.
 */
import type { AgentInfo, Session } from '../src/protocol/types';
import type { Refusal } from './rename';

/** How long a CLI takes to quit in the mock, where a real one takes a moment. */
export const QUIT_MS = 1_200;

/** How long the mock waits for a CLI that does not quit; a device waits 10 s. */
export const GIVE_UP_MS = 3_000;

/** The thread on the shared daemon whose terminal the device cannot tell apart. */
const AMBIGUOUS = 'ses-codex-shared';

/** The `claude` that does not exit when it is told to. */
const STUBBORN = 'ses-terminal';

export type TerminalClose =
  | { outcome: 'close'; afterMs: number }
  | { outcome: 'refused'; refusal: Refusal; afterMs: number };

/** What the device does with a Close on a session a terminal holds. */
export function planTerminalClose(session: Session, agent: AgentInfo | undefined): TerminalClose {
  if (agent?.capabilities.includes('close_terminal') !== true) {
    return {
      outcome: 'refused',
      refusal: { code: 'unsupported', message: 'this agent cannot be quit in its terminal from an app' },
      afterMs: 0,
    };
  }
  if (session.session_id === AMBIGUOUS) {
    return {
      outcome: 'refused',
      refusal: {
        code: 'unsupported',
        message: 'more than one Codex terminal is open in this folder; close it in its terminal',
      },
      afterMs: 0,
    };
  }
  if (session.session_id === STUBBORN) {
    return {
      outcome: 'refused',
      refusal: { code: 'conflict', message: 'the agent did not exit in time' },
      afterMs: GIVE_UP_MS,
    };
  }
  return { outcome: 'close', afterMs: QUIT_MS };
}
