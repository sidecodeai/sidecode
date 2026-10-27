/**
 * The rule behind the Sessions page's filter by status. It reads the dot a row
 * shows, so each option is a set of tones, and the three that narrow the list
 * share the five tones between them without overlap.
 */
import { describe, expect, it } from 'vitest';
import { dotTone, type DotTone } from '../src/components/dotTone';
import { matchesStatus, STATUS_FILTERS, type StatusFilter } from '../src/components/statusFilter';
import type { ControlOwner, SessionState } from '../src/protocol/types';

const TONES: DotTone[] = ['working', 'waiting', 'live', 'off', 'failed'];
const STATES: SessionState[] = [
  'starting',
  'running',
  'needs_approval',
  'needs_input',
  'idle',
  'readonly',
  'stopped',
  'error',
  // A state this build has never heard of: it claims nothing, and still has to
  // land in exactly one option.
  'compacting' as SessionState,
];
const CONTROLS: ControlOwner[] = ['remote', 'terminal', 'shared', 'none'];
const NARROWING = STATUS_FILTERS.filter((filter) => filter !== 'all');

/** The table, written out: the one narrowing option each tone belongs to. */
const OPTION_OF: Record<DotTone, StatusFilter> = {
  working: 'running',
  waiting: 'waiting',
  live: 'waiting',
  off: 'ended',
  failed: 'ended',
};

/** The narrowing option a session lands in, by the dot it shows. */
const optionOf = (
  state: SessionState,
  control: ControlOwner = 'remote',
  online = true,
): StatusFilter | undefined =>
  NARROWING.find((filter) => matchesStatus(filter, dotTone(state, control, online)));

describe('matchesStatus', () => {
  it('offers All states, Running, Waiting and Ended, in that order', () => {
    expect(STATUS_FILTERS).toEqual(['all', 'running', 'waiting', 'ended']);
  });

  it('lets every tone through All states', () => {
    for (const tone of TONES) expect(matchesStatus('all', tone), tone).toBe(true);
  });

  it('reads Running as the green dot, Waiting as both ambers and Ended as the grey and the red', () => {
    for (const tone of TONES) {
      for (const filter of NARROWING) {
        expect(matchesStatus(filter, tone), `${filter}, ${tone}`).toBe(OPTION_OF[tone] === filter);
      }
    }
  });

  it('puts every session in exactly one narrowing option, whatever it reports', () => {
    for (const state of STATES) {
      for (const control of CONTROLS) {
        for (const online of [true, false]) {
          const tone = dotTone(state, control, online);
          const hits = NARROWING.filter((filter) => matchesStatus(filter, tone));

          expect(hits, `${state}, ${control}, online ${String(online)}`).toHaveLength(1);
          expect(matchesStatus('all', tone)).toBe(true);
        }
      }
    }
  });

  it('calls a turn under way Running, and a question or a finished turn Waiting', () => {
    expect(optionOf('starting')).toBe('running');
    expect(optionOf('running', 'terminal')).toBe('running');
    expect(optionOf('needs_approval')).toBe('waiting');
    expect(optionOf('needs_input')).toBe('waiting');
    expect(optionOf('idle', 'shared')).toBe('waiting');
    expect(optionOf('readonly', 'terminal')).toBe('waiting');
  });

  it('calls a stopped session, an exited CLI, an error and a machine that is gone Ended', () => {
    expect(optionOf('stopped')).toBe('ended');
    expect(optionOf('idle', 'none')).toBe('ended');
    expect(optionOf('readonly', 'none')).toBe('ended');
    expect(optionOf('error')).toBe('ended');
    expect(optionOf('running', 'remote', false)).toBe('ended');
    expect(optionOf('needs_approval', 'remote', false)).toBe('ended');
  });

  it('calls a state it has never heard of Ended, on a reachable machine too', () => {
    expect(optionOf('compacting' as SessionState)).toBe('ended');
    expect(optionOf('compacting' as SessionState, 'terminal')).toBe('ended');
    expect(optionOf('compacting' as SessionState, 'none', false)).toBe('ended');
  });
});
