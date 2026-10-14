/**
 * The chat sidebar follows the session row's rule (`docs/DESIGN.md` § "The
 * session row says where it came from"): the sub-line is the folder and the
 * time whatever the session is doing, the dot alone carries the state, and the
 * legend belongs to the Sessions screen rather than here.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { Sidebar } from '../src/features/chat/Sidebar';
import { useDevices } from '../src/stores/devices';
import { keyOf, useSessions } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { devices, sessions } from '../mock/fixtures';
import type { Session } from '../src/protocol/types';

const index = (list: Session[]): Record<string, Session> =>
  Object.fromEntries(list.map((s) => [keyOf(s), s]));

beforeEach(() => {
  useDevices.setState({ devices, loaded: true, error: null });
  useSessions.setState({ sessions: index(sessions), loaded: true, agentFilter: null });
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

  it('draws no legend', () => {
    showSidebar();

    expect(document.querySelector('.session-legend')).toBeNull();
  });
});
