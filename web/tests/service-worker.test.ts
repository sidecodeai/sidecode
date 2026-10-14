/**
 * The service worker's half of push (PROTOCOL.md §3.7). It is plain JavaScript
 * the browser loads on its own, so it is exercised here the way a browser does:
 * the file is evaluated against a stand-in `self`, and the handlers it
 * registered are called with the payloads the gateway sends.
 *
 * A35 added three kinds. An app that does not know a kind still says a device
 * needs attention and still opens that device's session, which is what keeps an
 * old tab useful after a gateway upgrade. A47 added the count every push carries
 * for the app's icon.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const SOURCE = readFileSync(join(process.cwd(), 'public', 'sw.js'), 'utf8');

interface Notification {
  body: string;
  data: { rc: Record<string, unknown> };
  tag: string;
}

interface Worker {
  push: (payload: unknown) => Promise<Notification>;
  /** A47: every notification one push showed, which may be none. */
  deliver: (payload: unknown) => Promise<Notification[]>;
  click: (rc: Record<string, unknown>) => Promise<{ navigated: string[]; opened: string[] }>;
}

/** A47: the worker's own `navigator`, with or without the Badging API. */
interface BadgeNavigator {
  setAppBadge?: (count?: number) => Promise<void>;
  clearAppBadge?: () => Promise<void>;
}

/** Evaluate `sw.js` against a stand-in global and hand back its two handlers. */
function load(navigator?: BadgeNavigator): Worker {
  const listeners = new Map<string, (event: unknown) => void>();
  const shown: Notification[] = [];
  const navigated: string[] = [];
  const opened: string[] = [];
  const clients: { url: string; focus: () => void; postMessage: (m: unknown) => void }[] = [
    {
      url: 'https://rc.example.com/sessions',
      focus: () => undefined,
      postMessage: (message) => {
        navigated.push((message as { path: string }).path);
      },
    },
  ];

  const self = {
    addEventListener: (type: string, handler: (event: unknown) => void) => {
      listeners.set(type, handler);
    },
    location: { origin: 'https://rc.example.com' },
    navigator,
    skipWaiting: () => Promise.resolve(),
    registration: {
      showNotification: (_title: string, options: Notification) => {
        shown.push(options);
        return Promise.resolve();
      },
    },
    clients: {
      claim: () => Promise.resolve(),
      matchAll: () => Promise.resolve(clients),
      openWindow: (url: string) => {
        opened.push(url);
        return Promise.resolve(null);
      },
    },
  };

  // `caches` is only reached from the fetch and install handlers, which this
  // suite does not drive; the stub is there so evaluation does not need it.
  const caches = { open: () => Promise.resolve({}), keys: () => Promise.resolve([]) };
  new Function('self', 'caches', SOURCE)(self, caches);

  const fire = async (type: string, event: Record<string, unknown>): Promise<void> => {
    const handler = listeners.get(type);
    if (!handler) throw new Error(`sw.js registered no ${type} handler`);
    const waited: unknown[] = [];
    handler({ ...event, waitUntil: (p: unknown) => waited.push(p) });
    await Promise.all(waited);
  };

  return {
    push: async (payload) => {
      await fire('push', { data: { json: () => payload } });
      const last = shown.at(-1);
      if (!last) throw new Error('no notification was shown');
      return last;
    },
    deliver: async (payload) => {
      const before = shown.length;
      await fire('push', { data: { json: () => payload } });
      return shown.slice(before);
    },
    click: async (rc) => {
      navigated.length = 0;
      opened.length = 0;
      await fire('notificationclick', {
        notification: { close: vi.fn(), data: { rc } },
      });
      return { navigated, opened };
    },
  };
}

const payload = (rc: Record<string, unknown>) => ({ rc: { v: 1, ...rc } });

let worker: Worker;

beforeEach(() => {
  worker = load();
});

