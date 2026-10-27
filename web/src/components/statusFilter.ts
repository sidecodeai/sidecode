import type { DotTone } from './dotTone';

/**
 * The Sessions page's filter by status: All states, Running, Waiting, Ended.
 *
 * It reads the dot a row shows rather than the raw `state`, so what the reader
 * filters on is what they see, and the three options that narrow the list
 * share the five tones between them — every session is in exactly one:
 *
 * - Running is the green dot, `working`: a turn is under way (`starting` and
 *   `running`).
 * - Waiting is both ambers: `waiting`, a permission or a question to answer,
 *   and `live`, a finished turn whose result is there to be looked at, or a
 *   terminal still open. They are the one "For you" of the legend.
 * - Ended is the grey dot, where nothing owns the session any more, it was
 *   stopped or closed, or its machine is gone, and the red one, where the agent
 *   reported an error.
 */
export type StatusFilter = 'all' | 'running' | 'waiting' | 'ended';

/** The options, in the order the menu lists them. */
export const STATUS_FILTERS: readonly StatusFilter[] = ['all', 'running', 'waiting', 'ended'];

/** Whether a dot of this tone passes the filter. */
export function matchesStatus(filter: StatusFilter, tone: DotTone): boolean {
  switch (filter) {
    case 'all':
      return true;
    case 'running':
      return tone === 'working';
    case 'waiting':
      return tone === 'waiting' || tone === 'live';
    case 'ended':
      return tone === 'off' || tone === 'failed';
  }
}
