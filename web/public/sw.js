/**
 * remote-control service worker.
 * Network-first for navigations, cache-first for hashed build assets, and for
 * a push the app icon's count and a generic notification (payloads never carry
 * message content).
 */
const CACHE = 'rc-shell-v1';
const SHELL = ['/', '/manifest.webmanifest', '/icon.svg', '/icon-192.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

function isBypassed(url) {
  return (
    url.pathname.startsWith('/api') ||
    url.pathname.startsWith('/ws') ||
    url.pathname.startsWith('/install.sh') ||
    url.pathname.startsWith('/dist/')
  );
}

// A 502 from the reverse proxy must never become the offline app shell.
function cacheable(response) {
  return Boolean(response) && response.ok && response.type === 'basic';
}

function putInCache(key, response) {
  const copy = response.clone();
  caches
    .open(CACHE)
    .then((cache) => cache.put(key, copy))
    .catch(() => {});
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || isBypassed(url)) return;

  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (cacheable(response)) putInCache('/', response);
          return response;
        })
        .catch(() => caches.match('/').then((cached) => cached ?? Response.error())),
    );
    return;
  }

  if (url.pathname.startsWith('/assets/')) {
    event.respondWith(
      caches.match(request).then(
        (cached) =>
          cached ??
          fetch(request).then((response) => {
            if (cacheable(response)) putInCache(request, response);
            return response;
          }),
      ),
    );
  }
});

const TITLES = {
  needs_approval: 'approval needed',
  needs_input: 'a question is waiting',
  turn_completed: 'finished a turn',
  error: 'hit an error',
  // A35: what the device did about a session the usage limit stopped. The time
  // is never in a push — the gateway does not know this browser's zone — and is
  // read in the app, which has the session's `resume`.
  limit_reached: 'paused by the usage limit',
  resumed: 'resumed after the limit reset',
  resume_dropped: 'not resumed',
};

// A47: every push carries how many of the account's sessions have a red dot,
// which the installed app's icon shows while no page of it is open to keep it.
// A browser without a badge, or a payload without the count, changes nothing.
function setBadge(count) {
  const nav = self.navigator;
  if (!Number.isInteger(count) || count < 0) return Promise.resolve();
  if (!nav || typeof nav.setAppBadge !== 'function' || typeof nav.clearAppBadge !== 'function') {
    return Promise.resolve();
  }
  return Promise.resolve(count > 0 ? nav.setAppBadge(count) : nav.clearAppBadge()).catch(() => {});
}

self.addEventListener('push', (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (err) {
    payload = {};
  }
  const rc = payload.rc || {};
  const badge = setBadge(rc.badge);
  // `badge` changes the count and shows nothing. The gateway sends it to APNs
  // alone, so it is not expected here, but what it means does not change.
  if (rc.kind === 'badge') {
    event.waitUntil(badge);
    return;
  }
  const device = rc.device_name || 'A device';
  // The gateway writes the whole line in `rc.title`; a kind this build knows is
  // the fallback, and one it does not still says a device needs attention and
  // still opens its session.
  const body = rc.title || `${device}: ${TITLES[rc.kind] || 'needs your attention'}`;
  event.waitUntil(
    Promise.all([
      badge,
      self.registration.showNotification('SideCode', {
        body,
        icon: '/icon-192.png',
        badge: '/icon-192.png',
        tag: rc.session_id ? `rc-${rc.session_id}` : 'rc',
        renotify: true,
        data: { rc },
      }),
    ]),
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const rc = (event.notification.data && event.notification.data.rc) || {};
  const path =
    rc.device_id && rc.session_id ? `/sessions/${rc.device_id}/${rc.session_id}` : '/sessions';
  const target = new URL(path, self.location.origin).href;
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.startsWith(self.location.origin) && 'focus' in client) {
          client.postMessage({ type: 'rc.navigate', path });
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    }),
  );
});
