/**
 * The Sessions page renders the grouping rule: one collapsible group per
 * device, that device's active rows, then its own collapsed Archive, with an
 * agent filter shared with the chat sidebar.
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { SessionsPage } from '../src/features/sessions/SessionsPage';
import { useDevices } from '../src/stores/devices';
import { keyOf, useSessions } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { en, stringTables, strings } from '../src/strings';
import { devices, sessions } from '../mock/fixtures';
import type { Device, Session } from '../src/protocol/types';

/** A paired device with nothing on it at all. */
const quietDevice: Device = {
  device_id: 'dev-quiet',
  name: 'quiet-box',
  platform: 'linux',
  hostname: 'quiet-box',
  arch: 'x86_64',
  client_version: '0.1.0',
  online: true,
  last_seen: Date.now(),
  created_at: 0,
  latency_ms: 30,
  agents: [],
};

const MAC = 'mac-studio-office';
const CI = 'ci-runner-01';

const index = (list: Session[]): Record<string, Session> =>
  Object.fromEntries(list.map((s) => [keyOf(s), s]));

beforeEach(() => {
  useDevices.setState({ devices: [...devices, quietDevice], loaded: true, error: null });
  useSessions.setState({
    sessions: index(sessions),
    loaded: true,
    agentFilter: null,
    statusFilter: 'all',
  });
  useSettings.setState({ collapsedDevices: [], archiveExpanded: [] });
});

const renderPage = () =>
  render(
    <MemoryRouter>
      <SessionsPage />
    </MemoryRouter>,
  );

const deviceHeader = (name: string) => screen.getByRole('button', { name: new RegExp(name) });

const rowTitles = (): string[] =>
  screen
    .getAllByRole('button', { name: strings.sessions.open })
    .map((row) => row.querySelector('.session-title')?.textContent ?? '');

/** Row titles in a device's Active list, and in its own Archive. */
const titlesIn = (selector: string): string[] =>
  [...document.querySelectorAll(`${selector} .session-title`)].map((el) => el.textContent ?? '');

const activeTitles = (): string[] => titlesIn('.session-group > .session-list');
const archiveTitles = (): string[] => titlesIn('.session-archive-group .session-list');

/** One rendered row, by the title it prints. */
const rowFor = (title: string): HTMLElement => {
  const row = screen.getByText(title).closest('.session-row');
  if (!row) throw new Error(`no row for ${title}`);
  return row as HTMLElement;
};

/** The word beside the dot on a row, by the row's title. */
const wordIn = (title: string): string =>
  rowFor(title).querySelector('.session-state')?.textContent ?? '';

/** The tone class on one row's status dot, by the row's title. */
const dotTone = (title: string): string => {
  const row = screen
    .getAllByRole('button', { name: strings.sessions.open })
    .find((r) => r.querySelector('.session-title')?.textContent === title);
  // `breathes` is the working tone's motion, not a tone of its own.
  return [...(row?.querySelector('.dot')?.classList ?? [])]
    .filter((name) => name !== 'dot' && name !== 'breathes')
    .join(' ');
};

