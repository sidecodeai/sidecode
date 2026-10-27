/**
 * A55 — where Rename is offered in the browser: the chat header's title, and
 * a context menu on the Sessions page's rows and the chat sidebar's items.
 * Each is offered exactly when the session's agent lists `rename` on its device
 * and the device is online — terminal-held rows and rows in the Archive
 * included — and the row's own Close control is left as it was.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, RouterProvider, createMemoryRouter } from 'react-router';
import { ChatHeader } from '../src/features/chat/ChatHeader';
import { ChatPage } from '../src/features/chat/ChatPage';
import { Sidebar } from '../src/features/chat/Sidebar';
import { SessionsPage } from '../src/features/sessions/SessionsPage';
import { RequestError } from '../src/lib/ws';
import { useChat } from '../src/stores/chat';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { keyOf, useSessions } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import { claudeAgent, claudeNoShim, devices, grokNoLeader, sessions } from '../mock/fixtures';
import type { AgentInfo, Session } from '../src/protocol/types';

const { rpcMock } = vi.hoisted(() => ({ rpcMock: vi.fn() }));

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => null,
  setSocket: () => undefined,
}));

const index = (list: Session[]): Record<string, Session> =>
  Object.fromEntries(list.map((s) => [keyOf(s), s]));

const named = (title: string): Session => {
  const found = sessions.find((s) => s.title === title);
  if (!found) throw new Error(`no mock session titled ${title}`);
  return found;
};

beforeEach(() => {
  rpcMock.mockReset();
  useDevices.setState({ devices: [...devices], loaded: true, error: null });
  useSessions.setState({
    sessions: index(sessions),
    loaded: true,
    agentFilter: null,
    statusFilter: 'all',
  });
  useSettings.setState({ collapsedDevices: [], archiveExpanded: [], language: 'en' });
});

const dialog = (): HTMLElement => screen.getByRole('dialog', { name: strings.sessions.renameTitle });
const field = (): HTMLInputElement =>
  within(dialog()).getByRole('textbox', { name: strings.sessions.renameLabel });
const save = (): HTMLElement => within(dialog()).getByRole('button', { name: strings.common.save });
const menu = (): HTMLElement | null => screen.queryByRole('menu');

/** What a device answers a rename with: the session, named, and no newer. */
const answerWith = (target: Session, title: string) =>
  rpcMock.mockResolvedValueOnce({ session: { ...target, title } });

