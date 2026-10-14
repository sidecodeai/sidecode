/**
 * Who is signed in and where, in the two forms the app draws them: the initials
 * in a circle and the gateway's host. Both are pure, and both are read the same
 * way by the top bar and by the Settings header (`docs/DESIGN.md` § "The
 * Settings screen"), so the two never disagree.
 */

/** What separates the parts of a username the gateway allows: `.`, `_`, `-`. */
const PART = /[._\-\s]+/;

/**
 * One or two characters for the circle: the first letter of each of the first
 * two parts, or the first two letters of a single part. A script without letter
 * case — Chinese above all — reads as one character rather than two.
 */
export function initials(username: string): string {
  const [first, second] = username.split(PART).filter(Boolean);
  if (!first) return '?';
  if (second) return `${first.slice(0, 1)}${second.slice(0, 1)}`.toUpperCase();
  return /^[a-z0-9]/i.test(first) ? first.slice(0, 2).toUpperCase() : first.slice(0, 1);
}

/** The gateway origin without its scheme and without any path: `host[:port]`. */
export function gatewayHost(origin: string): string {
  return origin.replace(/^[a-z][a-z0-9+.-]*:\/\//i, '').replace(/\/.*$/, '');
}
