/**
 * A62 — the tab's copy of each conversation's task board: what `board.get`,
 * `board.refresh` and `board.updated` say, an older board dropped by its
 * revision — which the gateway takes from one counter for every board, so it
 * only grows — a frame with no board clearing the copy, every board off the
 * moment the account turns it off, nothing of an account left after sign-out or
 * a `hello` for another one; and the moments a conversation asks — after its
 * subscribe has answered, never beside it, back in front, after a reconnect
 * subscribed it again, and when the account turns the board on.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useBoardRequests } from '../src/features/chat/board/useBoardRequests';
import { RequestError } from '../src/lib/ws';
import { useBoards } from '../src/stores/board';
import { useChat, type ChatSession } from '../src/stores/chat';
import { useConnection } from '../src/stores/connection';
import { usePreferences } from '../src/stores/preferences';
import { sessionKey } from '../src/stores/sessions';
import { signOut } from '../src/stores/signOut';
import type { BoardUpdatedFrame, HelloFrame, PushFrame, SubscribeResult } from '../src/protocol/frames';
import type { Board, BoardItem, BoardStatus, Preferences, Session } from '../src/protocol/types';
import { sessions as fixtureSessions } from '../mock/fixtures';
import type * as WsModule from '../src/lib/ws';

const { rpcMock, captured, held } = vi.hoisted(() => ({
  rpcMock: vi.fn(),
  captured: { onFrame: null as ((frame: PushFrame) => void) | null },
  /** The app socket the chat store subscribes on, where a test needs one. */
  held: { socket: null as unknown },
}));

vi.mock('../src/lib/gateway', () => ({
  rpc: rpcMock,
  getSocket: () => held.socket,
  setSocket: () => undefined,
}));

// The connection store owns the socket, so frames are fed through the one it opens.
vi.mock('../src/lib/ws', async (importOriginal) => ({
  ...(await importOriginal<typeof WsModule>()),
  socketUrl: () => 'ws://test/ws/app',
  AppSocket: class {
    constructor(options: { onFrame: (frame: PushFrame) => void }) {
      captured.onFrame = options.onFrame;
    }
    start(): void {
      /* nothing to dial in a test */
    }
    stop(): void {
      /* nothing to close */
    }
  },
}));

const DEVICE = 'c5efb1ec-2912-4619-90f7-93b5172fd712';
const SESSION = 'ad2c9abb-4a1e-470a-835c-228778fc17f0';
const KEY = sessionKey(DEVICE, SESSION);

/** The conversation as the subscribe reply carries it. */
const SUMMARY: Session = {
  ...fixtureSessions[0]!,
  session_id: SESSION,
  device_id: DEVICE,
  turn: null,
  usage: null,
  last_seq: 0,
};

const ITEMS: BoardItem[] = [
  { id: 't3', text: 'Run the refresh test 20 times in a row', status: 'doing', created_at: 3, updated_at: 5 },
  { id: 't4', text: 'Check the settings drawer for the same timing bug', status: 'todo', created_at: 5, updated_at: 5 },
  { id: 't2', text: 'Fix the refresh window in tests/test_auth.py', status: 'done', created_at: 1, updated_at: 4 },
];

const board = (revision: number, items: BoardItem[] = ITEMS): Board => ({
  session_id: SESSION,
  device_id: DEVICE,
  revision,
  items,
  updated_at: 1_788_944_481_100,
  partial: false,
});

const CURRENT: BoardStatus = { state: 'current', reason: null };
const UPDATING: BoardStatus = { state: 'updating', reason: null };
const OFF: BoardStatus = { state: 'off', reason: null };

const updated = (b: Board | null, status: BoardStatus = CURRENT): BoardUpdatedFrame => ({
  type: 'board.updated',
  session_id: SESSION,
  device_id: DEVICE,
  board: b,
  status,
});

const entry = () => useBoards.getState().boards[KEY];

const handleFrame = (frame: PushFrame): void => {
  if (!captured.onFrame) throw new Error('the connection store opened no socket');
  captured.onFrame(frame);
};

const hello = (over: Partial<HelloFrame> = {}): HelloFrame => ({
  type: 'hello',
  protocol: 1,
  gateway_version: '1.21.0',
  user: { username: 'dev', role: 'admin' },
  devices: [],
  sessions: [],
  stt: { enabled: false, languages: ['auto'] },
  server_time: Date.now(),
  ...over,
});

/** The calls the store made, by request type. */
const sent = (type: string) => rpcMock.mock.calls.filter(([name]) => name === type).map(([, params]) => params);

