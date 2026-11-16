/**
 * The account button and its menu (`features/account-menu`): who is signed in,
 * at the foot of the chat sidebar and in the top bar, and a menu of Settings
 * and Sign out, which asks first in the Settings row's words and then does what
 * that row does.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router';
import {
  AccountButton,
  type AccountButtonPlace,
} from '../src/features/account-menu/AccountButton';
import { AppLayout } from '../src/layout/AppLayout';
import type { ConfigResponse } from '../src/lib/api';
import { useAuth } from '../src/stores/auth';
import { useConnection } from '../src/stores/connection';
import { useSettings } from '../src/stores/settings';
import { stringTables, strings } from '../src/strings';

const gatewayConfig: ConfigResponse = {
  public_origin: 'https://rc.example.com',
  stt: { enabled: false, languages: ['auto'] },
  push: { web_enabled: true, apns_enabled: true },
  version: '1.18.6',
};

beforeEach(() => {
  useSettings.setState({ language: 'en' });
  useAuth.setState({ status: 'signed-in', username: 'alice', role: 'admin', config: gatewayConfig });
  useConnection.setState({ status: 'open' });
});

afterEach(() => {
  vi.unstubAllGlobals();
  useSettings.setState({ language: 'en' });
});

/** Where the router is, so a test can see where an item led. */
function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>;
}

function show(place: AccountButtonPlace = 'sidebar') {
  return render(
    <MemoryRouter initialEntries={['/sessions/dev-mac/ses-1']}>
      <Routes>
        <Route path="*" element={<Where />} />
      </Routes>
      <AccountButton place={place} />
      <button type="button">elsewhere</button>
    </MemoryRouter>,
  );
}

const trigger = (): HTMLElement => {
  const el = document.querySelector<HTMLElement>('.account-button');
  if (!el) throw new Error('no account button');
  return el;
};
const menu = (): HTMLElement => screen.getByRole('menu');
const items = (): string[] =>
  within(menu())
    .getAllByRole('menuitem')
    .map((item) => item.textContent ?? '');

describe('the button at the foot of the sidebar', () => {
  it('names the account and the gateway’s host', () => {
    show();

    expect(trigger()).toHaveTextContent('alice');
    expect(trigger()).toHaveTextContent('rc.example.com');
    expect(trigger()).toHaveAccessibleName('alice rc.example.com');
    expect(trigger().querySelector('.avatar')).toHaveTextContent('AL');
    expect(trigger().querySelector('.avatar')).toHaveAttribute('aria-hidden', 'true');
  });

  it('draws the initials alone on the rail and in the top bar, named for the account', () => {
    for (const place of ['rail', 'topbar'] as const) {
      const { unmount } = show(place);
      expect(trigger()).toHaveAccessibleName(strings.accountMenu.button('alice'));
      expect(trigger()).toHaveTextContent(/^AL$/);
      unmount();
    }
  });
});

