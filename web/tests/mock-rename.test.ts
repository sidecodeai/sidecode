/**
 * A55 — the rules the mock device applies to `session.set` with a `title`
 * (§6.3), so what `npm run dev:mock` does can be read without a socket: the
 * normalisation, the name the agent ends up holding, and the two refusals an
 * app has words for, reached from state as they are on a real device.
 */
import { describe, expect, it } from 'vitest';
import {
  TITLE_LIMIT,
  heldName,
  normalizeTitle,
  planRename,
  renameRefusal,
} from '../mock/rename';
import { TERMINAL_BUSY } from '../mock/typing';
import {
  claudeAgent,
  claudeNoShim,
  codexAgent,
  codexNoDaemon,
  devices,
  grokAgent,
  grokNoLeader,
  piAgent,
  sessions,
} from '../mock/fixtures';
import type { AgentInfo, Session } from '../src/protocol/types';

const mockSession = (id: string): Session => {
  const found = sessions.find((s) => s.session_id === id);
  if (!found) throw new Error(`no such mock session ${id}`);
  return found;
};

/** The agent entry the mock device holds for a session, as `server.ts` reads it. */
const agentOf = (session: Session): AgentInfo | undefined =>
  devices.find((d) => d.device_id === session.device_id)?.agents.find((a) => a.agent === session.agent);

describe('normalising a name', () => {
  it('turns control, format, surrogate, line and paragraph separator characters into spaces', () => {
    // Cc: tab, newline, escape, NEL. Cf: zero-width space, a bidi mark, a soft
    // hyphen, the byte order mark. Cs: a lone surrogate. Zl, Zp.
    expect(normalizeTitle('a\tb\nc\u001bd\u0085e')).toBe('a b c d e');
    expect(normalizeTitle('a\u200bb\u200ec\u00add\ufeffe')).toBe('a b c d e');
    expect(normalizeTitle('a\ud800b\udfffc')).toBe('a b c');
    expect(normalizeTitle('a\u2028b\u2029c')).toBe('a b c');
  });

  it('keeps the two format characters that join emoji sequences', () => {
    expect(normalizeTitle('👩\u200d💻')).toBe('👩\u200d💻');
    expect(normalizeTitle('👩\u200d💻 at work')).toBe('👩\u200d💻 at work');
    expect(normalizeTitle('a\u200cb')).toBe('a\u200cb');
    // Beside the ones it does not keep, they are still what they were.
    expect(normalizeTitle('\u200b👩\u200d💻\u200b')).toBe('👩\u200d💻');
  });

  it('collapses runs of white space to one space and trims both ends', () => {
    expect(normalizeTitle('  Fix   the \t flaky\n\n test  ')).toBe('Fix the flaky test');
    expect(normalizeTitle('a\u00a0\u3000b')).toBe('a b');
  });

  it('does not rewrite what is not white space or invisible, NFKC included', () => {
    expect(normalizeTitle('Ａ１ ﬁle')).toBe('Ａ１ ﬁle');
    expect(normalizeTitle('修复  认证\u200b令牌')).toBe('修复 认证 令牌');
  });

  it('leaves nothing of a name made only of those characters', () => {
    expect(normalizeTitle(' \u200b\t\n ')).toBe('');
    expect(normalizeTitle('\u200b')).toBe('');
    expect(normalizeTitle('\ufeff')).toBe('');
    expect(normalizeTitle('\u0085')).toBe('');
    expect(normalizeTitle('\u200b\ufeff\u0085\u00ad\ud800')).toBe('');
  });
});

