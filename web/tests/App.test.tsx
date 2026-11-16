import { useLayoutEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { App } from '../src/App';
import { useAuth } from '../src/stores/auth';
import { useDevices } from '../src/stores/devices';
import { useSessions } from '../src/stores/sessions';
import { readSettingsFor, useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import { devices as deviceFixtures } from '../mock/fixtures';

let requested: string[] = [];
/** What `GET /api/config` answers; nothing, unless a test draws a screen that reads it. */
let gatewayConfig: unknown = {};

function stubFetch(sessionStatus: number) {
  requested = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://gateway.test');
      requested.push(url.pathname);
      if (url.pathname === '/api/config') {
        return new Response(JSON.stringify(gatewayConfig), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (url.pathname === '/api/session') {
        return sessionStatus === 200
          ? new Response(JSON.stringify({ ok: true, user: { username: 'admin' }, exp: 0 }), {
              status: 200,
              headers: { 'content-type': 'application/json' },
            })
          : new Response(JSON.stringify({ ok: false, error: { code: 'unauthorized' } }), {
              status: 401,
              headers: { 'content-type': 'application/json' },
            });
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
}

beforeEach(() => {
  useAuth.setState({ status: 'unknown', username: null, config: null, version: null });
  useSessions.setState({ loaded: false, sessions: {} });
  useDevices.setState({ devices: [], loaded: false, updateErrors: {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
  gatewayConfig = {};
});

describe('App routing', () => {
  it('shows the login screen for a protected path when the session probe fails', async () => {
    stubFetch(401);
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText(strings.login.subtitle)).toBeInTheDocument());
    expect(screen.queryByRole('heading', { name: strings.sessions.title })).not.toBeInTheDocument();
  });

  it('returns to a /pair link, fragment and all, once the password lands (A23)', async () => {
    stubFetch(401);
    render(
      <MemoryRouter initialEntries={['/pair#7ZK3M9Q2X5H8B1V4N6P0R2T4W6']}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText(strings.login.subtitle)).toBeInTheDocument());
    // The claim token lives in the fragment, so the whole path has to survive
    // the round trip through the login screen.
    useAuth.setState({ status: 'signed-in', username: 'admin' });

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: strings.pairing.claimTitle })).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(requested).toContain('/api/pairing/requests/7ZK3M9Q2X5H8B1V4N6P0R2T4W6/claim'),
    );
  });

  it('never issues an authenticated request while signed out', async () => {
    stubFetch(401);
    render(
      <MemoryRouter initialEntries={['/sessions']}>
        <App />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByText(strings.login.subtitle)).toBeInTheDocument());
    // Give any mount effect of a protected page the chance to fire. Both paths
    // the login screen touches are the unauthenticated ones of PROTOCOL.md §3.1:
    // the session probe, and the health call that says whether this gateway
    // takes registrations (A24).
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requested.sort()).toEqual(['/api/health', '/api/session']);
  });
});

/**
 * `docs/DESIGN.md` § "The three screens": on open the app lands on Sessions
 * when the account has a device and on Devices when it has none. The choice is
 * made from the first device list that arrives and is not remembered.
 */
describe('the landing rule', () => {
  const signedIn = () => {
    stubFetch(200);
    useAuth.setState({ status: 'signed-in', username: 'admin' });
    // Both lists are already in hand, so no page fetches on mount.
    useSessions.setState({ loaded: true, sessions: {} });
  };

  const renderAt = (path: string) =>
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    );

  it('lands on Sessions when the account has a device', async () => {
    signedIn();
    useDevices.setState({ devices: [deviceFixtures[0]!], loaded: true });
    renderAt('/');

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: strings.sessions.title })).toBeInTheDocument(),
    );
  });

  it('lands on Devices when the account has none', async () => {
    signedIn();
    useDevices.setState({ devices: [], loaded: true });
    renderAt('/');

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: strings.devices.title })).toBeInTheDocument(),
    );
  });

  it('decides nothing until the first device list has arrived', async () => {
    signedIn();
    useDevices.setState({ devices: [], loaded: false });
    renderAt('/');

    // An empty list nobody has confirmed yet would send every account to
    // Devices for the length of a round trip.
    expect(screen.queryByRole('heading', { name: strings.devices.title })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: strings.sessions.title })).not.toBeInTheDocument();
    expect(document.querySelector('.boot')).not.toBeNull();

    useDevices.getState().replaceAll([deviceFixtures[0]!]);
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: strings.sessions.title })).toBeInTheDocument(),
    );
  });

  it('applies the rule after signing in with nothing to return to', async () => {
    stubFetch(401);
    useSessions.setState({ loaded: true, sessions: {} });
    useDevices.setState({ devices: [], loaded: true });
    renderAt('/login');
    await waitFor(() => expect(screen.getByText(strings.login.subtitle)).toBeInTheDocument());

    useAuth.setState({ status: 'signed-in', username: 'admin' });
    // Signing out emptied the lists, so the rule waits for this account's own
    // `hello` rather than deciding from what the previous one had.
    useDevices.getState().replaceAll([]);
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: strings.devices.title })).toBeInTheDocument(),
    );
  });

  it('applies the rule to a path no route claims', async () => {
    signedIn();
    useDevices.setState({ devices: [], loaded: true });
    renderAt('/nowhere');

    await waitFor(() =>
      expect(screen.getByRole('heading', { name: strings.devices.title })).toBeInTheDocument(),
    );
  });
});

/**
 * A24: what this browser keeps for an account — here the timeline detail it
 * chose — is read before the first frame that account is drawn in, so nothing
 * flashes the previous account's or the default choice first. The layout
 * effects of a part drawn after the app run once the app's own have, before
 * the browser paints; whatever the store holds by then is what the first frame
 * draws.
 */
describe('the settings this browser keeps for an account', () => {
  const KEY = 'rc.settings.admin';
  /** Where the settings store keeps what it remembers: memory, in a browser with no storage. */
  const storage = () => useSettings.persist.getOptions().storage;

  afterEach(() => {
    readSettingsFor(null);
    void storage()?.removeItem(KEY);
  });

  it('are in place before the first frame the account is drawn in', async () => {
    // A tab that has read nobody's settings yet, as a page that has just loaded.
    readSettingsFor(null);
    void storage()?.setItem(KEY, { state: { timelineDetail: 'detailed' }, version: 1 });
    gatewayConfig = {
      public_origin: 'https://rc.example.com',
      stt: { enabled: false, languages: ['auto'] },
      push: { web_enabled: true, apns_enabled: false },
      version: '1.18.6',
    };
    stubFetch(200);
    useAuth.setState({ status: 'signed-in', username: 'admin' });
    useSessions.setState({ loaded: true, sessions: {} });
    useDevices.setState({ devices: [], loaded: true });
    let beforePaint: string | undefined;

    function Frame() {
      useLayoutEffect(() => {
        beforePaint = useSettings.getState().timelineDetail;
      }, []);
      return null;
    }

    render(
      <MemoryRouter initialEntries={['/settings/general']}>
        <App />
        <Frame />
      </MemoryRouter>,
    );

    expect(beforePaint).toBe('detailed');
    await waitFor(() => expect(screen.getByRole('button', { name: 'Detailed' })).toHaveAttribute('aria-pressed', 'true'));
  });
});