describe('SessionsPage grouping', () => {
  it('prints each device name exactly as the device reports it', () => {
    renderPage();

    expect(screen.getByText(MAC)).toBeInTheDocument();
    expect(screen.getByText(CI)).toBeInTheDocument();
  });

  it('renders no group for a device with nothing on it', () => {
    renderPage();

    expect(screen.queryByText(quietDevice.name)).not.toBeInTheDocument();
  });

  it('offers no global archive toggle', () => {
    renderPage();

    expect(screen.queryByRole('button', { name: /archived/i })).not.toBeInTheDocument();
  });

  it('collapses one device without touching the other, and remembers it', async () => {
    renderPage();

    expect(screen.getByText('Fix flaky auth test')).toBeInTheDocument();

    await userEvent.click(deviceHeader(MAC));

    expect(screen.queryByText('Fix flaky auth test')).not.toBeInTheDocument();
    expect(screen.getByText('Add OTLP traces')).toBeInTheDocument();
    expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);

    await userEvent.click(deviceHeader(MAC));

    expect(screen.getByText('Fix flaky auth test')).toBeInTheDocument();
    expect(useSettings.getState().collapsedDevices).toEqual([]);
  });

  it('keeps a device expanded by default even after another one was folded', () => {
    useSettings.setState({ collapsedDevices: ['dev-ci'] });
    renderPage();

    expect(screen.getByText('Fix flaky auth test')).toBeInTheDocument();
    expect(screen.queryByText('Add OTLP traces')).not.toBeInTheDocument();
  });

  it('gives every device its own collapsed Archive', async () => {
    renderPage();

    // dev-mac: two exited sessions and one archived by hand; dev-ci: one each.
    const macArchive = screen.getByRole('button', { name: strings.sessions.archiveGroup(3) });
    const ciArchive = screen.getByRole('button', { name: strings.sessions.archiveGroup(2) });
    expect(macArchive).toHaveAttribute('aria-expanded', 'false');
    expect(ciArchive).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('Rewrite the pairing docs')).not.toBeInTheDocument();

    await userEvent.click(macArchive);

    expect(screen.getByText('Rewrite the pairing docs')).toBeInTheDocument();
    expect(screen.getByText('Sketch the pairing QR flow')).toBeInTheDocument();
    // The other device's Archive stays shut.
    expect(screen.queryByText('Drop the legacy ingest path')).not.toBeInTheDocument();
    expect(useSettings.getState().archiveExpanded).toEqual(['dev-mac']);
  });

  it('opens the Archive that a search matched, leaving the stored ids alone', async () => {
    renderPage();

    await userEvent.type(
      screen.getByLabelText(strings.sessions.searchPlaceholder),
      'pairing docs',
    );

    expect(screen.getByText('Rewrite the pairing docs')).toBeInTheDocument();
    expect(useSettings.getState().archiveExpanded).toEqual([]);
  });

  it('opens a folded device while a search matches inside it', async () => {
    useSettings.setState({ collapsedDevices: ['dev-mac'] });
    renderPage();

    expect(screen.queryByText('Fix flaky auth test')).not.toBeInTheDocument();

    const search = screen.getByLabelText(strings.sessions.searchPlaceholder);
    await userEvent.type(search, 'flaky');

    expect(screen.getByText('Fix flaky auth test')).toBeInTheDocument();
    expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);

    await userEvent.clear(search);

    expect(screen.queryByText('Fix flaky auth test')).not.toBeInTheDocument();
  });

  it('tags every row with its agent', () => {
    renderPage();

    expect(screen.getAllByText('Claude Code').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Codex').length).toBeGreaterThan(0);
  });

  /** A25: the filter is a menu, so a choice is an option inside it. */
  const pickAgent = async (name: string) => {
    await userEvent.click(screen.getByRole('button', { name: strings.sessions.agentFilter }));
    await userEvent.click(screen.getByRole('option', { name }));
  };

  it('filters by agent and keeps the choice in the sessions store', async () => {
    renderPage();

    await pickAgent('Codex');

    expect(useSessions.getState().agentFilter).toBe('codex');
    expect(rowTitles()).not.toContain('Fix flaky auth test');
    expect(rowTitles()).toContain('Add OTLP traces');

    await pickAgent(strings.sessions.allAgents);

    expect(useSessions.getState().agentFilter).toBeNull();
    expect(rowTitles()).toContain('Fix flaky auth test');
  });

  it('drops a device whose sessions the agent filter removed', async () => {
    useSessions.setState({
      sessions: index(sessions.filter((s) => s.device_id !== 'dev-ci' || s.agent === 'codex')),
    });
    renderPage();

    await pickAgent('Claude Code');

    expect(screen.getByText(MAC)).toBeInTheDocument();
    expect(screen.queryByText(CI)).not.toBeInTheDocument();
  });

  it('puts the sessions that need an answer at the top of their device', () => {
    renderPage();

    expect(rowTitles()[0]).toBe('Migrate web to Vite 6');
  });

  it('tones every dot from the state and the control together', async () => {
    renderPage();

    expect(dotTone('Fix flaky auth test')).toBe('working');
    expect(dotTone('Migrate web to Vite 6')).toBe('waiting');
    expect(dotTone('Split the ingest migration')).toBe('waiting');
    // Idle, but a terminal still holds it: alive rather than exited.
    expect(dotTone('Wire the channel shim')).toBe('live');
    expect(dotTone('iOS push tokens')).toBe('live');
    expect(dotTone('Regenerate the API client')).toBe('failed');

    await userEvent.click(screen.getByRole('button', { name: strings.sessions.archiveGroup(3) }));

    expect(dotTone('Rewrite the pairing docs')).toBe('off');
  });

  /**
   * A15: the device clears `archived` as a turn starts, and the row has to
   * leave the Archive on that one `session.updated` alone — no reload, and no
   * second frame. `upsert` is exactly what the socket handler calls.
   */
  it('moves a resumed session from the Archive to the Active rows (A15)', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: strings.sessions.archiveGroup(3) }));

    expect(archiveTitles()).toContain('Sketch the pairing QR flow');
    expect(activeTitles()).not.toContain('Sketch the pairing QR flow');

    const revived = useSessions.getState().sessions['dev-mac/ses-archived-mac']!;
    act(() =>
      useSessions
        .getState()
        .upsert({ ...revived, archived: false, control: 'terminal', state: 'running' }),
    );

    expect(activeTitles()).toContain('Sketch the pairing QR flow');
    expect(archiveTitles()).not.toContain('Sketch the pairing QR flow');
    // Only this device's Archive is open, and it is one row shorter.
    expect(archiveTitles()).toHaveLength(2);
  });
});