beforeEach(() => {
  rpcMock.mockReset();
  useBoards.getState().reset();
  usePreferences.setState({ preferences: undefined });
  useConnection.getState().disconnect();
  useConnection.getState().connect(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the board a conversation asks for', () => {
  it('keeps what board.get answers, the board and its status', async () => {
    rpcMock.mockResolvedValue({ board: board(4), status: UPDATING });

    await useBoards.getState().get(DEVICE, SESSION);

    expect(rpcMock).toHaveBeenCalledWith('board.get', { session_id: SESSION });
    expect(entry()).toEqual({ board: board(4), status: UPDATING });
  });

  it('asks once while a board.get for the same conversation is out', async () => {
    let answer: (value: unknown) => void = () => undefined;
    rpcMock.mockReturnValue(new Promise((resolve) => (answer = resolve)));

    const first = useBoards.getState().get(DEVICE, SESSION);
    void useBoards.getState().get(DEVICE, SESSION);
    answer({ board: null, status: OFF });
    await first;

    expect(sent('board.get')).toHaveLength(1);
    expect(entry()).toEqual({ board: null, status: OFF });
  });

  it('sends Refresh as board.refresh and Start over with rebuild, and keeps the board under the status they answer', async () => {
    rpcMock.mockResolvedValueOnce({ board: board(4), status: CURRENT });
    await useBoards.getState().get(DEVICE, SESSION);
    rpcMock.mockResolvedValue({ status: UPDATING });

    await useBoards.getState().refresh(DEVICE, SESSION);
    await useBoards.getState().refresh(DEVICE, SESSION, true);

    expect(sent('board.refresh')).toEqual([{ session_id: SESSION }, { session_id: SESSION, rebuild: true }]);
    expect(entry()).toEqual({ board: board(4), status: UPDATING });
  });

  it('reads a gateway that keeps no boards as unsupported, which takes the chip away', async () => {
    rpcMock.mockRejectedValue(new RequestError({ code: 'unsupported', message: 'no boards' }));

    await useBoards.getState().get(DEVICE, SESSION);

    expect(entry()?.status).toEqual({ state: 'unavailable', reason: 'unsupported' });
  });

  it('keeps what it holds when a request fails any other way', async () => {
    rpcMock.mockResolvedValueOnce({ board: board(2), status: CURRENT });
    await useBoards.getState().get(DEVICE, SESSION);
    rpcMock.mockRejectedValue(new RequestError({ code: 'timeout', message: '' }));

    await useBoards.getState().get(DEVICE, SESSION);
    await useBoards.getState().refresh(DEVICE, SESSION);

    expect(entry()).toEqual({ board: board(2), status: CURRENT });
  });
});

describe('the revision rule', () => {
  /** A revision from the gateway's one counter: any size, and never consecutive by design. */
  const HELD = 40_812;

  beforeEach(() => {
    useBoards.getState().apply(updated(board(HELD)));
  });

  it('drops a frame whose board has a lower revision, its status with it', () => {
    useBoards.getState().apply(updated(board(HELD - 1, []), UPDATING));

    expect(entry()).toEqual({ board: board(HELD), status: CURRENT });
  });

  it('drops a reply whose board has a lower revision, its status with it', async () => {
    rpcMock.mockResolvedValue({ board: board(2), status: UPDATING });

    await useBoards.getState().get(DEVICE, SESSION);

    expect(entry()).toEqual({ board: board(HELD), status: CURRENT });
  });

  it('takes the same revision with a new status, and a higher one however far it jumps', () => {
    useBoards.getState().apply(updated(board(HELD), { state: 'stale', reason: 'device_offline', retry_at: null }));
    expect(entry()?.status.reason).toBe('device_offline');

    useBoards.getState().apply(updated(board(9_007_199_254_740_000, [])));
    expect(entry()).toEqual({ board: board(9_007_199_254_740_000, []), status: CURRENT });
  });

  it('clears the board on a frame with none, and takes its status', () => {
    useBoards.getState().apply(updated(null, UPDATING));

    expect(entry()).toEqual({ board: null, status: UPDATING });
  });

  it('takes the next board after one with none, whatever revision it carries', () => {
    useBoards.getState().apply(updated(null, OFF));
    useBoards.getState().apply(updated(board(7)));

    expect(entry()).toEqual({ board: board(7), status: CURRENT });
  });

  it('reads a state it does not know as stale, keeping its reason', () => {
    useBoards.getState().apply(updated(board(HELD), { state: 'paused', reason: 'maintenance' }));

    expect(entry()?.status).toEqual({ state: 'stale', reason: 'maintenance' });
  });
});

describe('the frames', () => {
  it('reads whether the gateway keeps boards from hello, and none from a gateway that does not say', () => {
    handleFrame(hello({ board: { enabled: true } }));
    expect(useConnection.getState().board).toEqual({ enabled: true });

    handleFrame(hello());
    expect(useConnection.getState().board).toBeNull();
  });

  it('applies board.updated from the socket', () => {
    handleFrame(updated(board(7)));

    expect(entry()).toEqual({ board: board(7), status: CURRENT });
  });

  it('turns every board off when the account’s preference goes off, and leaves them when it is on', () => {
    const on: Preferences = { resume_after_limit: false, task_board: true };
    handleFrame(hello({ preferences: on, board: { enabled: true } }));
    handleFrame(updated(board(7)));
    handleFrame({ type: 'preferences.updated', preferences: { ...on, language: 'en' } });
    expect(entry()?.board?.revision).toBe(7);

    handleFrame({ type: 'preferences.updated', preferences: { resume_after_limit: false, task_board: false } });

    expect(entry()).toEqual({ board: null, status: OFF });
  });

  it('reads a preference that is absent as off', () => {
    useBoards.getState().apply(updated(board(2)));

    handleFrame(hello({ preferences: { resume_after_limit: false }, board: { enabled: true } }));

    expect(entry()).toEqual({ board: null, status: OFF });
  });
});

describe('another account', () => {
  it('finds nothing after sign-out, not even a reply that was on its way', async () => {
    let answer: (value: unknown) => void = () => undefined;
    rpcMock.mockReturnValue(new Promise((resolve) => (answer = resolve)));
    handleFrame(updated(board(3)));
    const late = useBoards.getState().get(DEVICE, SESSION);

    signOut();
    answer({ board: board(9), status: CURRENT });
    await late;

    expect(useBoards.getState().boards).toEqual({});
  });

  it('empties the store on a hello for another account, and keeps it for the same one', () => {
    const on: Preferences = { resume_after_limit: false, task_board: true };
    handleFrame(hello({ preferences: on }));
    handleFrame(updated(board(3)));

    handleFrame(hello({ preferences: on }));
    expect(entry()?.board?.revision).toBe(3);

    handleFrame(hello({ preferences: on, user: { username: 'alice', role: 'member' } }));
    expect(useBoards.getState().boards).toEqual({});
  });
});

describe('when a conversation asks', () => {
  /** The chat store's copy of the conversation, with only how its subscribe stands. */
  const subscription = (state: ChatSession['subscription']) =>
    useChat.setState({ sessions: { [KEY]: { subscription: state } as ChatSession } });

  const preference = (on: boolean) =>
    usePreferences.setState({ preferences: { resume_after_limit: false, task_board: on } });

  const render = (inFront = true) =>
    renderHook(({ front }) => useBoardRequests(DEVICE, SESSION, front), { initialProps: { front: inFront } });

  beforeEach(() => {
    rpcMock.mockResolvedValue({ board: null, status: OFF });
    useConnection.setState({ board: { enabled: true } });
    preference(false);
    subscription('pending');
  });

  afterEach(() => {
    useChat.setState({ sessions: {} });
    held.socket = null;
  });

  it('sends board.get after the session.subscribe reply, never beside the subscribe', async () => {
    const answers: ((result: SubscribeResult) => void)[] = [];
    held.socket = {
      subscribe: (_session: string, _since: number | undefined, handlers: { onResult: (r: SubscribeResult) => void }) =>
        answers.push(handlers.onResult),
      unsubscribe: () => undefined,
      updateCursor: () => undefined,
      stop: () => undefined,
    };
    rpcMock.mockImplementation(async (type: string) =>
      type === 'session.history' ? { events: [], has_more: false } : { board: null, status: OFF },
    );
    useChat.setState({ sessions: {} });
    render();

    // The real chat store opens it: the subscribe goes out with the newest page beside it.
    await act(async () => useChat.getState().open(DEVICE, SESSION, { fresh: true }));
    expect(answers).toHaveLength(1);
    expect(sent('session.history')).toHaveLength(1);
    expect(sent('board.get')).toEqual([]);

    await act(async () => answers[0]?.({ session: SUMMARY, events: [], resync: false }));

    expect(sent('board.get')).toEqual([{ session_id: SESSION }]);
  });

  it('asks once the subscribe has answered, and not before', async () => {
    render();
    expect(sent('board.get')).toEqual([]);

    await act(async () => subscription('live'));

    expect(sent('board.get')).toEqual([{ session_id: SESSION }]);
  });

  it('asks again when the page comes back to the front, and not when it leaves it', async () => {
    subscription('live');
    const view = render();
    await act(async () => undefined);

    view.rerender({ front: false });
    await act(async () => undefined);
    view.rerender({ front: true });
    await act(async () => undefined);

    expect(sent('board.get')).toHaveLength(2);
  });

  it('asks again after a reconnect subscribed the conversation again', async () => {
    subscription('live');
    render();
    await act(async () => undefined);

    await act(async () => subscription('pending'));
    await act(async () => subscription('live'));

    expect(sent('board.get')).toHaveLength(2);
  });

  it('asks for an update when the account turns the board on, here or in another app', async () => {
    subscription('live');
    render();
    await act(async () => undefined);

    await act(async () => preference(true));

    expect(sent('board.refresh')).toEqual([{ session_id: SESSION }]);
  });

  it('asks nothing of a gateway that keeps no boards, or one too old to say', async () => {
    useConnection.setState({ board: { enabled: false } });
    subscription('live');
    const view = render();
    await act(async () => preference(true));
    useConnection.setState({ board: null });
    view.rerender({ front: false });
    view.rerender({ front: true });
    await act(async () => undefined);

    expect(rpcMock).not.toHaveBeenCalled();
  });
});