describe('reading the title of a frame', () => {
  const push = mockSession('ses-push');
  const plan = (title: unknown, session: Session = push, all: readonly Session[] = sessions) =>
    planRename(session, agentOf(session), { title }, all);

  it('has nothing to do for a frame that carries none', () => {
    expect(planRename(push, agentOf(push), { model: 'claude-haiku-4-5' }, sessions)).toEqual({
      outcome: 'none',
    });
  });

  it('keeps the name the device normalised', () => {
    expect(plan('  Push   tokens\n')).toEqual({ outcome: 'rename', title: 'Push tokens' });
  });

  it('keeps the joiners of an emoji sequence in the name', () => {
    expect(plan('👩\u200d💻 pairing')).toEqual({ outcome: 'rename', title: '👩\u200d💻 pairing' });
  });

  it('answers a name equal to the current one at once, before any refusal is looked at', () => {
    expect(plan(push.title)).toEqual({ outcome: 'same' });
    expect(plan(` ${push.title}  `)).toEqual({ outcome: 'same' });
    // Even where nothing could rename it: a Claude terminal the device cannot reach.
    const unreachable = mockSession('ses-attach');
    expect(plan(unreachable.title, unreachable)).toEqual({ outcome: 'same' });
  });

  it('refuses an empty name, and one that is empty once normalised, with bad_request', () => {
    for (const title of ['', '   ', '\u200b', '\t\n', '\ufeff', '\u0085', '\u200b\ufeff\u0085']) {
      expect(plan(title)).toEqual({
        outcome: 'refused',
        refusal: { code: 'bad_request', message: 'title must not be empty' },
      });
    }
  });

  it('refuses more than 60 code points with bad_request, and allows exactly 60', () => {
    expect(TITLE_LIMIT).toBe(60);
    expect(plan('a'.repeat(61))).toEqual({
      outcome: 'refused',
      refusal: { code: 'bad_request', message: 'title is too long (max 60 characters)' },
    });
    expect(plan('😀'.repeat(61))).toMatchObject({ outcome: 'refused' });
    expect(plan('😀'.repeat(60))).toEqual({ outcome: 'rename', title: '😀'.repeat(60) });
    expect(plan('字'.repeat(60))).toMatchObject({ outcome: 'rename' });
  });

  it('counts the length after the white space has collapsed', () => {
    expect(plan(`${'a'.repeat(30)}${' '.repeat(40)}${'b'.repeat(29)}`)).toMatchObject({
      outcome: 'rename',
    });
  });

  it('refuses a title that is not text', () => {
    expect(plan(42)).toMatchObject({ outcome: 'refused', refusal: { code: 'bad_request' } });
    expect(plan(null)).toMatchObject({ outcome: 'refused', refusal: { code: 'bad_request' } });
  });
});

describe('unsupported: where the device has no way to name the session', () => {
  it('is a live Claude the device cannot reach: a terminal holds it and nothing is attached', () => {
    for (const id of ['ses-attach', 'ses-terminal']) {
      const session = mockSession(id);
      expect(session.agent).toBe('claude');
      expect(session.control).toBe('terminal');
      expect(renameRefusal(session, agentOf(session))).toMatchObject({ code: 'unsupported' });
    }
  });

  it('is a Codex thread with neither a daemon nor a runner, or one that is archived', () => {
    const noDaemon = mockSession('ses-codex-terminal');
    expect(agentOf(noDaemon)?.attach_ready).toBe(false);
    expect(renameRefusal(noDaemon, agentOf(noDaemon))).toMatchObject({
      code: 'unsupported',
      message: 'the Codex daemon is not running',
    });

    const archived = mockSession('ses-archived');
    expect(renameRefusal(archived, agentOf(archived))).toMatchObject({
      code: 'unsupported',
      message: 'the thread is archived in Codex',
    });
    // Even where the daemon runs.
    expect(renameRefusal({ ...archived, device_id: 'dev-mac' }, codexAgent)).toMatchObject({
      code: 'unsupported',
    });
  });

  it('is an agent that does not list rename: Grok Build while its leader is off', () => {
    const grok = mockSession('ses-grok-no-leader');
    expect(agentOf(grok)).toEqual(expect.objectContaining({ agent: 'grok', attach_ready: false }));
    expect(renameRefusal(grok, agentOf(grok))).toMatchObject({ code: 'unsupported' });
    expect(renameRefusal(grok, undefined)).toMatchObject({ code: 'unsupported' });
  });

  it('is nothing else the fixtures hold', () => {
    const refused = sessions
      .filter((s) => renameRefusal(s, agentOf(s)) !== null)
      .map((s) => s.session_id)
      .sort();

    expect(refused).toEqual(
      [
        'ses-archived',
        'ses-attach',
        'ses-codex-terminal',
        'ses-grok-no-leader',
        'ses-terminal',
      ].sort(),
    );
  });

  it('is reached as a plan for such a session, whatever the name', () => {
    const session = mockSession('ses-attach');
    expect(planRename(session, agentOf(session), { title: 'Anything new' }, sessions)).toEqual({
      outcome: 'refused',
      refusal: {
        code: 'unsupported',
        message: 'the session is open in a terminal the device cannot reach; rename it there',
      },
    });
  });
});