/**
 * `docs/DESIGN.md` § "The session row": three lines — title and time, agent and
 * origin, then the working directory alone after a folder glyph.
 */
describe('the session row', () => {
  /** Every word the visible rows print beside their dots. */
  const words = (): string[] =>
    [...document.querySelectorAll('.session-state')].map((el) => el.textContent ?? '');

  it('puts the agent at the leading edge and the origin at the trailing edge of the second line', () => {
    renderPage();
    const [first, second, ...rest] = rowFor('Fix flaky auth test').querySelectorAll('.session-line');
    if (!first || !second) throw new Error('the row has fewer than two lines');
    expect(rest).toHaveLength(0);
    expect(first.querySelector('.session-title')?.textContent).toBe('Fix flaky auth test');
    expect(first.querySelector('.session-time')).not.toBeNull();
    expect(second.firstElementChild?.classList.contains('agent-chip')).toBe(true);
    expect(second.lastElementChild?.classList.contains('session-state')).toBe(true);
    expect(second.querySelector('.session-state .dot')).not.toBeNull();
    expect(second.textContent).not.toContain('~/');
  });

  it('gives the working directory the third line, after an unfilled outline folder', () => {
    renderPage();
    const cwd = rowFor('Fix flaky auth test').querySelector('.session-cwd');
    if (!cwd) throw new Error('no cwd line');
    const folder = cwd.querySelector('svg.session-folder');
    if (!folder) throw new Error('no folder');
    expect(folder.getAttribute('fill')).toBe('none');
    expect(folder.getAttribute('stroke-linecap')).toBe('round');
    expect(folder.getAttribute('stroke-linejoin')).toBe('round');
    expect(folder.getAttribute('aria-hidden')).toBe('true');
    expect(cwd.querySelector('.session-path bdi')?.textContent).toBe('~/dev/remote-control/gateway');
  });

  /**
   * `docs/DESIGN.md` § "The session row says where it came from": the word is
   * the origin whatever the state, and the state is the dot's colour alone.
   */
  it('says where a session came from, not what it is doing', () => {
    renderPage();

    // Running here; running inside a terminal; a terminal the device joined.
    expect(wordIn('Fix flaky auth test')).toBe('Remote launch');
    expect(dotTone('Fix flaky auth test')).toBe('working');
    expect(wordIn('Refactor relay routing')).toBe('Terminal');
    expect(wordIn('Wire the channel shim')).toBe('Terminal');
    // A question waiting says nothing more than a quiet row does.
    expect(wordIn('Migrate web to Vite 6')).toBe('Remote launch');
    expect(dotTone('Migrate web to Vite 6')).toBe('waiting');
  });

  it('breathes the dot of a working row, and no other', () => {
    renderPage();
    const dotOf = (title: string) => rowFor(title).querySelector('.dot');

    expect(dotOf('Fix flaky auth test')).toHaveClass('working', 'breathes');
    expect(dotOf('Refactor relay routing')).toHaveClass('working', 'breathes');
    expect(dotOf('Migrate web to Vite 6')).toHaveClass('waiting');
    expect(dotOf('Migrate web to Vite 6')).not.toHaveClass('breathes');
    expect(dotOf('iOS push tokens')).not.toHaveClass('breathes');
  });

  it('gives the word the secondary ink whatever the state', () => {
    renderPage();
    expect(document.querySelector('.session-state.attention')).toBeNull();
  });

  it('prints no state word on any row', () => {
    renderPage();
    for (const word of words()) {
      expect(word).not.toMatch(/running|idle|attached|offline|error/i);
    }
  });

  it('keeps "Archived" before the origin on a hand-archived row', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: strings.sessions.archiveGroup(2) }));

    expect(wordIn('Drop the legacy ingest path')).toBe('Archived · Remote launch');
  });

  it('says the origin on an offline device too, and greys the dot', () => {
    useDevices.setState({
      devices: devices.map((d) => (d.name === CI ? { ...d, online: false } : d)),
      loaded: true,
      error: null,
    });
    renderPage();

    expect(wordIn('Nightly perf sweep')).toBe('Terminal');
    expect(dotTone('Nightly perf sweep')).toBe('off');
    // The group header already says the machine is offline; the row does not.
    for (const word of words()) expect(word).not.toMatch(/offline/i);
  });
});