describe('the Sessions page', () => {
  const page = () =>
    render(
      <MemoryRouter>
        <SessionsPage />
      </MemoryRouter>,
    );

  const rowFor = (title: string): HTMLElement => {
    const row = screen.getByText(title).closest('.session-row');
    if (!row) throw new Error(`no row for ${title}`);
    return row as HTMLElement;
  };

  /** Right-click a row: whether a menu of ours opened, and the browser's own was kept away. */
  const rightClick = (title: string): { opened: boolean; kept: boolean } => {
    const proceed = fireEvent.contextMenu(rowFor(title), { clientX: 200, clientY: 120 });
    const opened = menu() !== null;
    if (opened) fireEvent.keyDown(document, { key: 'Escape' });
    return { opened, kept: !proceed };
  };

  it('opens a menu of Rename alone on a right-click, named for the row', () => {
    page();

    fireEvent.contextMenu(rowFor('iOS push tokens'), { clientX: 200, clientY: 120 });

    const open = screen.getByRole('menu', { name: 'iOS push tokens' });
    expect(within(open).getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
      'Rename',
    ]);
  });

  it('opens the dialog on that row’s title, and sends the name alone', async () => {
    const target = named('iOS push tokens');
    answerWith(target, 'Push tokens for iOS');
    page();

    fireEvent.contextMenu(rowFor('iOS push tokens'), { clientX: 200, clientY: 120 });
    await userEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));

    expect(menu()).toBeNull();
    expect(field()).toHaveValue('iOS push tokens');
    await userEvent.clear(field());
    await userEvent.type(field(), 'Push tokens for iOS');
    await userEvent.click(save());

    expect(rpcMock).toHaveBeenCalledWith('session.set', {
      session_id: target.session_id,
      title: 'Push tokens for iOS',
    });
  });

  it('draws the reply’s title on the row when the device answers, and the row stays in its place', async () => {
    const target = named('iOS push tokens');
    const titles = () =>
      [...document.querySelectorAll('.session-group > .session-list .session-title')].map(
        (el) => el.textContent,
      );
    page();
    const before = titles();
    // Claude suffixed the name another live session held.
    answerWith(target, 'Fix flaky auth test (2)');

    fireEvent.contextMenu(rowFor('iOS push tokens'), { clientX: 200, clientY: 120 });
    await userEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));
    await userEvent.clear(field());
    await userEvent.type(field(), 'Fix flaky auth test');
    await userEvent.click(save());

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('Fix flaky auth test (2)')).toBeInTheDocument();
    expect(screen.queryByText('iOS push tokens')).not.toBeInTheDocument();
    const after = titles();
    expect(after.indexOf('Fix flaky auth test (2)')).toBe(before.indexOf('iOS push tokens'));
    expect(after.filter((title) => title !== 'Fix flaky auth test (2)')).toEqual(
      before.filter((title) => title !== 'iOS push tokens'),
    );
  });

  it('leaves the row’s Close control as it was', () => {
    page();

    const row = rowFor('iOS push tokens');
    expect(within(row).getByRole('button', { name: strings.sessions.close })).toBeInTheDocument();
    // Rename is not on the row: it is the menu’s, and the header’s.
    expect(within(row).queryByRole('button', { name: strings.common.rename })).toBeNull();
  });

  it('is offered on every row whose agent lists rename on a device that is online', () => {
    useSettings.setState({ archiveExpanded: ['dev-mac', 'dev-ci'] });
    page();

    // Running, idle, waiting, errored; every agent; a terminal that holds the
    // session, a shared one, and the Archive's hand-archived rows.
    for (const title of [
      'Fix flaky auth test',
      'iOS push tokens',
      'Migrate web to Vite 6',
      'Regenerate the API client',
      'Typecheck the web app',
      'Port the hook allowlist',
      'Sketch the RPC event assembler',
      'Refactor relay routing',
      'Wire the channel shim',
      'Nightly perf sweep',
      'Bisect the ingest regression',
      'Rework the leader reconnect',
      'Rewrite the pairing docs',
      'Sketch the pairing QR flow',
      'Drop the legacy ingest path',
    ]) {
      expect(rightClick(title), title).toEqual({ opened: true, kept: true });
    }
  });

  it('is not offered for an agent the device does not list rename for', () => {
    page();

    // Grok Build on the device whose leader is off lists no `rename`.
    expect(rightClick('Profile the wheel build')).toEqual({ opened: false, kept: false });
    expect(grokNoLeader.capabilities).not.toContain('rename');
  });

  it('is not offered while the device is offline, and the browser’s own menu is left alone', () => {
    useDevices.setState({
      devices: devices.map((d) => (d.device_id === 'dev-mac' ? { ...d, online: false } : d)),
      loaded: true,
      error: null,
    });
    page();

    expect(rightClick('iOS push tokens')).toEqual({ opened: false, kept: false });
    // The device that is still online keeps it.
    expect(rightClick('Add OTLP traces')).toEqual({ opened: true, kept: true });
  });

  it('is not offered for a device from before the amendment, whatever version it runs', () => {
    useDevices.setState({
      devices: devices.map((d) =>
        d.device_id === 'dev-mac'
          ? {
              ...d,
              client_version: '99.0.0',
              agents: d.agents.map((agent) => ({
                ...agent,
                capabilities: agent.capabilities.filter((c) => c !== 'rename'),
              })),
            }
          : d,
      ),
      loaded: true,
      error: null,
    });
    page();

    expect(rightClick('iOS push tokens')).toEqual({ opened: false, kept: false });
  });

  it('opens from the keyboard on the ContextMenu key and on Shift+F10', async () => {
    page();
    const open = within(rowFor('iOS push tokens')).getByRole('button', {
      name: strings.sessions.open,
    });

    open.focus();
    await userEvent.keyboard('{ContextMenu}');
    expect(screen.getByRole('menuitem', { name: 'Rename' })).toHaveFocus();
    await userEvent.keyboard('{Escape}');
    expect(menu()).toBeNull();
    expect(open).toHaveFocus();

    await userEvent.keyboard('{Shift>}{F10}{/Shift}');
    await userEvent.keyboard('{Enter}');
    expect(field()).toHaveValue('iOS push tokens');
  });

  it('closes on Escape and on a press outside', async () => {
    page();

    fireEvent.contextMenu(rowFor('iOS push tokens'), { clientX: 200, clientY: 120 });
    await userEvent.keyboard('{Escape}');
    expect(menu()).toBeNull();

    fireEvent.contextMenu(rowFor('iOS push tokens'), { clientX: 200, clientY: 120 });
    await userEvent.click(screen.getByText(strings.sessions.title));
    expect(menu()).toBeNull();
  });

  it('keeps the dialog when a status change moves its row to another list', async () => {
    const target = named('iOS push tokens');
    page();
    fireEvent.contextMenu(rowFor('iOS push tokens'), { clientX: 200, clientY: 120 });
    await userEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));
    fireEvent.change(field(), { target: { value: 'iOS push tokens (again)' } });

    // The CLI exits while the dialog is open: the row leaves Active for the
    // Archive, which unmounts the row it was opened from.
    act(() => {
      useSessions.getState().upsert({ ...target, control: 'none', state: 'stopped' });
    });

    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(field()).toHaveValue('iOS push tokens (again)');
  });

  it('is in Chinese when the interface is', async () => {
    useSettings.setState({ language: 'zh-Hans' });
    page();

    fireEvent.contextMenu(rowFor('iOS push tokens'), { clientX: 200, clientY: 120 });
    expect(screen.getByRole('menuitem', { name: '重命名' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('menuitem', { name: '重命名' }));

    expect(screen.getByRole('dialog', { name: '重命名会话' })).toBeInTheDocument();
    expect(screen.getByText('会话名称')).toBeInTheDocument();
  });
});