describe('the push the service worker renders', () => {
  it('writes the line the gateway sent', async () => {
    const shown = await worker.push(
      payload({
        kind: 'limit_reached',
        device_name: 'mac-studio-office',
        title: 'mac-studio-office: paused by the usage limit',
        device_id: 'dev-1',
        session_id: 'ses-1',
      }),
    );
    expect(shown.body).toBe('mac-studio-office: paused by the usage limit');
  });

  it('knows the three A35 kinds on its own, without a title', async () => {
    const lines: string[] = [];
    for (const kind of ['limit_reached', 'resumed', 'resume_dropped']) {
      const shown = await worker.push(payload({ kind, device_name: 'mac-studio-office' }));
      lines.push(shown.body);
    }
    expect(lines).toEqual([
      'mac-studio-office: paused by the usage limit',
      'mac-studio-office: resumed after the limit reset',
      'mac-studio-office: not resumed',
    ]);
  });

  it('still says a device needs attention for a kind it has never seen', async () => {
    const shown = await worker.push(payload({ kind: 'something_new', device_name: 'ci-box' }));
    expect(shown.body).toBe('ci-box: needs your attention');
  });

  it('carries the session it is about, whatever the kind', async () => {
    const shown = await worker.push(
      payload({ kind: 'resumed', device_name: 'mac', session_id: 'ses-9' }),
    );
    expect(shown.tag).toBe('rc-ses-9');
    expect(shown.data.rc.session_id).toBe('ses-9');
  });
});

/**
 * A47 §3.7: every push carries `rc.badge`, how many of the account's sessions
 * have a red dot, and the installed app's icon shows it while no page of the
 * app is open to keep it. `badge` changes the count and shows nothing.
 */
describe('the app icon badge a push sets', () => {
  function badged() {
    const nav = {
      setAppBadge: vi.fn((_count?: number) => Promise.resolve()),
      clearAppBadge: vi.fn(() => Promise.resolve()),
    };
    return { nav, worker: load(nav) };
  }

  it('shows the count the push carries, with the notification', async () => {
    const { nav, worker } = badged();
    const shown = await worker.deliver(
      payload({ kind: 'turn_completed', device_name: 'mac', session_id: 'ses-1', badge: 3 }),
    );

    expect(nav.setAppBadge).toHaveBeenCalledWith(3);
    expect(shown).toHaveLength(1);
  });

  it('shows none at zero', async () => {
    const { nav, worker } = badged();
    await worker.deliver(payload({ kind: 'needs_input', device_name: 'mac', badge: 0 }));

    expect(nav.clearAppBadge).toHaveBeenCalledOnce();
    expect(nav.setAppBadge).not.toHaveBeenCalled();
  });

  it('changes only the count for a badge push, and shows nothing', async () => {
    const { nav, worker } = badged();
    const shown = await worker.deliver(
      payload({ kind: 'badge', device_name: 'mac', session_id: 'ses-1', title: '', badge: 2 }),
    );

    expect(nav.setAppBadge).toHaveBeenCalledWith(2);
    expect(shown).toEqual([]);
  });

  it('leaves the badge alone when the push carries no count, or not a count', async () => {
    const { nav, worker } = badged();
    for (const badge of [undefined, -1, 1.5, '4', null]) {
      await worker.deliver(payload({ kind: 'turn_completed', device_name: 'mac', badge }));
    }

    expect(nav.setAppBadge).not.toHaveBeenCalled();
    expect(nav.clearAppBadge).not.toHaveBeenCalled();
  });

  it('still shows every push where the browser has no badge', async () => {
    const shown = await load().deliver(
      payload({ kind: 'turn_completed', device_name: 'mac', title: 'mac: finished a turn', badge: 1 }),
    );

    expect(shown.map((n) => n.body)).toEqual(['mac: finished a turn']);
  });

  it('still shows the notification when the browser refuses the badge', async () => {
    const nav = {
      setAppBadge: vi.fn(() => Promise.reject(new Error('not installed'))),
      clearAppBadge: vi.fn(() => Promise.reject(new Error('not installed'))),
    };
    const shown = await load(nav).deliver(
      payload({ kind: 'needs_approval', device_name: 'mac', title: 'mac: approval needed', badge: 1 }),
    );

    expect(shown.map((n) => n.body)).toEqual(['mac: approval needed']);
  });
});

describe('the deep link a notification opens', () => {
  it('goes to the session for every new kind', async () => {
    for (const kind of ['limit_reached', 'resumed', 'resume_dropped', 'something_new']) {
      const result = await worker.click({ kind, device_id: 'dev-1', session_id: 'ses-1' });
      expect(result.navigated).toEqual(['/sessions/dev-1/ses-1']);
    }
  });

  it('falls back to the session list when the payload names none', async () => {
    const result = await worker.click({ kind: 'resumed' });
    expect(result.navigated).toEqual(['/sessions']);
  });
});
