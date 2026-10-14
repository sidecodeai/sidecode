/**
 * The status-dot rule, in full. `dotTone` is the one place a session's tone is
 * decided, so this suite walks every `state` × `control` pair, online and
 * offline, against the table in `docs/DESIGN.md`.
 */
import { describe, expect, it } from 'vitest';
import { dotTone, type DotTone } from '../src/components/dotTone';
import type { ControlOwner, SessionState } from '../src/protocol/types';

const CONTROLS: ControlOwner[] = ['remote', 'terminal', 'shared', 'none'];

/** The table, written out: state → control → tone, for an online device. */
const ONLINE: Record<SessionState, Record<ControlOwner, DotTone>> = {
  starting: { remote: 'working', terminal: 'working', shared: 'working', none: 'working' },
  running: { remote: 'working', terminal: 'working', shared: 'working', none: 'working' },
  needs_approval: { remote: 'waiting', terminal: 'waiting', shared: 'waiting', none: 'waiting' },
  needs_input: { remote: 'waiting', terminal: 'waiting', shared: 'waiting', none: 'waiting' },
  idle: { remote: 'live', terminal: 'live', shared: 'live', none: 'off' },
  readonly: { remote: 'live', terminal: 'live', shared: 'live', none: 'off' },
  stopped: { remote: 'off', terminal: 'off', shared: 'off', none: 'off' },
  error: { remote: 'failed', terminal: 'failed', shared: 'failed', none: 'failed' },
};

const STATES = Object.keys(ONLINE) as SessionState[];

const walk = (online: boolean): Record<string, DotTone> =>
  Object.fromEntries(
    STATES.flatMap((state) =>
      CONTROLS.map((control) => [`${state}/${control}`, dotTone(state, control, online)]),
    ),
  );

describe('dotTone', () => {
  it('covers every state and control on an online device', () => {
    const expected = Object.fromEntries(
      STATES.flatMap((state) =>
        CONTROLS.map((control) => [`${state}/${control}`, ONLINE[state][control]]),
      ),
    );

    expect(walk(true)).toEqual(expected);
  });

  it('goes quiet for every state once the device is offline', () => {
    const tones = new Set(Object.values(walk(false)));

    expect([...tones]).toEqual(['off']);
  });

  it('separates a finished turn from an exited CLI', () => {
    // The pair the old state-only rule could not tell apart.
    expect(dotTone('idle', 'remote', true)).toBe('live');
    expect(dotTone('idle', 'none', true)).toBe('off');
  });

  it('keeps a terminal session alive while the terminal holds it', () => {
    expect(dotTone('readonly', 'terminal', true)).toBe('live');
    expect(dotTone('readonly', 'shared', true)).toBe('live');
  });

  it('marks a blocked session differently from a running one', () => {
    // `waiting` is the one tone that animates, so the two must not share it.
    expect(dotTone('running', 'remote', true)).toBe('working');
    expect(dotTone('needs_input', 'remote', true)).toBe('waiting');
    expect(dotTone('needs_approval', 'remote', true)).toBe('waiting');
  });
});