describe('the chat sidebar', () => {
  const sidebar = () =>
    render(
      <MemoryRouter>
        <Sidebar activeKey="" onNewSession={vi.fn()} />
      </MemoryRouter>,
    );

  const itemFor = (title: string): HTMLElement => {
    const item = screen.getByText(title).closest('li');
    if (!item) throw new Error(`no sidebar item for ${title}`);
    return item as HTMLElement;
  };

  it('opens a menu of Rename on a right-click, and the dialog from it', async () => {
    const target = named('iOS push tokens');
    answerWith(target, 'Push tokens, renamed');
    sidebar();

    fireEvent.contextMenu(itemFor('iOS push tokens'), { clientX: 60, clientY: 200 });
    expect(
      within(screen.getByRole('menu', { name: 'iOS push tokens' }))
        .getAllByRole('menuitem')
        .map((item) => item.textContent),
    ).toEqual(['Rename']);
    await userEvent.click(screen.getByRole('menuitem', { name: 'Rename' }));
    expect(field()).toHaveValue('iOS push tokens');
    await userEvent.clear(field());
    await userEvent.type(field(), 'Push tokens, renamed');
    await userEvent.click(save());

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(rpcMock).toHaveBeenCalledWith('session.set', {
      session_id: target.session_id,
      title: 'Push tokens, renamed',
    });
    expect(screen.getByText('Push tokens, renamed')).toBeInTheDocument();
    expect(screen.queryByText('iOS push tokens')).not.toBeInTheDocument();
  });

  it('is offered on a terminal-held item and not where the device lists no rename', () => {
    sidebar();

    expect(fireEvent.contextMenu(itemFor('Nightly perf sweep'))).toBe(false);
    expect(menu()).not.toBeNull();
    fireEvent.keyDown(document, { key: 'Escape' });

    expect(fireEvent.contextMenu(itemFor('Profile the wheel build'))).toBe(true);
    expect(menu()).toBeNull();
  });

  it('opens from the keyboard on the focused item', async () => {
    sidebar();
    within(itemFor('iOS push tokens')).getByRole('button').focus();

    await userEvent.keyboard('{ContextMenu}');

    expect(screen.getByRole('menuitem', { name: 'Rename' })).toHaveFocus();
  });
});