describe('conflict: a busy terminal the device would have to type into', () => {
  const shared = mockSession('ses-shared');

  it('is a Claude session the device types into while its turn runs', () => {
    expect(agentOf(shared)?.attach).toBe('channel');
    expect(renameRefusal(shared, agentOf(shared))).toBeNull();

    for (const busy of [
      { state: 'running' as const },
      { state: 'needs_approval' as const },
      { state: 'needs_input' as const },
      { turn: { turn_id: 't1', started_at: 1 } },
    ]) {
      expect(renameRefusal({ ...shared, ...busy }, agentOf(shared))).toEqual({
        code: 'conflict',
        message: TERMINAL_BUSY,
      });
    }
  });

  it('is not a Codex or Grok session in a terminal: those are a method call, which lands', () => {
    const codexShared = mockSession('ses-codex-shared');
    expect(codexShared.state).toBe('running');
    expect(renameRefusal(codexShared, agentOf(codexShared))).toBeNull();
    const grokShared = mockSession('ses-grok-shared');
    expect(grokShared.state).toBe('running');
    expect(renameRefusal(grokShared, agentOf(grokShared))).toBeNull();
  });

  it('is not a session the device drives itself, however busy', () => {
    const flaky = mockSession('ses-flaky');
    expect(flaky.state).toBe('running');
    expect(renameRefusal(flaky, agentOf(flaky))).toBeNull();
  });
});

describe('which agents list rename', () => {
  it('is Claude, Codex and pi always, and Grok Build only with its leader', () => {
    for (const agent of [claudeAgent, claudeNoShim, codexAgent, codexNoDaemon, grokAgent, piAgent]) {
      expect(agent.capabilities, agent.agent).toContain('rename');
    }
    expect(grokNoLeader.capabilities).not.toContain('rename');
  });
});

describe('the name the agent holds', () => {
  const flaky = mockSession('ses-flaky');
  const push = mockSession('ses-push');
  const held = (title: string, session: Session = push) => heldName(title, session, sessions);

  it('is the name asked for when nothing else holds it', () => {
    expect(held('A name nobody has')).toBe('A name nobody has');
  });

  it('is suffixed when another live Claude session on the machine holds it', () => {
    expect(held(flaky.title)).toBe('Fix flaky auth test (2)');
  });

  it('is suffixed again when the first suffix is taken too', () => {
    const second: Session = { ...mockSession('ses-vite'), title: 'Fix flaky auth test (2)' };
    const all = [...sessions.filter((s) => s.session_id !== 'ses-vite'), second];

    expect(heldName(flaky.title, push, all)).toBe('Fix flaky auth test (3)');
  });

  it('does not count a session nothing holds, one in the Archive, or one on another machine', () => {
    const exited = mockSession('ses-exited');
    expect(exited.control).toBe('none');
    expect(held(exited.title)).toBe(exited.title);

    const handArchived = mockSession('ses-archived-mac');
    expect(handArchived.archived).toBe(true);
    expect(held(handArchived.title)).toBe(handArchived.title);

    const elsewhere = mockSession('ses-attach');
    expect(elsewhere.device_id).not.toBe(push.device_id);
    expect(held(elsewhere.title)).toBe(elsewhere.title);
  });

  it('is never changed for a session’s own name, nor for any other agent', () => {
    expect(heldName(push.title, push, sessions)).toBe(push.title);
    const codex = mockSession('ses-crash');
    expect(heldName(flaky.title, codex, sessions)).toBe(flaky.title);
  });

  it('shows in the plan, where the reply’s title then differs from the request', () => {
    expect(planRename(push, agentOf(push), { title: flaky.title }, sessions)).toEqual({
      outcome: 'rename',
      title: 'Fix flaky auth test (2)',
    });
  });
});
