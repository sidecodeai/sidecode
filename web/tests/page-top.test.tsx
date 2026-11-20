/**
 * The pages scroll the document, one scroller they all share, so a different page opens at its
 * top (`src/lib/usePageTop.ts`): Sessions opened from the foot of Settings shows its title, its
 * search and New session. The sections of Settings are one page and keep the offset, as does the
 * way back from a device to its list, and Back is the browser's, which restores what it kept.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useNavigate } from 'react-router';
import { App } from '../src/App';
import { useAuth } from '../src/stores/auth';
import { useDevices } from '../src/stores/devices';
import { useSessions } from '../src/stores/sessions';
import { readSettingsFor, useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import { devices } from '../mock/fixtures';
import { Address } from './Address';

class SilentSocket {
  close(): void {}
  send(): void {}
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** The gateway, with `alice` signed in. */
function gateway(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://gateway.test').pathname;
      if (path === '/api/live/status') return json({}, 404);
      if (path === '/api/config') {
        return json({
          public_origin: 'https://rc.example.com',
          stt: { enabled: false, languages: ['auto'] },
          push: { web_enabled: true, apns_enabled: false },
          version: '1.21.0',
        });
      }
      if (path === '/api/session') return json({ ok: true, user: { username: 'alice', role: 'member' }, exp: 0 });
      return json({});
    }),
  );
}

/** Every move of the window the app asks for; each one lands where it was asked to. */
const scrollTo = vi.fn((_x: number, y: number) => {
  vi.stubGlobal('scrollY', y);
});

/** The window scrolled well down, as by someone who read to the foot of a page. */
const scrollDown = (): void => {
  vi.stubGlobal('scrollY', 900);
};

/** The browser's Back. */
function BackButton() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(-1)}>
      Browser back
    </button>
  );
}

beforeEach(() => {
  gateway();
  vi.stubGlobal('WebSocket', SilentSocket);
  vi.stubGlobal('scrollTo', scrollTo);
  scrollTo.mockClear();
  useAuth.setState({ status: 'unknown', username: null, role: null, config: null, version: null });
  useSessions.setState({ loaded: true, sessions: {} });
  // Offline, so its page asks the device for nothing.
  useDevices.setState({ devices: [{ ...devices[0]!, online: false }], loaded: true, updateErrors: {} });
  readSettingsFor(null);
  useSettings.setState({ language: 'en' });
});

afterEach(() => {
  vi.unstubAllGlobals();
  readSettingsFor(null);
  void useSettings.persist.getOptions().storage?.removeItem('rc.settings.alice');
  useSettings.setState({ language: 'en' });
});

function openApp(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Address />
      <BackButton />
    </MemoryRouter>,
  );
}

const address = (): string | null => screen.getByTestId('address').textContent;

/** The top bar's tabs, once the account and the page are in. */
const tabs = (): Promise<HTMLElement> => screen.findByRole('navigation', { name: strings.nav.primary });

describe('the page offset', () => {
  it('opens Sessions at its top from the foot of Settings', async () => {
    openApp('/settings/general');
    const bar = await tabs();
    scrollDown();

    await userEvent.click(within(bar).getByRole('link', { name: strings.nav.sessions }));

    expect(await screen.findByRole('heading', { level: 1, name: strings.sessions.title })).toBeInTheDocument();
    expect(scrollTo).toHaveBeenCalledOnce();
    expect(scrollTo).toHaveBeenCalledWith(0, 0);
  });

  it('keeps the offset between the sections of Settings, which are one page', async () => {
    openApp('/settings/general');
    const sections = await screen.findByRole('navigation', { name: strings.settings.title });
    scrollDown();

    await userEvent.click(within(sections).getByRole('link', { name: strings.settings.voice }));

    await waitFor(() => expect(address()).toBe('/settings/voice'));
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it('keeps the offset on the way back from a device to its list', async () => {
    openApp('/devices/dev-mac');
    const back = await within(await screen.findByRole('main')).findByRole('link', {
      name: strings.devicePage.back,
    });
    scrollDown();

    await userEvent.click(back);

    expect(await screen.findByRole('heading', { level: 1, name: strings.devices.title })).toBeInTheDocument();
    expect(address()).toBe('/devices');
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it('leaves Back to the browser, even to a different page', async () => {
    openApp('/settings/general');
    const bar = await tabs();
    scrollDown();
    await userEvent.click(within(bar).getByRole('link', { name: strings.nav.sessions }));
    await screen.findByRole('heading', { level: 1, name: strings.sessions.title });
    scrollTo.mockClear();
    scrollDown();

    await userEvent.click(screen.getByRole('button', { name: 'Browser back' }));

    await waitFor(() => expect(address()).toBe('/settings/general'));
    expect(scrollTo).not.toHaveBeenCalled();
  });
});