describe('the chat header', () => {
  const target = named('iOS push tokens');

  const header = (
    over: { session?: Session; agent?: AgentInfo | null; deviceOnline?: boolean } = {},
  ) =>
    render(
      <MemoryRouter>
        <ChatHeader
          session={over.session ?? target}
          agent={over.agent === undefined ? claudeAgent : over.agent}
          deviceName="mac-studio-office"
          deviceOnline={over.deviceOnline ?? true}
          todos={[]}
          stopping={false}
          onStop={vi.fn()}
        />
      </MemoryRouter>,
    );

  const heading = (): HTMLElement => screen.getByRole('heading', { level: 1 });

  it('makes the title a control when the agent lists rename and the device is online', () => {
    header();

    const title = within(heading()).getByRole('button', { name: 'iOS push tokens' });
    expect(title).toHaveAttribute('title', strings.common.rename);
    expect(title).toHaveAttribute('aria-haspopup', 'dialog');
    expect(heading()).toHaveTextContent('iOS push tokens');
  });

  it('leaves the title plain text where Rename is not offered', () => {
    const withoutRename: AgentInfo = {
      ...claudeAgent,
      capabilities: claudeAgent.capabilities.filter((c) => c !== 'rename'),
    };
    for (const over of [
      { agent: null },
      { agent: withoutRename },
      { agent: grokNoLeader },
      { deviceOnline: false },
    ]) {
      const { unmount } = header(over);

      expect(within(heading()).queryByRole('button')).toBeNull();
      expect(heading()).toHaveTextContent('iOS push tokens');
      unmount();
    }
  });

  it('is offered on the agent without the shim too: Claude’s rename needs none', () => {
    header({ agent: claudeNoShim });

    expect(within(heading()).getByRole('button')).toBeInTheDocument();
  });

  it('opens the dialog on the title with a click, and Escape closes it', async () => {
    header();

    await userEvent.click(within(heading()).getByRole('button'));

    expect(field()).toHaveValue('iOS push tokens');
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(within(heading()).getByRole('button')).toHaveFocus();
  });

  it('opens on Enter from the keyboard', async () => {
    header();
    within(heading()).getByRole('button').focus();

    await userEvent.keyboard('{Enter}');

    expect(dialog()).toBeInTheDocument();
  });

  it('names an untitled session, and opens its dialog empty', async () => {
    header({ session: { ...target, title: '' } });

    const title = within(heading()).getByRole('button');
    expect(title).toHaveTextContent(strings.sessions.untitled);
    await userEvent.click(title);

    expect(field()).toHaveValue('');
    expect(save()).toBeDisabled();
  });

  it('draws the title in the reply once the device answers, not the one typed', async () => {
    // The header is handed the session the store holds, as the chat page does.
    function Live() {
      const live = useSessions((s) => s.sessions[keyOf(target)]!);
      return (
        <MemoryRouter>
          <ChatHeader
            session={live}
            agent={claudeAgent}
            deviceName="mac-studio-office"
            deviceOnline
            todos={[]}
            stopping={false}
            onStop={vi.fn()}
          />
        </MemoryRouter>
      );
    }
    render(<Live />);
    answerWith(target, 'Fix flaky auth test (2)');

    await userEvent.click(within(heading()).getByRole('button'));
    await userEvent.clear(field());
    await userEvent.type(field(), 'Fix flaky auth test');
    await userEvent.click(save());

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(heading()).toHaveTextContent('Fix flaky auth test (2)');
  });

  it('keeps the dialog open with a line under the field when the device refuses', async () => {
    header();
    rpcMock.mockRejectedValueOnce(new RequestError({ code: 'conflict', message: 'busy' }));

    await userEvent.click(within(heading()).getByRole('button'));
    await userEvent.clear(field());
    await userEvent.type(field(), 'Another name');
    await userEvent.click(save());

    expect(await within(dialog()).findByRole('alert')).toBeInTheDocument();
    expect(field()).toHaveValue('Another name');
    expect(heading()).toHaveTextContent('iOS push tokens');
  });
});