/**
 * `docs/DESIGN.md` § "A legend, once, and quiet": what the colours mean, said
 * once under the toolbar, in four entries rather than five.
 */
describe('the dot legend', () => {
  const legend = (): HTMLElement | null => document.querySelector('.session-legend');

  it('reads the four colours in order, drawn once', () => {
    renderPage();
    const line = legend();
    if (!line) throw new Error('no legend');

    expect(document.querySelectorAll('.session-legend')).toHaveLength(1);
    expect([...line.querySelectorAll('.session-legend-entry')].map((e) => e.textContent)).toEqual([
      'Working',
      'For you',
      'Not running',
      'Error',
    ]);
    // The amber entry takes the still tone: the pulsing one is the same colour.
    expect([...line.querySelectorAll('.dot')].map((d) => d.className)).toEqual([
      'dot working',
      'dot live',
      'dot off',
      'dot failed',
    ]);
  });

  it('sits between the toolbar and the first group', () => {
    renderPage();
    const line = legend();
    expect(line?.previousElementSibling?.className).toBe('sessions-toolbar');
    expect(line?.nextElementSibling?.className).toBe('session-group');
  });

  it('is not drawn when the list is empty', () => {
    useSessions.setState({ sessions: {}, loaded: true, agentFilter: null });
    renderPage();

    expect(legend()).toBeNull();
    expect(screen.getByText(strings.sessions.empty)).toBeInTheDocument();
  });
});

/**
 * The filter by status: a menu beside the search and the agent filter whose
 * options read the dot a row shows (`tests/statusFilter.test.ts` is the rule),
 * kept in the sessions store like the agent filter.
 */
