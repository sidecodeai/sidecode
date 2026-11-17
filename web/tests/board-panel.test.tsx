/**
 * A62 — the Tasks chip in the conversation header and the panel it opens: the
 * count of what is open, the board grouped Doing, To do and Done recently in
 * plain text, every status the gateway can report drawn as its note, Refresh,
 * Start over after its question, Turn off in Settings, and Enable leading to
 * the disclosure before anything is written. The chip is drawn only on a
 * gateway that keeps boards.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { BoardChip } from '../src/features/chat/board/BoardChip';
import { useBoards } from '../src/stores/board';
import { useConnection } from '../src/stores/connection';
import { usePreferences } from '../src/stores/preferences';
import { useSettings } from '../src/stores/settings';
import { sessionKey } from '../src/stores/sessions';
import { strings } from '../src/strings';
import { Address } from './Address';
import type { Board, BoardItem, BoardStatus } from '../src/protocol/types';

const { rpcMock } = vi.hoisted(() => ({ rpcMock: vi.fn() }));

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => null,
  setSocket: () => undefined,
}));

const DEVICE = 'dev-mac';
const SESSION = 'ses-flaky';
const s = strings.chat.board;

const item = (id: string, status: BoardItem['status'], text: string): BoardItem => ({
  id,
  text,
  status,
  created_at: 1,
  updated_at: 1,
});

const ITEMS: BoardItem[] = [
  item('t3', 'doing', 'Run the refresh test 20 times in a row'),
  item('t4', 'todo', 'Check the settings drawer for the same timing bug'),
  item('t2', 'done', 'Fix the refresh window in tests/test_auth.py'),
  item('t1', 'done', 'Reproduce the flaky auth refresh test'),
];

const FOUR_MINUTES_AGO = () => Date.now() - 4 * 60_000;

const board = (items: BoardItem[] = ITEMS, over: Partial<Board> = {}): Board => ({
  session_id: SESSION,
  device_id: DEVICE,
  revision: 4,
  items,
  updated_at: FOUR_MINUTES_AGO(),
  partial: false,
  ...over,
});

const status = (state: string, reason: string | null = null): BoardStatus => ({ state, reason });

/** The store holds this for the conversation, as if the gateway had said it. */
const holds = (b: Board | null, st: BoardStatus) =>
  useBoards.setState({ boards: { [sessionKey(DEVICE, SESSION)]: { board: b, status: st } } });

const json = (body: unknown): Response =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });

const patches: Record<string, unknown>[] = [];

beforeEach(() => {
  rpcMock.mockReset();
  rpcMock.mockResolvedValue({ status: status('updating') });
  patches.length = 0;
  useSettings.setState({ language: 'en' });
  useBoards.getState().reset();
  useConnection.setState({ board: { enabled: true }, clockSkewMs: 0 });
  usePreferences.setState({ preferences: { resume_after_limit: false, task_board: true } });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
      patches.push(body);
      return json({ preferences: { resume_after_limit: false, ...body } });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  useConnection.setState({ board: null });
  usePreferences.setState({ preferences: undefined });
});

function renderChip() {
  return render(
    <MemoryRouter initialEntries={['/sessions/dev-mac/ses-flaky']}>
      <Routes>
        <Route path="*" element={<BoardChip deviceId={DEVICE} sessionId={SESSION} />} />
      </Routes>
      <Address />
    </MemoryRouter>,
  );
}

const chip = () => screen.getByRole('button', { name: /^Tasks/ });
const panel = () => screen.getByRole('region', { name: s.title });

async function openPanel(user = userEvent.setup()) {
  const view = renderChip();
  await user.click(chip());
  return { user, panel: panel(), view };
}

describe('the chip', () => {
  it('is not drawn on a gateway that keeps no boards, one too old to say, or one that refuses them', () => {
    useConnection.setState({ board: { enabled: false } });
    const view = renderChip();
    expect(screen.queryByRole('button', { name: /^Tasks/ })).toBeNull();
    view.unmount();

    useConnection.setState({ board: null });
    const old = renderChip();
    expect(screen.queryByRole('button', { name: /^Tasks/ })).toBeNull();
    old.unmount();

    useConnection.setState({ board: { enabled: true } });
    holds(null, status('unavailable', 'unsupported'));
    renderChip();
    expect(screen.queryByRole('button', { name: /^Tasks/ })).toBeNull();
  });

  it('counts what is doing and to do, and nothing done', () => {
    holds(board(), status('current'));
    renderChip();

    expect(chip()).toHaveTextContent(s.chipCount(2));
  });

  it('counts nothing while off, unavailable or empty', () => {
    for (const [b, st] of [
      [null, status('off')],
      [board(), status('unavailable', 'quota_exceeded')],
      [board(ITEMS.filter((i) => i.status === 'done')), status('current')],
      [null, status('current')],
    ] as const) {
      holds(b, st);
      const view = renderChip();
      expect(chip().textContent).toBe(s.chip);
      view.unmount();
    }
  });

  it('says 看板 in 中文', () => {
    useSettings.setState({ language: 'zh-Hans' });
    holds(board(), status('current'));
    render(
      <MemoryRouter>
        <BoardChip deviceId={DEVICE} sessionId={SESSION} />
      </MemoryRouter>,
    );

    expect(screen.getByRole('button', { name: '看板 2' })).toBeInTheDocument();
  });
});

