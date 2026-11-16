/**
 * The chat sidebar folds by hand on a wide window (`docs/DESIGN.md` § "The
 * chat sidebar"): one toggle, top left and the same element in both states;
 * a rail of the toggle, New session, Search and the account's initials, with
 * nothing else of the sidebar left in the page; ⌘. or Ctrl+. while the chat
 * page is open; the choice kept per account; and a dot on the folded toggle
 * while anything waits. Below 1024 px nothing of this applies: the sidebar is
 * not drawn, and the shortcut leaves the stored choice alone.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { Sidebar } from '../src/features/chat/Sidebar';
import { useAuth } from '../src/stores/auth';
import { useDevices } from '../src/stores/devices';
import { countWaiting, keyOf, selectSessionList, useSessions } from '../src/stores/sessions';
import { readSettingsFor, useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import { devices, sessions } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

const index = (list: Session[]): Record<string, Session> =>
  Object.fromEntries(list.map((s) => [keyOf(s), s]));

beforeEach(() => {
  useDevices.setState({ devices, loaded: true, error: null });
  useSessions.setState({ sessions: index(sessions), loaded: true, agentFilter: null });
  useSettings.setState({
    collapsedDevices: [],
    archiveExpanded: [],
    sidebarCollapsed: false,
    language: 'en',
  });
  useAuth.setState({ status: 'signed-in', username: 'alice', role: 'member' });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  useSettings.setState({ sidebarCollapsed: false, language: 'en' });
});

const showSidebar = (onNewSession = vi.fn()) =>
  render(
    <MemoryRouter>
      <Sidebar activeKey="" onNewSession={onNewSession} />
    </MemoryRouter>,
  );

const aside = (): HTMLElement => {
  const el = document.querySelector<HTMLElement>('aside.sidebar');
  if (!el) throw new Error('no sidebar');
  return el;
};

const closeToggle = () => screen.getByRole('button', { name: strings.chat.closeSidebar });
const waitingNow = () => countWaiting(selectSessionList(useSessions.getState().sessions));
const openToggle = () =>
  screen.getByRole('button', { name: strings.chat.openSidebarWaiting(waitingNow()) });

/** The shortcut as this system writes it: jsdom names no platform, so Ctrl. */
const pressShortcut = (init: KeyboardEventInit = {}): boolean =>
  fireEvent.keyDown(document, { key: '.', ctrlKey: true, ...init });

