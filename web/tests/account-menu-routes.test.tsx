/**
 * The account menu through the app's own route table: Settings leads to
 * Settings' index, drawn as the address says. The menu is the top bar's here;
 * the chat sidebar's foot opens the same one (`account-menu.test.tsx`).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { App } from '../src/App';
import { useAuth } from '../src/stores/auth';
import { useDevices } from '../src/stores/devices';
import { useSessions } from '../src/stores/sessions';
import { readSettingsFor, useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import { Address } from './Address';
import { currentNavItem } from './settingsScreen';

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
          version: '1.18.6',
        });
      }
      if (path === '/api/session') return json({ ok: true, user: { username: 'alice', role: 'member' }, exp: 0 });
      return json({});
    }),
  );
}

beforeEach(() => {
  useAuth.setState({ status: 'unknown', username: null, role: null, config: null, version: null });
  useSessions.setState({ loaded: true, sessions: {} });
  useDevices.setState({ devices: [], loaded: true, updateErrors: {} });
  readSettingsFor(null);
  useSettings.setState({ language: 'en' });
  vi.stubGlobal('WebSocket', SilentSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  readSettingsFor(null);
  void useSettings.persist.getOptions().storage?.removeItem('rc.settings.alice');
  useSettings.setState({ language: 'en' });
});

function openApp(path = '/devices') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <Address />
    </MemoryRouter>,
  );
}

const address = (): string | null => screen.getByTestId('address').textContent;

/** Opens the top bar's account menu, once the account and the page are in. */
async function openMenu(name: string): Promise<HTMLElement> {
  const bar = await waitFor(() => {
    const found = document.querySelector<HTMLElement>('.topbar');
    if (!found) throw new Error('no top bar yet');
    return found;
  });
  await userEvent.click(await within(bar).findByRole('button', { name: strings.accountMenu.button(name) }));
  return screen.getByRole('menu');
}

describe('the account menu', () => {
  it('lists Settings and Sign out, and Settings leads to the index under its own title', async () => {
    gateway();
    openApp();

    const menu = await openMenu('alice');
    expect(within(menu).getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
      strings.nav.settings,
      strings.settings.signOut,
    ]);

    await userEvent.click(within(menu).getByRole('menuitem', { name: strings.nav.settings }));
    expect(address()).toBe('/settings');
    expect(screen.getByRole('heading', { level: 1, name: strings.settings.title })).toBeInTheDocument();
    expect(document.querySelector('.settings')).toHaveClass('at-index');
    expect(currentNavItem()).toBe(strings.settings.general);
  });
});
