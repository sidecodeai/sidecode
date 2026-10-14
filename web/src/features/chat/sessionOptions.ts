import type { Session } from '../../protocol/types';

/** What `session.set` can carry from the composer. A21 adds the speed tier. */
export interface SessionOptions {
  model?: string;
  permission_mode?: string;
  effort?: string;
  speed?: string | null;
}

/**
 * The session as it will be once the device accepts the patch. Absent and null
 * are different: `speed: null` is the standard tier, which is a change, while
 * leaving `speed` out is not.
 */
export function applyOptions(session: Session, patch: SessionOptions): Session {
  const next = { ...session };
  if (patch.model !== undefined) next.model = patch.model;
  if (patch.permission_mode !== undefined) next.permission_mode = patch.permission_mode;
  if (patch.effort !== undefined) next.effort = patch.effort;
  if (patch.speed !== undefined) next.speed = patch.speed;
  return next;
}