describe('the menu', () => {
  it('opens on a click, as the button’s menu, with focus on its first item', async () => {
    show();
    expect(trigger()).toHaveAttribute('aria-haspopup', 'menu');
    expect(trigger()).toHaveAttribute('aria-expanded', 'false');

    await userEvent.click(trigger());

    expect(trigger()).toHaveAttribute('aria-expanded', 'true');
    // The button controls the menu itself, not the surface it is drawn on.
    expect(menu().id).not.toBe('');
    expect(trigger()).toHaveAttribute('aria-controls', menu().id);
    expect(within(menu()).getAllByRole('menuitem')[0]).toHaveFocus();
  });

  it('lists Settings and Sign out, under the name, the role and the host', async () => {
    show();
    await userEvent.click(trigger());

    expect(menu()).toHaveAccessibleName('alice Admin · rc.example.com');
    expect(items()).toEqual(['Settings', 'Sign out']);
    expect(screen.getByRole('menuitem', { name: 'Settings' })).toHaveAttribute('href', '/settings');
    expect(within(menu()).getByRole('separator')).toBeInTheDocument();
  });

  it('moves with the arrow keys, round the ends', async () => {
    show();
    await userEvent.click(trigger());
    const [first, last] = within(menu()).getAllByRole('menuitem');

    await userEvent.keyboard('{ArrowUp}');
    expect(last).toHaveFocus();
    await userEvent.keyboard('{ArrowDown}');
    expect(first).toHaveFocus();
    await userEvent.keyboard('{End}');
    expect(last).toHaveFocus();
  });

  it('closes on Escape and on Tab, with focus back on the button', async () => {
    show();
    for (const key of ['{Escape}', '{Tab}']) {
      await userEvent.click(trigger());
      await userEvent.keyboard(key);

      expect(screen.queryByRole('menu')).toBeNull();
      expect(trigger()).toHaveFocus();
    }
  });

  it('closes on a press outside, and on a second press of the button', async () => {
    show();
    await userEvent.click(trigger());
    await userEvent.click(screen.getByRole('button', { name: 'elsewhere' }));
    expect(screen.queryByRole('menu')).toBeNull();

    await userEvent.click(trigger());
    await userEvent.click(trigger());
    expect(screen.queryByRole('menu')).toBeNull();
  });

  /**
   * A resize can take the button away — below 1024 px the sidebar is not drawn —
   * and a menu left open would hold focus beside nothing; the window losing
   * focus closes it too, as it closes a context menu.
   */
  it('closes when the window is resized or loses focus, with focus back on the button', async () => {
    show();
    for (const away of ['resize', 'blur']) {
      await userEvent.click(trigger());
      expect(screen.getByRole('menu')).toBeInTheDocument();

      act(() => {
        window.dispatchEvent(new Event(away));
      });

      expect(screen.queryByRole('menu')).toBeNull();
      expect(trigger()).toHaveAttribute('aria-expanded', 'false');
      expect(trigger()).toHaveFocus();
    }
  });

  it('goes where an item leads and closes, with focus back on the button', async () => {
    show();
    await userEvent.click(trigger());

    await userEvent.click(screen.getByRole('menuitem', { name: 'Settings' }));

    expect(screen.getByTestId('where')).toHaveTextContent('/settings');
    expect(screen.queryByRole('menu')).toBeNull();
    expect(trigger()).toHaveFocus();
  });

  it('is in Chinese when the interface is', async () => {
    useSettings.setState({ language: 'zh-Hans' });
    show('topbar');

    expect(trigger()).toHaveAccessibleName('账户：alice');
    await userEvent.click(trigger());
    expect(items()).toEqual(['设置', '退出登录']);
    expect(stringTables['zh-Hans'].accountMenu.roleAtHost('管理员', 'rc.example.com')).toBe(
      '管理员 · rc.example.com',
    );
  });
});

describe('Sign out', () => {
  it('asks first in the Settings row’s words, and leaves the menu', async () => {
    show();
    await userEvent.click(trigger());
    await userEvent.click(screen.getByRole('menuitem', { name: strings.settings.signOut }));

    expect(screen.queryByRole('menu')).toBeNull();
    const dialog = screen.getByRole('dialog', { name: strings.settings.signOutConfirm });
    expect(dialog).toHaveTextContent(strings.settings.signOutNote);
    expect(dialog.closest('.overlay')?.parentElement).toBe(document.body);
  });

  it('changes nothing when cancelled, and gives focus back to the button', async () => {
    const logout = vi.fn(async () => undefined);
    useAuth.setState({ logout });
    show();
    await userEvent.click(trigger());
    await userEvent.click(screen.getByRole('menuitem', { name: strings.settings.signOut }));

    await userEvent.click(screen.getByRole('button', { name: strings.common.cancel }));

    expect(screen.queryByRole('dialog')).toBeNull();
    expect(logout).not.toHaveBeenCalled();
    expect(trigger()).toHaveFocus();
  });

  it('signs out as the Settings row does, then shows the login screen', async () => {
    const logout = vi.fn(async () => undefined);
    useAuth.setState({ logout });
    show();
    await userEvent.click(trigger());
    await userEvent.click(screen.getByRole('menuitem', { name: strings.settings.signOut }));

    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: strings.settings.signOut }));

    expect(logout).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/login'));
  });
});

describe('the top bar', () => {
  it('makes the initials a button that opens the same menu, and keeps the rest of the bar', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 404 })));
    render(
      <MemoryRouter initialEntries={['/devices']}>
        <Routes>
          <Route element={<AppLayout />}>
            <Route path="*" element={<Where />} />
          </Route>
        </Routes>
      </MemoryRouter>,
    );
    const bar = document.querySelector<HTMLElement>('.topbar');
    if (!bar) throw new Error('no top bar');

    const button = within(bar).getByRole('button', { name: strings.accountMenu.button('alice') });
    expect(bar.querySelector('.topbar-right')?.lastElementChild).toBe(button);
    expect(within(bar).getByRole('link', { name: strings.nav.devices })).toBeInTheDocument();
    expect(within(bar).getByText('rc.example.com')).toBeInTheDocument();

    fireEvent.click(button);
    expect(items()).toEqual(['Settings', 'Sign out']);
    // The voice assistant's entry beside it asks the gateway once; let that settle.
    await waitFor(() => expect(fetch).toHaveBeenCalled());
  });
});