describe('the filter by status', () => {
  afterEach(() => {
    useSettings.setState({ language: 'en' });
  });

  const trigger = () => screen.getByRole('button', { name: strings.sessions.statusFilter });

  const pickStatus = async (name: string) => {
    await userEvent.click(trigger());
    await userEvent.click(screen.getByRole('option', { name }));
  };

  /** The tone of every row on the page. */
  const tones = (): Set<string> => new Set(rowTitles().map(dotTone));

  const quiet = sessions.find((s) => s.session_id === 'ses-push') as Session;

  it('is a menu of All states, Running, Waiting and Ended, with All marked', async () => {
    renderPage();
    await userEvent.click(trigger());

    const options = screen.getAllByRole('option');
    expect(options.map((o) => o.textContent)).toEqual(['All states', 'Running', 'Waiting', 'Ended']);
    expect(options.map((o) => o.getAttribute('aria-selected'))).toEqual([
      'true',
      'false',
      'false',
      'false',
    ]);
  });

  it('stands right after the search and before the agent and device filters', () => {
    renderPage();
    const toolbar = document.querySelector('.sessions-toolbar') as HTMLElement;

    expect(toolbar.firstElementChild?.classList.contains('search-field')).toBe(true);
    expect([...toolbar.querySelectorAll('button.pill')].map((b) => b.getAttribute('aria-label'))).toEqual([
      strings.sessions.statusFilter,
      strings.sessions.agentFilter,
      strings.sessions.allDevices,
    ]);
  });

  it('says the chosen word on its button', async () => {
    renderPage();
    expect(trigger()).toHaveTextContent('All states');

    await pickStatus('Waiting');

    expect(trigger()).toHaveTextContent('Waiting');
  });

  it('narrows the list to the green dots under Running, and keeps the choice in the store', async () => {
    renderPage();

    await pickStatus('Running');

    expect(useSessions.getState().statusFilter).toBe('running');
    expect(rowTitles()).toContain('Fix flaky auth test');
    expect(rowTitles()).not.toContain('Migrate web to Vite 6');
    expect(tones()).toEqual(new Set(['working']));
  });

  it('reads Waiting as both ambers, a question and a finished turn alike', async () => {
    renderPage();

    await pickStatus('Waiting');

    expect(rowTitles()).toContain('Migrate web to Vite 6');
    expect(rowTitles()).toContain('iOS push tokens');
    expect(rowTitles()).not.toContain('Fix flaky auth test');
    expect(tones()).toEqual(new Set(['waiting', 'live']));
  });

  it('reads Ended as the grey and the red dots, and opens the Archives that hold them', async () => {
    renderPage();
    expect(screen.queryByText('Rewrite the pairing docs')).not.toBeInTheDocument();

    await pickStatus('Ended');

    expect(archiveTitles()).toContain('Rewrite the pairing docs');
    expect(activeTitles()).toContain('Regenerate the API client');
    expect(tones()).toEqual(new Set(['off', 'failed']));
    // The opening is the filter's, not the reader's: nothing was stored.
    expect(useSettings.getState().archiveExpanded).toEqual([]);
  });

  it('unfolds a device the reader folded, and hands it back under All states', async () => {
    useSettings.setState({ collapsedDevices: ['dev-mac'] });
    renderPage();
    expect(screen.queryByText('Fix flaky auth test')).not.toBeInTheDocument();

    await pickStatus('Running');

    expect(screen.getByText('Fix flaky auth test')).toBeInTheDocument();
    expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);

    await pickStatus(strings.sessions.allStates);

    expect(screen.queryByText('Fix flaky auth test')).not.toBeInTheDocument();
    expect(useSessions.getState().statusFilter).toBe('all');
  });

  it('combines with the agent filter', async () => {
    renderPage();

    await pickStatus('Running');
    await userEvent.click(screen.getByRole('button', { name: strings.sessions.agentFilter }));
    await userEvent.click(screen.getByRole('option', { name: 'Codex' }));

    expect(rowTitles()).toEqual(['Typecheck the web app']);
  });

  it('survives going into a conversation and back, because it lives in the store', async () => {
    const first = renderPage();
    await pickStatus('Running');
    first.unmount();

    renderPage();

    expect(trigger()).toHaveTextContent('Running');
    expect(tones()).toEqual(new Set(['working']));
  });

  it('says so, and how to get the list back, when the status leaves nothing', () => {
    useSessions.setState({ sessions: index([quiet]), statusFilter: 'running' });
    renderPage();

    expect(screen.getByText(strings.sessions.noStatusMatches)).toBeInTheDocument();
    expect(document.querySelector('.empty')?.textContent).toContain(strings.sessions.statusEmptyHint);
    // The menu stays, so the way back is one choice away, and no legend names dots that are not there.
    expect(trigger()).toHaveTextContent('Running');
    expect(document.querySelector('.session-legend')).toBeNull();
  });

  it('answers a search before it answers the status', async () => {
    useSessions.setState({ sessions: index([quiet]), statusFilter: 'running' });
    renderPage();

    await userEvent.type(screen.getByLabelText(strings.sessions.searchPlaceholder), 'zzz');

    expect(screen.getByText(strings.sessions.noMatches)).toBeInTheDocument();
    expect(document.querySelector('.empty')?.textContent).not.toContain(strings.sessions.statusEmptyHint);
  });

  it('does not blame the status alone while an agent filter is on too', () => {
    // The agent matches this session and the status does not: the pair left nothing.
    useSessions.setState({
      sessions: index([quiet]),
      statusFilter: 'running',
      agentFilter: quiet.agent,
    });
    renderPage();

    expect(screen.getByText(strings.sessions.noFilterMatches)).toBeInTheDocument();
    expect(document.querySelector('.empty')?.textContent).toContain(strings.sessions.filterEmptyHint);
    expect(screen.queryByText(strings.sessions.noStatusMatches)).not.toBeInTheDocument();
    expect(document.querySelector('.empty')?.textContent).not.toContain(strings.sessions.statusEmptyHint);
  });

  it('does not blame the status alone while a device filter is on too', async () => {
    useSessions.setState({ statusFilter: 'running' });
    renderPage();

    await userEvent.click(screen.getByRole('button', { name: strings.sessions.allDevices }));
    await userEvent.click(screen.getByRole('option', { name: quietDevice.name }));

    expect(screen.getByText(strings.sessions.noFilterMatches)).toBeInTheDocument();
    expect(screen.queryByText(strings.sessions.noStatusMatches)).not.toBeInTheDocument();
  });

  it('keeps its own words while the status is the only filter, and answers a search first of all', async () => {
    useSessions.setState({ sessions: index([quiet]), statusFilter: 'running' });
    renderPage();

    expect(screen.getByText(strings.sessions.noStatusMatches)).toBeInTheDocument();
    expect(screen.queryByText(strings.sessions.noFilterMatches)).not.toBeInTheDocument();

    useSessions.setState({ agentFilter: quiet.agent });
    await userEvent.type(screen.getByLabelText(strings.sessions.searchPlaceholder), 'zzz');

    expect(screen.getByText(strings.sessions.noMatches)).toBeInTheDocument();
    expect(screen.queryByText(strings.sessions.noFilterMatches)).not.toBeInTheDocument();
  });

  it('says the pair of filters in Chinese too', () => {
    useSettings.setState({ language: 'zh-Hans' });
    useSessions.setState({
      sessions: index([quiet]),
      statusFilter: 'running',
      agentFilter: quiet.agent,
    });
    renderPage();

    expect(screen.getByText('没有符合这些筛选条件的会话。')).toBeInTheDocument();
    expect(document.querySelector('.empty')?.textContent).toContain('清除一个筛选条件即可查看更多会话。');
  });

  it('leaves the plain empty list as it was while no status narrows it', () => {
    useSessions.setState({ sessions: {} });
    renderPage();

    expect(screen.getByText(strings.sessions.empty)).toBeInTheDocument();
    expect(screen.queryByText(strings.sessions.noStatusMatches)).not.toBeInTheDocument();
    expect(trigger()).toHaveTextContent('All states');
  });

  it('says its words in Chinese', async () => {
    useSettings.setState({ language: 'zh-Hans' });
    renderPage();

    expect(screen.getByRole('button', { name: '按状态筛选' })).toHaveTextContent('全部状态');
    await userEvent.click(screen.getByRole('button', { name: '按状态筛选' }));

    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual([
      '全部状态',
      '正在运行',
      '正在等待',
      '已结束',
    ]);
  });
});