describe('the toggle', () => {
  it('opens the header, before the brand and New session, and names what it does', () => {
    showSidebar();
    const toggle = closeToggle();
    const head = toggle.closest('.sidebar-head');

    expect(head?.firstElementChild).toBe(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveAttribute('aria-controls', aside().id);
    expect(toggle).toHaveAttribute('aria-keyshortcuts', 'Control+.');
    expect(screen.getByRole('link', { name: strings.productName })).toHaveAttribute('href', '/sessions');
    expect(screen.getByRole('button', { name: strings.chat.newSession })).toBeInTheDocument();
  });

  it('folds the sidebar to its rail and opens it again, as the same element with focus kept', async () => {
    const user = userEvent.setup();
    showSidebar();
    const toggle = closeToggle();
    toggle.focus();

    await user.keyboard('{Enter}');
    expect(aside()).toHaveClass('collapsed');
    expect(openToggle()).toBe(toggle);
    expect(toggle).toHaveFocus();
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(useSettings.getState().sidebarCollapsed).toBe(true);

    await user.keyboard('{Enter}');
    expect(aside()).not.toHaveClass('collapsed');
    expect(closeToggle()).toBe(toggle);
    expect(toggle).toHaveFocus();
    expect(useSettings.getState().sidebarCollapsed).toBe(false);
  });

  it('slides only for a fold the person asked for, not for a stored one read on load', async () => {
    useSettings.setState({ sidebarCollapsed: true });
    showSidebar();
    expect(aside()).toHaveClass('collapsed');
    expect(aside()).not.toHaveClass('moving');

    // An account's settings read after the page drew.
    act(() => useSettings.setState({ sidebarCollapsed: false }));
    expect(aside()).not.toHaveClass('moving');

    await userEvent.click(closeToggle());
    expect(aside()).toHaveClass('collapsed', 'moving');
  });
});

describe('the rail', () => {
  it('holds the toggle, New session, Search and the account’s initials, and nothing else', async () => {
    showSidebar();
    await userEvent.click(closeToggle());

    expect(screen.getByRole('button', { name: strings.chat.newSession })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: strings.common.search })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: strings.accountMenu.button('alice') })).toBeInTheDocument();
    expect(screen.getAllByRole('button')).toHaveLength(4);

    // What the rail does not draw is not in the page, so it can neither take
    // focus nor be read out.
    expect(screen.queryByRole('searchbox')).toBeNull();
    expect(screen.queryByRole('navigation')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByText('Fix flaky auth test')).toBeNull();
    expect(screen.queryByText(strings.chat.sidebarFooter(devices.length, waitingNow()))).toBeNull();
  });

  it('starts a session from its New session', async () => {
    const onNewSession = vi.fn();
    showSidebar(onNewSession);
    await userEvent.click(closeToggle());

    await userEvent.click(screen.getByRole('button', { name: strings.chat.newSession }));

    expect(onNewSession).toHaveBeenCalledTimes(1);
  });

  it('opens the sidebar from its Search, with the caret in the search field', async () => {
    showSidebar();
    await userEvent.click(closeToggle());

    await userEvent.click(screen.getByRole('button', { name: strings.common.search }));

    expect(aside()).not.toHaveClass('collapsed');
    expect(screen.getByRole('searchbox', { name: strings.chat.searchPlaceholder })).toHaveFocus();
  });

  it('puts a dot on the toggle while sessions wait, and says how many', async () => {
    showSidebar();
    expect(document.querySelector('.sidebar-toggle-dot')).toBeNull();
    await userEvent.click(closeToggle());

    expect(waitingNow()).toBeGreaterThan(0);
    const toggle = openToggle();
    expect(toggle.querySelector('.sidebar-toggle-dot')).toHaveAttribute('aria-hidden', 'true');

    // Nothing waits any more: the plain words, and no dot.
    act(() => {
      useSessions.setState({
        sessions: index(sessions.map((s) => ({ ...s, state: 'idle' }) as Session)),
      });
    });
    expect(screen.getByRole('button', { name: strings.chat.openSidebar })).toBe(toggle);
    expect(toggle.querySelector('.sidebar-toggle-dot')).toBeNull();
  });
});

describe('the shortcut', () => {
  it('folds and opens on Ctrl+., taking the key from the page', () => {
    showSidebar();

    expect(pressShortcut()).toBe(false);
    expect(aside()).toHaveClass('collapsed', 'moving');
    expect(pressShortcut()).toBe(false);
    expect(aside()).not.toHaveClass('collapsed');
  });

  it('is ⌘. on an Apple system, where Ctrl+. is left alone', async () => {
    vi.spyOn(navigator, 'platform', 'get').mockReturnValue('MacIntel');
    showSidebar();
    expect(closeToggle()).toHaveAttribute('aria-keyshortcuts', 'Meta+.');

    expect(pressShortcut()).toBe(true);
    expect(aside()).not.toHaveClass('collapsed');
    expect(fireEvent.keyDown(document, { key: '.', metaKey: true })).toBe(false);
    expect(aside()).toHaveClass('collapsed');
  });

  it('folds once for a held key, not at the key-repeat rate', () => {
    showSidebar();

    expect(pressShortcut()).toBe(false);
    for (let i = 0; i < 5; i += 1) pressShortcut({ repeat: true });

    expect(aside()).toHaveClass('collapsed');
    expect(useSettings.getState().sidebarCollapsed).toBe(true);
  });

  it('is not ⌘. elsewhere, nor taken with Alt held', () => {
    showSidebar();

    expect(fireEvent.keyDown(document, { key: '.', metaKey: true })).toBe(true);
    expect(pressShortcut({ altKey: true })).toBe(true);
    expect(aside()).not.toHaveClass('collapsed');
  });

  it('leaves an input method’s composition alone', () => {
    showSidebar();

    expect(pressShortcut({ isComposing: true })).toBe(true);
    expect(pressShortcut({ keyCode: 229 })).toBe(true);
    expect(aside()).not.toHaveClass('collapsed');
  });

  it('does nothing while a dialog or a menu is open over the page', () => {
    showSidebar();
    for (const overlay of ['aria-modal', 'popover-panel']) {
      const el = document.createElement('div');
      if (overlay === 'aria-modal') el.setAttribute('aria-modal', 'true');
      else el.className = overlay;
      document.body.append(el);

      expect(pressShortcut()).toBe(true);
      expect(aside()).not.toHaveClass('collapsed');
      el.remove();
    }
  });

  it('does nothing below 1024 px, where the sidebar is not drawn, and keeps the stored choice', () => {
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: false, media: query }));
    showSidebar();

    expect(pressShortcut()).toBe(true);
    expect(useSettings.getState().sidebarCollapsed).toBe(false);
  });

  it('belongs to the chat page: once the sidebar is gone, the key is the page’s again', () => {
    const { unmount } = showSidebar();
    unmount();

    expect(pressShortcut()).toBe(true);
    expect(useSettings.getState().sidebarCollapsed).toBe(false);
  });
});

