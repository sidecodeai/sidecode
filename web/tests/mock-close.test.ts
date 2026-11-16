/**
 * A61 — the mock device's answer to a Close on a session a terminal holds,
 * reached from state as it is on a real device: the agent quits after a moment
 * where the device can end it, `unsupported` where it cannot end it precisely
 * or does not list `close_terminal`, and `conflict` where it does not exit in
 * time.
 */
import { describe, expect, it } from 'vitest';
import { GIVE_UP_MS, QUIT_MS, planTerminalClose } from '../mock/closeTerminal';
import { devices, sessions } from '../mock/fixtures';
import type { AgentInfo, Session } from '../src/protocol/types';

const mockSession = (id: string): Session => {
  const found = sessions.find((s) => s.session_id === id);
  if (!found) throw new Error(`no such mock session ${id}`);
  return found;
};

/** The agent entry the mock device holds for a session, as `server.ts` reads it. */
const agentOf = (session: Session): AgentInfo | undefined =>
  devices.find((d) => d.device_id === session.device_id)?.agents.find((a) => a.agent === session.agent);

const plan = (id: string) => {
  const session = mockSession(id);
  return planTerminalClose(session, agentOf(session));
};

describe('closing a session a terminal holds, on the mock device', () => {
  it('lists close_terminal, last, for every agent on the Mac, and for nothing on the CI runner', () => {
    const listing = (deviceId: string) =>
      devices
        .find((d) => d.device_id === deviceId)
        ?.agents.filter((a) => a.capabilities.includes('close_terminal'))
        .map((a) => a.agent);
    expect(listing('dev-mac')).toEqual(['claude', 'codex', 'grok', 'pi']);
    expect(listing('dev-ci')).toEqual([]);
    for (const agent of devices.find((d) => d.device_id === 'dev-mac')?.agents ?? []) {
      expect(agent.capabilities.at(-1), agent.agent).toBe('close_terminal');
    }
  });

  it('quits the agent after a moment', () => {
    expect(plan('ses-shared')).toEqual({ outcome: 'close', afterMs: QUIT_MS });
    expect(plan('ses-grok-shared')).toEqual({ outcome: 'close', afterMs: QUIT_MS });
    expect(plan('ses-grok-terminal')).toEqual({ outcome: 'close', afterMs: QUIT_MS });
  });

  it('refuses an agent that does not list close_terminal', () => {
    expect(plan('ses-attach')).toMatchObject({ outcome: 'refused', refusal: { code: 'unsupported' } });
    expect(plan('ses-codex-terminal')).toMatchObject({
      outcome: 'refused',
      refusal: { code: 'unsupported' },
    });
    expect(plan('ses-grok-no-leader')).toMatchObject({
      outcome: 'refused',
      refusal: { code: 'unsupported' },
    });
  });

  it('refuses a daemon thread whose terminal it cannot tell apart', () => {
    expect(plan('ses-codex-shared')).toMatchObject({
      outcome: 'refused',
      refusal: { code: 'unsupported' },
      afterMs: 0,
    });
  });

  it('answers conflict, after waiting, for an agent that does not exit', () => {
    expect(plan('ses-terminal')).toMatchObject({
      outcome: 'refused',
      refusal: { code: 'conflict' },
      afterMs: GIVE_UP_MS,
    });
  });
});
