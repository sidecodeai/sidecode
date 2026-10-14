/**
 * A47: the number on the installed app's icon, where the browser offers one
 * (`navigator.setAppBadge`). Elsewhere nothing happens, and a browser that
 * refuses — the app is not installed, or badges are off — is not this app's
 * to report.
 */
type Badging = Partial<Pick<Navigator, 'setAppBadge' | 'clearAppBadge'>>;

/** The badge reads `count`; none at zero. */
export function showAppBadge(count: number, nav: Badging = navigator): void {
  if (typeof nav.setAppBadge !== 'function' || typeof nav.clearAppBadge !== 'function') return;
  const done = count > 0 ? nav.setAppBadge(count) : nav.clearAppBadge();
  void done.catch(() => undefined);
}

/** Sign-out: the next person at this browser sees no number of ours. */
export function clearAppBadge(nav: Badging = navigator): void {
  showAppBadge(0, nav);
}