/**
 * A fold takes away most of what the sidebar holds. Focus on any of it goes to
 * the toggle rather than falling to the page; focus on what both states draw,
 * or outside the sidebar, stays where it is.
 */
describe('focus across a fold', () => {
  const rowMore = (title: string): HTMLElement => {
    const item = screen.getByText(title).closest('li');
    if (!item) throw new Error(`no row for ${title}`);
    return within(item).getByRole('button', { name: strings.a11y.openMenu });
  };

  const takenAway: [string, () => HTMLElement][] = [
    ['the search field', () => screen.getByRole('searchbox', { name: strings.chat.searchPlaceholder })],
    ['a row', () => screen.getByRole('button', { name: /Fix flaky auth test/ })],
    ['a row’s More', () => rowMore('iOS push tokens')],
    ['a device header', () => screen.getByRole('button', { name: /mac-studio-office/ })],
  ];

  it.each(takenAway)('goes to the toggle when the shortcut folds away %s', (_, control) => {
    showSidebar();
    control().focus();

    pressShortcut();

    expect(aside()).toHaveClass('collapsed');
    expect(openToggle()).toHaveFocus();
  });

  it('goes to the toggle when a click that leaves focus where it was folds, as Safari’s does', () => {
    showSidebar();
    screen.getByRole('searchbox').focus();

    // A click event alone, which moves no focus.
    fireEvent.click(closeToggle());

    expect(openToggle()).toHaveFocus();
  });

  it('goes to the toggle when the shortcut opens the sidebar from the rail’s Search', async () => {
    showSidebar();
    await userEvent.click(closeToggle());
    screen.getByRole('button', { name: strings.common.search }).focus();

    pressShortcut();

    expect(aside()).not.toHaveClass('collapsed');
    expect(closeToggle()).toHaveFocus();
  });

  it('stays on New session and on the account button, which both states draw', () => {
    showSidebar();
    const newSession = screen.getByRole('button', { name: strings.chat.newSession });
    newSession.focus();
    pressShortcut();
    expect(newSession).toHaveFocus();

    const account = screen.getByRole('button', { name: strings.accountMenu.button('alice') });
    account.focus();
    pressShortcut();
    expect(account).toHaveFocus();
  });

  it('is left alone outside the sidebar', () => {
    render(
      <MemoryRouter>
        <Sidebar activeKey="" onNewSession={vi.fn()} />
        <button type="button">composer</button>
      </MemoryRouter>,
    );
    const outside = screen.getByRole('button', { name: 'composer' });
    outside.focus();

    pressShortcut();

    expect(aside()).toHaveClass('collapsed');
    expect(outside).toHaveFocus();
  });
});

