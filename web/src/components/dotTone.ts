import type { ControlOwner, SessionState } from '../protocol/types';

/**
 * The five tones a session dot can take. The rule is the one table in
 * `docs/DESIGN.md`, shared with the iOS app, so it lives in a pure function
 * rather than in the markup: `state` alone cannot tell a finished turn on a
 * live session from an exited one, and `control` is what separates them.
 */
export type DotTone = 'working' | 'waiting' | 'live' | 'off' | 'failed';

export function dotTone(state: SessionState, control: ControlOwner, online: boolean): DotTone {
  // An offline device says nothing about what it last reported, so the dot goes
  // quiet whatever the state was.
  if (!online) return 'off';
  switch (state) {
    case 'starting':
    case 'running':
      return 'working';
    // Blocked on the user, which is the one thing worth interrupting them for.
    case 'needs_approval':
    case 'needs_input':
      return 'waiting';
    case 'error':
      return 'failed';
    case 'stopped':
      return 'off';
    // Alive and quiet only while something still holds the session: a finished
    // turn, a terminal still open, or the device attached to one.
    // `control: "none"` is a CLI that exited and left a resumable session.
    case 'idle':
    case 'readonly':
      return control === 'none' ? 'off' : 'live';
  }
}