/**
 * While a search or a status is showing the groups they are open whatever the
 * user folded, so a click on a header would change nothing on screen and only
 * flip the stored choice behind their back: the headers take none, and the
 * stored folds come back as they were once the list stops revealing.
 */
describe('the group headers while the list is revealing groups', () => {
  const searchField = () => screen.getByLabelText(strings.sessions.searchPlaceholder);

  const pickStatus = async (name: string) => {
    await userEvent.click(screen.getByRole('button', { name: strings.sessions.statusFilter }));
    await userEvent.click(screen.getByRole('option', { name }));
  };

  /** Every device and Archive header on the page. */
  const headers = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.group-head')];

  it('take no click on a device while a search is showing it', async () => {
    renderPage();
    await userEvent.type(searchField(), 'flaky');

    const header = deviceHeader(MAC);
    expect(header).toHaveAttribute('aria-disabled', 'true');
    await userEvent.click(header);

    expect(useSettings.getState().collapsedDevices).toEqual([]);
    expect(header).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Fix flaky auth test')).toBeInTheDocument();
  });

  it('hand the click back once the search is cleared', async () => {
    renderPage();
    await userEvent.type(searchField(), 'flaky');
    await userEvent.click(deviceHeader(MAC));
    await userEvent.clear(searchField());

    expect(deviceHeader(MAC)).not.toHaveAttribute('aria-disabled');
    await userEvent.click(deviceHeader(MAC));

    expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);
    expect(screen.queryByText('Fix flaky auth test')).not.toBeInTheDocument();
  });

  it('take no click on an Archive that a search has opened', async () => {
    renderPage();
    await userEvent.type(searchField(), 'pairing docs');

    const archive = screen.getByRole('button', { name: strings.sessions.archiveGroup(1) });
    expect(archive).toHaveAttribute('aria-disabled', 'true');
    expect(archive).toHaveAttribute('aria-expanded', 'true');
    await userEvent.click(archive);

    expect(useSettings.getState().archiveExpanded).toEqual([]);
    expect(screen.getByText('Rewrite the pairing docs')).toBeInTheDocument();
  });

  it('take no click while a status is showing the groups', async () => {
    renderPage();
    await pickStatus('Ended');

    expect(headers().length).toBeGreaterThan(0);
    for (const header of headers()) {
      expect(header).toHaveAttribute('aria-disabled', 'true');
      await userEvent.click(header);
    }

    expect(useSettings.getState().collapsedDevices).toEqual([]);
    expect(useSettings.getState().archiveExpanded).toEqual([]);
    expect(archiveTitles()).toContain('Rewrite the pairing docs');
  });

  it('leave a folded device folded in the store while a status is unfolding it', async () => {
    useSettings.setState({ collapsedDevices: ['dev-mac'] });
    renderPage();
    await pickStatus('Running');

    await userEvent.click(deviceHeader(MAC));

    expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);
    expect(screen.getByText('Fix flaky auth test')).toBeInTheDocument();

    await pickStatus(strings.sessions.allStates);

    expect(deviceHeader(MAC)).not.toHaveAttribute('aria-disabled');
    expect(screen.queryByText('Fix flaky auth test')).not.toBeInTheDocument();
  });

  it('stay live while nothing but an agent or a device narrows the list', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: strings.sessions.agentFilter }));
    await userEvent.click(screen.getByRole('option', { name: 'Codex' }));

    expect(headers().some((header) => header.hasAttribute('aria-disabled'))).toBe(false);
    await userEvent.click(deviceHeader(MAC));
    expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);
    await userEvent.click(deviceHeader(MAC));

    await userEvent.click(screen.getByRole('button', { name: strings.sessions.allDevices }));
    await userEvent.click(screen.getByRole('option', { name: MAC }));

    expect(headers().some((header) => header.hasAttribute('aria-disabled'))).toBe(false);
    await userEvent.click(deviceHeader(MAC));
    expect(useSettings.getState().collapsedDevices).toEqual(['dev-mac']);
  });

  it('still open and close an Archive when nothing is revealing it', async () => {
    renderPage();
    const archive = screen.getByRole('button', { name: strings.sessions.archiveGroup(3) });

    expect(archive).not.toHaveAttribute('aria-disabled');
    await userEvent.click(archive);

    expect(useSettings.getState().archiveExpanded).toEqual(['dev-mac']);
  });
});

