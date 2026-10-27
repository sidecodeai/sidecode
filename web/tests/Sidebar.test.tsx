/**
 * The chat sidebar follows the session row's rule (`docs/DESIGN.md` § "The
 * session row says where it came from"): the sub-line is the folder and the
 * time whatever the session is doing, the dot alone carries the state, and the
 * legend belongs to the Sessions screen rather than here.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { Sidebar } from '../src/features/chat/Sidebar';
import { useDevices } from '../src/stores/devices';
import { keyOf, useSessions } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { strings } from '../src/strings';
import { devices, sessions } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

const index = (list: Session[]): Record<string, Session> =>
  Object.fromEntries(list.map((s) => [keyOf(s), s]));

beforeEach(() => {
  useDevices.setState({ devices, loaded: true, error: null });
  useSessions.setState({
    sessions: index(sessions),
    loaded: true,
    agentFilter: null,
    statusFilter: 'all',
  });
  useSettings.setState({ collapsedDevices: [], archiveExpanded: [] });
});

const showSidebar = () =>
  render(
    <MemoryRouter>
      <Sidebar activeKey="" onNewSession={vi.fn()} />
    </MemoryRouter>,
  );

/** The sub-line of one row, by the row's title. */
const subLine = (title: string): string => {
  const item = screen.getByText(title).closest('.sidebar-item');
  if (!item) throw new Error(`no sidebar row for ${title}`);
  return item.querySelector('.sidebar-sub')?.textContent ?? '';
};

describe('the chat sidebar', () => {
  it('reads the folder and the time on every row, whatever the state', () => {
    showSidebar();

    // Remote and running, a terminal the device joined, and a question waiting.
    expect(subLine('Fix flaky auth test')).toBe('gateway · 4m');
    expect(subLine('Wire the channel shim')).toBe('protocol · 2m');
    expect(subLine('Migrate web to Vite 6')).toBe('web · 12m');
  });

  it('says no state word and colours no sub-line', () => {
    showSidebar();

    for (const line of document.querySelectorAll('.sidebar-sub')) {
      expect(line.textContent ?? '').not.toMatch(/running|idle|attached|offline|error/i);
      expect(line.className).toBe('sidebar-sub');
    }
  });

  it('breathes the dot of a working session, and no other', () => {
    showSidebar();
    const dotOf = (title: string) => screen.getByText(title).closest('.sidebar-item')?.querySelector('.dot');

    expect(dotOf('Fix flaky auth test')).toHaveClass('working', 'breathes');
    expect(dotOf('Migrate web to Vite 6')).not.toHaveClass('breathes');
    expect(dotOf('Wire the channel shim')).not.toHaveClass('breathes');
  });

  it('draws no legend', () => {
    showSidebar();

    expect(document.querySelector('.session-legend')).toBeNull();
  });

  it('shares the agent filter with the Sessions page, and leaves the status filter to it', () => {
    useSessions.setState({ agentFilter: 'codex', statusFilter: 'running' });
    showSidebar();

    // A conversation changes status under the reader's eyes, so a status that
    // narrowed this list would drop the open conversation's own row.
    expect(screen.getByText('Typecheck the web app')).toBeInTheDocument();
    expect(screen.getByText('Add OTLP traces')).toBeInTheDocument();
    expect(screen.queryByText('Fix flaky auth test')).not.toBeInTheDocument();
  });

  /**
   * The sidebar has a search of its own, which reveals groups as the Sessions
   * page's does: a click on a header then changes nothing on screen and would
   * only flip the stored choice, so the header takes none.
   */
  describe('while its own search is showing the groups', () => {
    const searchField = () => screen.getByLabelText(strings.chat.searchPlaceholder);
    const deviceHeader = (name: string) => screen.getByRole('button', { name: new RegExp(name) });

    it('takes no click on a device header', async () => {
      showSidebar();
      await userEvent.type(searchField(), 'flaky');

      const header = deviceHeader('mac-studio-office');
      expect(header).toHaveAttribute('aria-disabled', 'true');
      await userEvent.click(header);

      expect(useSettings.getState().collapsedDevices).toEqual([]);
      expect(screen.getByText('Fix flaky auth test')).toBeInTheDocument();
    });

    it('takes no click on an Archive header that the search has opened', async () => {
      showSidebar();
      await userEvent.type(searchField(), 'pairing docs');

      const archive = screen.getByRole('button', { name: strings.sessions.archiveGroup(1) });
      expect(archive).toHaveAttribute('aria-disabled', 'true');
      await userEvent.click(archive);

      expect(useSettings.getState().archiveExpanded).toEqual([]);
      expect(screen.getByText('Rewrite the pairing docs')).toBeInTheDocument();
    });

    it('hands the clicks back once the search is cleared', async () => {
      showSidebar();
      await userEvent.type(searchField(), 'flaky');
      await userEvent.clear(searchField());

      expect(deviceHeader('mac-studio-office')).not.toHaveAttribute('aria-disabled');
      await userEvent.click(deviceHeader('mac-studio-office'));
      expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);

      const archive = screen.getByRole('button', { name: strings.sessions.archiveGroup(2) });
      expect(archive).not.toHaveAttribute('aria-disabled');
    });

    it('stays live under a status filter, which the sidebar does not apply', async () => {
      useSessions.setState({ statusFilter: 'running' });
      showSidebar();

      const header = deviceHeader('mac-studio-office');
      expect(header).not.toHaveAttribute('aria-disabled');
      await userEvent.click(header);

      expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);
    });
  });
});
