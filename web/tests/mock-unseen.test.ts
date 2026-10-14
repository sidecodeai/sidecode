/**
 * A47 §4.4 — the mark as the mock gateway keeps it, so what `npm run dev:mock`
 * shows is what a gateway would: set when a session moves from a running turn
 * to waiting for the person (`needs_approval`,
 * `needs_input`, or `idle` / `readonly` with a control), read from the states
 * alone; cleared when it works again, by `session.seen` and by an archive; and
 * carried only while it is true.
 */
import { describe, expect, it } from 'vitest';
import { clearMark, isWaiting, isWorking, moveState } from '../mock/unseen';
import { sessions } from '../mock/fixtures';
import type { ControlOwner, Session, SessionState } from '../src/protocol/types';

const STATES: SessionState[] = [
  'starting',
  'idle',
  'running',
  'needs_approval',
  'needs_input',
  'error',
  'stopped',
  'readonly',
];
const CONTROLS: ControlOwner[] = ['remote', 'terminal', 'shared', 'none'];

const base = sessions.find((s) => s.session_id === 'ses-push')!;

function session(state: SessionState, control: ControlOwner, unseen?: true): Session {
  const { unseen: _drop, ...rest } = base;
  return { ...rest, state, control, ...(unseen ? { unseen } : {}) };
}

/** The mark after one move, from a session that carried `unseen` or not. */
function after(
  from: SessionState,
  to: SessionState,
  control: ControlOwner = 'remote',
  unseen?: true,
): boolean {
  const s = session(from, control, unseen);
  moveState(s, to);
  expect(s.state).toBe(to);
  return s.unseen === true;
}

describe('working and waiting', () => {
  it('reads working from the state alone: a running turn, not a start', () => {
    expect(STATES.filter(isWorking)).toEqual(['running']);
  });

  it('reads waiting from the state and the control: amber', () => {
    for (const control of CONTROLS) {
      const waiting = STATES.filter((state) => isWaiting(state, control));
      expect(waiting).toEqual(
        control === 'none'
          ? ['needs_approval', 'needs_input']
          : ['idle', 'needs_approval', 'needs_input', 'readonly'],
      );
    }
  });
});

describe('setting the mark', () => {
  it('marks a session that moves from a running turn to every kind of waiting', () => {
    expect(after('running', 'needs_approval')).toBe(true);
    expect(after('running', 'needs_input')).toBe(true);
    expect(after('running', 'idle')).toBe(true);
    expect(after('running', 'idle', 'shared')).toBe(true);
    expect(after('running', 'readonly', 'terminal')).toBe(true);
  });

  it('does not mark a session that only started', () => {
    expect(after('starting', 'idle')).toBe(false);
    expect(after('starting', 'needs_approval')).toBe(false);
    expect(after('starting', 'readonly', 'terminal')).toBe(false);
  });

  it('does not mark a CLI that exited, an error or a stop', () => {
    expect(after('running', 'idle', 'none')).toBe(false);
    expect(after('running', 'readonly', 'none')).toBe(false);
    expect(after('running', 'error')).toBe(false);
    expect(after('running', 'stopped')).toBe(false);
  });

  it('does not mark a move from waiting to waiting, nor from rest', () => {
    expect(after('needs_approval', 'idle')).toBe(false);
    expect(after('needs_input', 'idle')).toBe(false);
    expect(after('idle', 'needs_approval')).toBe(false);
    expect(after('error', 'idle')).toBe(false);
    expect(after('stopped', 'idle')).toBe(false);
  });

  it('keeps a mark through a move that is not work', () => {
    expect(after('needs_approval', 'idle', 'remote', true)).toBe(true);
    expect(after('idle', 'needs_input', 'remote', true)).toBe(true);
    expect(after('idle', 'error', 'remote', true)).toBe(true);
    expect(after('idle', 'idle', 'none', true)).toBe(true);
  });
});

describe('clearing the mark', () => {
  it('clears it when the session works again', () => {
    for (const from of STATES) {
      expect(after(from, 'running', 'remote', true)).toBe(false);
    }
  });

  it('clears it for session.seen and an archive, and says whether there was one', () => {
    const marked = session('idle', 'remote', true);

    expect(clearMark(marked)).toBe(true);
    expect(marked.unseen).toBeUndefined();
    expect(clearMark(marked)).toBe(false);
  });

  it('carries the field only while it is true', () => {
    const s = session('running', 'remote');
    moveState(s, 'idle');
    expect(s).toHaveProperty('unseen', true);

    moveState(s, 'running');
    expect(s).not.toHaveProperty('unseen');
  });
});

describe('what the mock starts with', () => {
  it('starts one session marked, an unarchived one waiting for the person', () => {
    const marked = sessions.filter((s) => s.unseen === true);

    expect(marked.map((s) => s.session_id)).toEqual(['ses-vite']);
    for (const s of marked) {
      expect(s.archived).toBe(false);
      expect(isWaiting(s.state, s.control)).toBe(true);
    }
    // Every other session leaves the field out, as the gateway does for false.
    for (const s of sessions.filter((s) => s.unseen !== true)) {
      expect(s).not.toHaveProperty('unseen');
    }
  });
});