/**
 * `docs/DESIGN.md` § "Close, then the Archive" (A39): the row action is Close,
 * not Archive; it ends the session and the row lands in the Archive. It is
 * offered on exactly one kind of row, it asks first only while the agent is
 * working, and there is no unarchive anywhere.
 */
describe('the close action', () => {
  const realClose = useSessions.getState().close;

  afterEach(() => {
    useSessions.setState({ close: realClose });
    useSettings.setState({ language: 'en' });
  });

  const closeIn = (title: string) =>
    within(rowFor(title)).queryByRole('button', { name: strings.sessions.close });

  const dialog = () => screen.getByRole('dialog');

  const buttonIn = (parent: HTMLElement, name: string) =>
    within(parent).getByRole('button', { name });

  /**
   * The store's own `close` stands in for the device: the reply it applies is
   * the session archived, unowned and stopped, which is what A39 promises.
   */
  const record = (): string[] => {
    const closed: string[] = [];
    useSessions.setState({
      close: async (session) => {
        closed.push(session.session_id);
        useSessions.getState().upsert({
          ...session,
          archived: true,
          control: 'none',
          state: 'stopped',
          state_detail: null,
        });
      },
    });
    return closed;
  };

  it('offers it on a session the device drives', () => {
    renderPage();
    expect(closeIn('iOS push tokens')).toBeInTheDocument();
  });

  it('offers nothing on a row a terminal holds', () => {
    renderPage();
    // `terminal` and `shared`: the terminal owns the row until it exits.
    expect(closeIn('Refactor relay routing')).toBeNull();
    expect(closeIn('Wire the channel shim')).toBeNull();
  });

  it('offers nothing on a row already in the Archive', async () => {
    renderPage();
    await userEvent.click(screen.getByRole('button', { name: strings.sessions.archiveGroup(3) }));

    // Hand-archived, and an exited session the CLI let go of.
    expect(closeIn('Sketch the pairing QR flow')).toBeNull();
    expect(closeIn('Rewrite the pairing docs')).toBeNull();
    expect(screen.queryByRole('button', { name: /unarchive/i })).not.toBeInTheDocument();
  });

  it('closes an idle session on the tap, without asking', async () => {
    const closed = record();
    renderPage();
    const button = closeIn('iOS push tokens');
    if (!button) throw new Error('the action should be offered');

    await userEvent.click(button);

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(closed).toEqual(['ses-push']);
  });

  it('asks before closing a working session, and sends nothing on Cancel', async () => {
    const closed = record();
    renderPage();
    const button = closeIn('Fix flaky auth test');
    if (!button) throw new Error('the action should be offered');

    await userEvent.click(button);

    expect(within(dialog()).getByText(strings.sessions.closeTitle)).toBeInTheDocument();
    expect(within(dialog()).getByText(strings.sessions.closeBody)).toBeInTheDocument();

    await userEvent.click(buttonIn(dialog(), strings.common.cancel));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(closed).toEqual([]);
    expect(activeTitles()).toContain('Fix flaky auth test');
  });

  it('sends one close on the answer, and the row lands in the Archive', async () => {
    const closed = record();
    renderPage();
    const button = closeIn('Fix flaky auth test');
    if (!button) throw new Error('the action should be offered');

    await userEvent.click(button);
    await userEvent.click(buttonIn(dialog(), strings.sessions.close));

    expect(closed).toEqual(['ses-flaky']);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(activeTitles()).not.toContain('Fix flaky auth test');

    // One row more in the device's own Archive, marked and gone quiet.
    await userEvent.click(screen.getByRole('button', { name: strings.sessions.archiveGroup(4) }));
    expect(archiveTitles()).toContain('Fix flaky auth test');
    expect(wordIn('Fix flaky auth test')).toBe('Archived · Remote launch');
    expect(dotTone('Fix flaky auth test')).toBe('off');
    expect(closeIn('Fix flaky auth test')).toBeNull();
  });

  it('says Close on the button', () => {
    renderPage();
    expect(closeIn('iOS push tokens')).toHaveAttribute('title', 'Close');
  });

  it('says 关闭 in Chinese, on the button and in the question', async () => {
    useSettings.setState({ language: 'zh-Hans' });
    renderPage();
    const button = closeIn('Fix flaky auth test');
    if (!button) throw new Error('the action should be offered');
    expect(button).toHaveAttribute('title', '关闭');

    await userEvent.click(button);

    expect(within(dialog()).getByText('关闭此会话？')).toBeInTheDocument();
    expect(within(dialog()).getByText('Agent 仍在工作，未完成的部分会丢失。')).toBeInTheDocument();
  });

  it('leaves the word Archive to the group caption alone', () => {
    renderPage();

    expect(screen.queryByRole('button', { name: 'Archive' })).not.toBeInTheDocument();
    expect(Object.keys(en.sessions)).not.toContain('archive');
    expect(Object.keys(stringTables['zh-Hans'].sessions)).not.toContain('archive');
    expect(screen.getByRole('button', { name: strings.sessions.archiveGroup(3) })).toBeInTheDocument();
  });
});