describe('the board', () => {
  it('groups the items Doing, To do and Done recently, in the gateway’s order, behind their glyphs', async () => {
    holds(board(), status('current'));
    const { panel } = await openPanel();

    expect(within(panel).getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual([
      s.doing,
      s.todo,
      s.done,
    ]);
    const texts = (name: string) =>
      within(within(panel).getByRole('list', { name })).getAllByRole('listitem').map((li) => li.textContent);
    expect(texts(s.doing)).toEqual(['Run the refresh test 20 times in a row']);
    expect(texts(s.todo)).toEqual(['Check the settings drawer for the same timing bug']);
    expect(texts(s.done)).toEqual([
      'Fix the refresh window in tests/test_auth.py',
      'Reproduce the flaky auth refresh test',
    ]);
    const glyphs = [...panel.querySelectorAll('.board-glyph')].map((glyph) => glyph.className);
    expect(glyphs).toEqual(['board-glyph doing', 'board-glyph todo', 'board-glyph done', 'board-glyph done']);
    expect(panel.querySelectorAll('.board-item.done')).toHaveLength(2);
  });

  it('draws no group that has nothing in it', async () => {
    holds(board(ITEMS.filter((i) => i.status === 'todo')), status('current'));
    const { panel } = await openPanel();

    expect(within(panel).getAllByRole('list')).toHaveLength(1);
    expect(within(panel).queryByText(s.doing)).toBeNull();
    expect(within(panel).queryByText(s.done)).toBeNull();
  });

  it('draws item text as plain text: no Markdown, no links, no markup', async () => {
    const text = '**Ship** <b>it</b> [docs](https://example.com) `npm test` https://example.com';
    holds(board([item('t9', 'todo', text)]), status('current'));
    const { panel } = await openPanel();

    const row = within(panel).getByRole('listitem');
    expect(row.textContent).toBe(text);
    expect(row.querySelector('a, strong, b, code, em')).toBeNull();
  });

  it('says when it was last updated, and that older rounds were left out when they were', async () => {
    holds(board(ITEMS, { partial: true }), status('current'));
    const { panel } = await openPanel();

    expect(within(panel).getByText(s.updated('4m ago'))).toBeInTheDocument();
    expect(within(panel).getByText(s.partial)).toBeInTheDocument();
  });
});

describe('every state of the panel', () => {
  it('off: what the board is for and Enable, with no board and no actions', async () => {
    holds(null, status('off'));
    const { panel } = await openPanel();

    expect(within(panel).getByText(s.offLead)).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: s.enable })).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: s.refresh })).toBeNull();
    expect(within(panel).queryByRole('list')).toBeNull();
  });

  it('empty: no tasks yet, for a current board with none and for one never made', async () => {
    holds(board([]), status('current'));
    const first = await openPanel();
    expect(within(first.panel).getByText(s.empty)).toBeInTheDocument();
    first.view.unmount();

    holds(null, status('current'));
    const { panel } = await openPanel();
    expect(within(panel).getByText(s.empty)).toBeInTheDocument();
  });

  it('updating: the board as it is, with Updating… and Refresh held back', async () => {
    holds(board(), status('updating'));
    const { panel } = await openPanel();

    expect(within(panel).getAllByRole('listitem')).toHaveLength(4);
    expect(within(panel).getByText(s.updating)).toBeInTheDocument();
    expect(panel.querySelector('.spinner')).not.toBeNull();
    expect(within(panel).getByRole('button', { name: s.refresh })).toBeDisabled();
  });

  it('updating a first board: three quiet lines', async () => {
    holds(null, status('updating'));
    const { panel } = await openPanel();

    expect(panel.querySelectorAll('.board-skeleton span')).toHaveLength(3);
    expect(within(panel).getByText(s.updating)).toBeInTheDocument();
  });

  it.each([
    ['device_offline', s.offline],
    ['rate_limited', s.paused],
  ])('stale, %s: the board and its line, with nothing to retry', async (reason, line) => {
    holds(board(), status('stale', reason));
    const { panel } = await openPanel();

    expect(within(panel).getAllByRole('listitem')).toHaveLength(4);
    expect(within(panel).getByText(line)).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: strings.common.retry })).toBeNull();
  });

  it.each(['model_failed', 'invalid_output', 'history_unavailable', 'a_reason_from_later'])(
    'stale, %s: the board, “Couldn’t update just now.” and Retry, which asks for an update',
    async (reason) => {
      holds(board(), status('stale', reason));
      const { user, panel } = await openPanel();

      expect(within(panel).getByText(s.failed)).toBeInTheDocument();
      await user.click(within(panel).getByRole('button', { name: strings.common.retry }));

      expect(rpcMock).toHaveBeenCalledWith('board.refresh', { session_id: SESSION });
    },
  );

  it('unavailable, quota exceeded: the board and the friendly notice, with no actions', async () => {
    holds(board(), status('unavailable', 'quota_exceeded'));
    const { panel } = await openPanel();

    expect(within(panel).getAllByRole('listitem')).toHaveLength(4);
    expect(within(panel).getByText(s.quota)).toBeInTheDocument();
    expect(within(panel).queryByRole('button', { name: s.refresh })).toBeNull();
  });

  it.each(['billing_not_configured', 'a_reason_from_later'])(
    'unavailable, %s: the task board isn’t available right now',
    async (reason) => {
      holds(null, status('unavailable', reason));
      const { panel } = await openPanel();

      expect(within(panel).getByText(s.unavailable)).toBeInTheDocument();
    },
  );

  it('before the gateway has answered: three quiet lines and nothing else', async () => {
    const { panel } = await openPanel();

    expect(panel.querySelectorAll('.board-skeleton span')).toHaveLength(3);
    expect(within(panel).queryByRole('status')).toBeNull();
  });
});

