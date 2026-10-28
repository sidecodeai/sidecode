import { useLiveHost } from '../../src/features/live/useLiveHost';

/** What `App` mounts for the call, as it mounts it: the real hook, so the watchers and the focus are the production ones. */
export function Host(): null {
  useLiveHost();
  return null;
}