describe('the chat page', () => {
  // jsdom has no scrolling, and the timeline scrolls itself to the latest row
  // on mount. Stubbed the way `ChatPage.test.tsx` stubs it.
  const nativeScrollTo = HTMLElement.prototype.scrollTo;
  beforeAll(() => {
    HTMLElement.prototype.scrollTo = function scrollToStub(this: HTMLElement) {
      // Nothing to scroll: this suite is about the header.
    } as HTMLElement['scrollTo'];
  });
  afterAll(() => {
    HTMLElement.prototype.scrollTo = nativeScrollTo;
  });

  beforeEach(() => {
    useChat.setState({ sessions: {} });
    // Never 'open', so the page does not try to subscribe over the stand-in socket.
    useConnection.setState({ status: 'closed' });
  });

  const route = (path: string) => {
    const router = createMemoryRouter(
      [{ path: '/sessions/:deviceId/:sessionId', element: <ChatPage /> }],
      { initialEntries: [path] },
    );
    render(<RouterProvider router={router} />);
    return router;
  };

  const heading = (): HTMLElement => screen.getByRole('heading', { level: 1 });

  it('reads the agent and the device it hands the header, and follows the device offline', () => {
    route('/sessions/dev-mac/ses-push');

    expect(within(heading()).getByRole('button', { name: 'iOS push tokens' })).toBeInTheDocument();

    act(() => {
      useDevices.setState({
        devices: devices.map((d) => (d.device_id === 'dev-mac' ? { ...d, online: false } : d)),
      });
    });
    expect(within(heading()).queryByRole('button')).toBeNull();
    expect(heading()).toHaveTextContent('iOS push tokens');
  });

  it('offers nothing for an agent the device lists no rename for', () => {
    route('/sessions/dev-ci/ses-grok-no-leader');

    expect(heading()).toHaveTextContent('Profile the wheel build');
    expect(within(heading()).queryByRole('button')).toBeNull();
  });

  it('keeps a sidebar menu open while the transcript scrolls to its tail, and closes it when the sidebar scrolls', () => {
    route('/sessions/dev-mac/ses-flaky');
    const item = screen.getByText('iOS push tokens').closest('li');
    if (!item) throw new Error('no sidebar item');

    fireEvent.contextMenu(item, { clientX: 60, clientY: 200 });
    expect(screen.getByRole('menu', { name: 'iOS push tokens' })).toBeInTheDocument();

    // A streaming turn scrolls the transcript on every flush, beside the sidebar.
    const transcript = document.querySelector('.timeline');
    if (!transcript) throw new Error('no transcript');
    for (let i = 0; i < 5; i += 1) fireEvent.scroll(transcript);
    expect(screen.getByRole('menu', { name: 'iOS push tokens' })).toBeInTheDocument();

    // The sidebar's own list moves the item from under the menu.
    const list = document.querySelector('.sidebar-list');
    if (!list) throw new Error('no sidebar list');
    fireEvent.scroll(list);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('closes a dialog open on one conversation when the route moves to another', async () => {
    const router = route('/sessions/dev-mac/ses-push');
    await userEvent.click(within(heading()).getByRole('button'));
    expect(field()).toHaveValue('iOS push tokens');

    await act(async () => {
      await router.navigate('/sessions/dev-mac/ses-flaky');
    });

    expect(screen.queryByRole('dialog', { name: strings.sessions.renameTitle })).toBeNull();
    expect(heading()).toHaveTextContent('Fix flaky auth test');
  });
});