/**
 * `docs/DESIGN.md` § "Session lists": a thread the agent has not named yet
 * arrives with an empty `title`, and the row still carries a name in the
 * title's own type — including under a search for those words.
 */
describe('a session with no title', () => {
  const untitled: Session = {
    ...sessions[0]!,
    session_id: 'ses-untitled',
    device_id: 'dev-mac',
    title: '',
    cwd: '/Users/me/dev/remote-control/web',
    state: 'idle',
    turn: null,
    todos: null,
    usage: null,
  };

  const withUntitled = () =>
    useSessions.setState({ sessions: index([...sessions, untitled]) });

  it('names the row "Untitled session"', () => {
    withUntitled();
    renderPage();

    expect(activeTitles()).toContain(strings.sessions.untitled);
  });

  it('leaves every titled row as the device reported it', () => {
    withUntitled();
    renderPage();

    expect(activeTitles()).toContain('Fix flaky auth test');
    expect(rowTitles().filter((t) => t === strings.sessions.untitled)).toHaveLength(1);
  });

  it('is found by a search for those words', async () => {
    withUntitled();
    renderPage();

    await userEvent.type(screen.getByLabelText(strings.sessions.searchPlaceholder), 'untitled');

    expect(rowTitles()).toEqual([strings.sessions.untitled]);
  });
});