describe('the stored choice', () => {
  afterEach(() => readSettingsFor(null));

  it('is the account’s: each account reads its own back', () => {
    readSettingsFor('fold.a');
    useSettings.getState().setSidebarCollapsed(true);

    readSettingsFor('fold.b');
    expect(useSettings.getState().sidebarCollapsed).toBe(false);

    readSettingsFor('fold.a');
    expect(useSettings.getState().sidebarCollapsed).toBe(true);
  });

  it('reads as open from a stored state written before the rail', () => {
    void useSettings.persist.getOptions().storage?.setItem('rc.settings.fold.c', {
      state: { language: 'en', collapsedDevices: ['dev-mac'] },
      version: 1,
    });

    readSettingsFor('fold.c');

    expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);
    expect(useSettings.getState().sidebarCollapsed).toBe(false);
  });
});

describe('how the fold is drawn', () => {
  const css = (...path: string[]): string =>
    readFileSync(join(process.cwd(), 'src', ...path), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
  const rule = (source: string, selector: string): string => {
    const start = source.indexOf(`${selector} {`);
    expect(start, `${selector} is missing`).toBeGreaterThan(-1);
    return source.slice(source.indexOf('{', start) + 1, source.indexOf('}', start));
  };

  it('is a 52 px rail beside a column the sidebar’s own width sets', () => {
    expect(rule(css('styles', 'tokens.css'), ':root')).toContain('--sidebar-rail-w: 52px');
    expect(rule(css('features', 'chat', 'chat.css'), '.chat-layout')).toContain(
      'grid-template-columns: auto minmax(0, 1fr)',
    );
    const sidebar = css('features', 'chat', 'sidebar.css');
    expect(rule(sidebar, '.sidebar')).toContain('width: var(--sidebar-w)');
    expect(rule(sidebar, '.sidebar')).toContain('overflow: clip');
    expect(rule(sidebar, '.sidebar.collapsed')).toContain('width: var(--sidebar-rail-w)');
  });

  it('slides on the motion tokens, which reduced motion sets to nothing, and clips rather than reflows', () => {
    const sidebar = css('features', 'chat', 'sidebar.css');
    expect(rule(sidebar, '.sidebar.moving')).toContain('transition: width var(--dur) var(--ease)');
    expect(rule(sidebar, '.sidebar-inner')).toContain('width: var(--sidebar-w)');
    expect(rule(sidebar, '.sidebar.collapsed .sidebar-inner')).toContain('width: var(--sidebar-rail-w)');
    expect(css('styles', 'tokens.css')).toMatch(/prefers-reduced-motion: reduce[\s\S]*--dur: 0ms/);
  });

  it('is not drawn below 1024 px, folded or not', () => {
    const sidebar = css('features', 'chat', 'sidebar.css');
    const narrow = sidebar.slice(sidebar.indexOf('@media (max-width: 1023px)'));
    expect(rule(narrow, '.sidebar')).toContain('display: none');
  });

  /**
   * The list scrolls above the foot, and a row it cuts off used to run into the
   * count. The foot is a bar, and a bar's edge is where a hairline goes; the
   * rail's foot has nothing above it to part from.
   */
  it('parts the open sidebar’s foot from the list with a hairline, and the rail’s with none', () => {
    const sidebar = css('features', 'chat', 'sidebar.css');
    expect(rule(sidebar, '.sidebar-foot')).toContain('border-top: 1px solid var(--hairline)');
    expect(rule(sidebar, '.sidebar.collapsed .sidebar-foot')).toContain('border-top: none');
    expect(rule(sidebar, '.sidebar-list')).toContain('overflow-y: auto');
  });

  it('draws the foot after the list, holding the count and then the account', () => {
    showSidebar();
    const foot = aside().querySelector('.sidebar-foot');

    expect(foot?.previousElementSibling).toBe(screen.getByRole('navigation'));
    expect(foot?.firstElementChild).toHaveTextContent(
      strings.chat.sidebarFooter(devices.length, waitingNow()),
    );
    expect(foot?.lastElementChild).toHaveClass('account-button');
  });
});