describe('the actions', () => {
  it('asks for an update with Refresh', async () => {
    holds(board(), status('current'));
    const { user, panel } = await openPanel();

    await user.click(within(panel).getByRole('button', { name: s.refresh }));

    expect(rpcMock).toHaveBeenCalledWith('board.refresh', { session_id: SESSION });
  });

  it('starts over only once the question is answered, and not on Cancel', async () => {
    holds(board(), status('current'));
    const { user, panel } = await openPanel();

    await user.click(within(panel).getByRole('button', { name: strings.a11y.openMenu }));
    await user.click(within(panel).getByRole('menuitem', { name: s.startOver }));
    expect(within(panel).getByText(s.startOverAsk)).toBeInTheDocument();
    expect(within(panel).getByRole('button', { name: strings.common.cancel })).toHaveFocus();
    await user.click(within(panel).getByRole('button', { name: strings.common.cancel }));
    expect(rpcMock).not.toHaveBeenCalled();
    expect(within(panel).getAllByRole('listitem')).toHaveLength(4);

    await user.click(within(panel).getByRole('button', { name: strings.a11y.openMenu }));
    await user.click(within(panel).getByRole('menuitem', { name: s.startOver }));
    await user.click(within(panel).getByRole('button', { name: s.startOver }));

    expect(rpcMock).toHaveBeenCalledWith('board.refresh', { session_id: SESSION, rebuild: true });
  });

  it('leads Turn off in Settings to General, and closes', async () => {
    holds(board(), status('current'));
    const { user, panel } = await openPanel();

    await user.click(within(panel).getByRole('button', { name: strings.a11y.openMenu }));
    await user.click(within(panel).getByRole('menuitem', { name: s.turnOff }));

    expect(screen.getByTestId('address')).toHaveTextContent('/settings/general');
    expect(screen.queryByRole('region', { name: s.title })).toBeNull();
  });
});

describe('turning the board on from the panel', () => {
  const c = strings.settings.taskBoardConsent;

  beforeEach(() => {
    usePreferences.setState({ preferences: { resume_after_limit: false } });
    holds(null, status('off'));
  });

  it('shows the disclosure first, and Not now writes nothing', async () => {
    const { user, panel } = await openPanel();
    await user.click(within(panel).getByRole('button', { name: s.enable }));

    const dialog = screen.getByRole('dialog', { name: c.title });
    expect(screen.queryByRole('region', { name: s.title })).toBeNull();
    for (const sentence of [c.lead, c.sends, c.keeps, c.caution]) {
      expect(within(dialog).getByText(sentence)).toBeInTheDocument();
    }
    expect(within(dialog).getAllByRole('listitem')).toHaveLength(3);
    expect(patches).toEqual([]);

    await user.click(within(dialog).getByRole('button', { name: c.notNow }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(patches).toEqual([]);
  });

  it('writes task_board: true on Agree and continue, and the account holds it once the gateway answers', async () => {
    const { user, panel } = await openPanel();
    await user.click(within(panel).getByRole('button', { name: s.enable }));
    await user.click(screen.getByRole('button', { name: c.agree }));

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(patches).toEqual([{ task_board: true }]);
    expect(usePreferences.getState().preferences?.task_board).toBe(true);
  });

  it('keeps the dialog and says so when the gateway refuses', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 500 })));
    const { user, panel } = await openPanel();
    await user.click(within(panel).getByRole('button', { name: s.enable }));
    await user.click(screen.getByRole('button', { name: c.agree }));

    expect(await screen.findByRole('alert')).toHaveTextContent(strings.errors.setFailed);
    expect(screen.getByRole('dialog', { name: c.title })).toBeInTheDocument();
    expect(usePreferences.getState().preferences?.task_board).toBeUndefined();
  });
});
