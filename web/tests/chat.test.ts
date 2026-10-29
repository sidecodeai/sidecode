import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { HistoryResult, SubscribeResult } from '../src/protocol/frames';
import type { Session, SessionEvent, Trigger } from '../src/protocol/types';
import { strings } from '../src/strings';
import { emptyTimeline, hasGap, mergeHistory } from '../src/stores/timeline';

const rpc = vi.fn();
const subscribe = vi.fn();
const unsubscribe = vi.fn();
const updateCursor = vi.fn();

vi.mock('../src/lib/gateway', () => ({
  rpc: (...args: unknown[]) => rpc(...args),
  getSocket: () => ({ subscribe, unsubscribe, updateCursor }),
  setSocket: vi.fn(),
}));

const { useChat } = await import('../src/stores/chat');
const { useSessions } = await import('../src/stores/sessions');
const { useOutbox } = await import('../src/stores/outbox');
const { RequestError } = await import('../src/lib/ws');
const { dotTone } = await import('../src/components/dotTone');

const DEVICE = 'dev-1';
const SESSION = 'ses-1';
const KEY = `${DEVICE}/${SESSION}`;

const session: Session = {
  session_id: SESSION,
  device_id: DEVICE,
  agent: 'claude',
  title: 'Fix flaky auth test',
  cwd: '/Users/me/dev/gateway',
  git: null,
  state: 'running',
  state_detail: null,
  origin: 'remote',
  control: 'remote',
  model: 'claude-sonnet-4-5',
  permission_mode: 'default',
  effort: 'high',
  created_at: 1,
  updated_at: 2,
  last_seq: 0,
  archived: false,
  turn: null,
  todos: { total: 4, done: 1 },
  usage: null,
  queued: 2,
};

const todosEvent = (seq: number, done: number): SessionEvent =>
  ({
    seq,
    ts: 1_000 + seq,
    kind: 'todos',
    items: [
      { id: 't1', text: 'Reproduce', status: done > 0 ? 'completed' : 'pending' },
      { id: 't2', text: 'Isolate the clock', status: done > 1 ? 'completed' : 'in_progress' },
      { id: 't3', text: 'Re-run the suite', status: 'pending' },
    ],
  }) as SessionEvent;

const queueEvent = (seq: number, ids: string[]): SessionEvent =>
  ({
    seq,
    ts: 1_000 + seq,
    kind: 'queue',
    pending: ids.map((id) => ({ id, text: `queued ${id}`, ts: 5 })),
  }) as SessionEvent;

const userMessage = (seq: number, blockId: string, text: string): SessionEvent =>
  ({ seq, ts: 1_000 + seq, kind: 'user_message', block_id: blockId, text, source: 'remote' }) as SessionEvent;

/** A30: a message the CLI filed as a user turn that nobody typed. */
const agentMessage = (seq: number, blockId: string, text: string): SessionEvent => ({
  seq,
  ts: 1_000 + seq,
  kind: 'user_message',
  block_id: blockId,
  text,
  source: 'agent',
});

/** Written without a cast, so the compiler checks the trigger against A30. */
const turnStarted = (seq: number, trigger: Trigger, turnId: string): SessionEvent => ({
  seq,
  ts: 1_000 + seq,
  kind: 'turn_started',
  turn_id: turnId,
  trigger,
});

const turnCompleted = (seq: number, turnId: string): SessionEvent => ({
  seq,
  ts: 1_000 + seq,
  kind: 'turn_completed',
  turn_id: turnId,
  stop_reason: 'completed',
  duration_ms: 1_200,
});

const statusEvent = (seq: number, state: Session['state']): SessionEvent => ({
  seq,
  ts: 1_000 + seq,
  kind: 'status',
  state,
});

const textEvent = (seq: number, text: string): SessionEvent =>
  ({ seq, ts: 1_000 + seq, kind: 'assistant_text', block_id: `b${seq}`, text, done: true }) as SessionEvent;

/** One tool call block, as it is at the moment it was last written. */
const toolEvent = (seq: number, status: 'running' | 'succeeded'): SessionEvent =>
  ({
    seq,
    ts: 1_000 + seq,
    kind: 'tool_call',
    block_id: 't1',
    tool: 'Bash',
    tool_kind: 'shell',
    title: 'pytest',
    status,
    started_at: 1_000,
  }) as SessionEvent;

type Handlers = {
  onResult: (result: SubscribeResult) => void;
  onError: (err: unknown) => void;
};

/**
 * Runs `open()` for a conversation nobody has read yet and hands back the
 * subscribe handlers the store registered.
 */
function openFresh(): Handlers {
  useChat.getState().open(DEVICE, SESSION);
  const call = subscribe.mock.calls.at(-1);
  if (!call) throw new Error('open() did not subscribe');
  return call[2] as Handlers;
}

/**
 * The same for a conversation whose newest page was read, which is what a reply
 * that carries events or a cursor is a reconnect of: its transcript is what the
 * device sent, and nothing more is owed from it.
 */
function openSession(): Handlers {
  const handlers = openFresh();
  useChat.setState((s) => ({
    sessions: { ...s.sessions, [KEY]: { ...s.sessions[KEY]!, historyLoaded: true } },
  }));
  return handlers;
}

const chat = () => useChat.getState().sessions[KEY];
const summary = () => useSessions.getState().sessions[KEY];

beforeEach(() => {
  rpc.mockReset();
  subscribe.mockReset();
  unsubscribe.mockReset();
  updateCursor.mockReset();
  useChat.setState({ sessions: {} });
  useSessions.setState({ sessions: { [KEY]: { ...session } }, loaded: true });
  useOutbox.setState({ pending: {} });
  rpc.mockResolvedValue({ events: [], has_more: false });
});

describe('subscribe replay', () => {
  it('ignores a subscribe reply from the previous account after reset', () => {
    const previous = openSession();
    useChat.getState().reset();
    const current: Session = { ...session, title: 'New account session', last_seq: 100 };
    openSession().onResult({ session: current, resync: false, events: [textEvent(100, 'new')] });

    previous.onResult({ session, resync: false, events: [textEvent(1, 'old account')] });

    expect(summary()).toBe(current);
    expect(chat()?.timeline.order).toEqual(['b100']);
  });

  it('applies todos and queue snapshots carried by buffered events', () => {
    const handlers = openSession();
    handlers.onResult({
      session,
      resync: false,
      events: [todosEvent(10, 1), queueEvent(11, ['q1', 'q2']), textEvent(12, 'hello')],
    });

    expect(chat()?.todos).toHaveLength(3);
    expect(chat()?.todos[0]?.status).toBe('completed');
    expect(chat()?.queue.map((q) => q.id)).toEqual(['q1', 'q2']);
    // The timeline still drops the state-only kinds.
    expect(chat()?.timeline.order).toEqual(['b12']);
    expect(chat()?.ready).toBe(true);
  });

  it('keeps the subscribe summary authoritative over replayed snapshots', () => {
    const handlers = openSession();
    const current: Session = {
      ...session,
      state: 'idle',
      todos: { total: 3, done: 3 },
      queued: 0,
      last_seq: 20,
    };
    handlers.onResult({
      session: current,
      resync: false,
      events: [
        todosEvent(10, 2),
        queueEvent(11, ['q1']),
        { seq: 12, ts: 12, kind: 'status', state: 'needs_approval', detail: 'waiting' } as SessionEvent,
      ],
    });

    expect(summary()).toBe(current);
    expect(chat()?.todos).toHaveLength(3);
    expect(chat()?.queue.map((item) => item.id)).toEqual(['q1']);
  });

  it('keeps a completed Codex session amber after reconnect replay', () => {
    const initial: Session = { ...session, agent: 'codex', control: 'shared', last_seq: 1 };
    openSession().onResult({ session: initial, resync: false, events: [textEvent(1, 'earlier')] });
    useChat.getState().close(DEVICE, SESSION);
    const handlers = openSession();
    expect(subscribe).toHaveBeenLastCalledWith(SESSION, 1, expect.any(Object));
    const current: Session = { ...initial, state: 'idle', model: 'codex-current', last_seq: 6 };
    handlers.onResult({
      session: current,
      resync: false,
      // The gateway retained running before the device dropped. Backfill does
      // not restore the missed idle status because history never stores it.
      events: [
        statusEvent(2, 'running'),
        { seq: 3, ts: 1_003, kind: 'meta', model: 'codex-previous' },
        textEvent(4, 'finished'),
        turnCompleted(5, 'previous-turn'),
      ],
    });

    expect(summary()).toBe(current);
    expect(dotTone(current.state, current.control, true)).toBe('live');
    expect(chat()?.timeline.order).toEqual(['b1', 'b4']);
  });

  it('keeps the current running turn when replay ends an earlier turn', () => {
    const handlers = openSession();
    const current: Session = {
      ...session,
      last_seq: 30,
      turn: { turn_id: 'current-turn', started_at: 1_030 },
    };
    handlers.onResult({
      session: current,
      resync: false,
      events: [turnStarted(10, 'terminal', 'previous-turn'), turnCompleted(20, 'previous-turn')],
    });

    expect(summary()).toBe(current);
    expect(summary()?.turn?.turn_id).toBe('current-turn');
  });

  it('prefers the explicit A6 queue snapshot over replayed queue events', () => {
    const handlers = openSession();
    handlers.onResult({
      session,
      resync: false,
      events: [queueEvent(10, ['stale'])],
      queue: { pending: [{ id: 'fresh', text: 'from the gateway', ts: 9 }] },
    });

    expect(chat()?.queue.map((q) => q.id)).toEqual(['fresh']);
  });

  it('accepts a subscribe reply without the optional queue field', () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [queueEvent(10, ['q1'])] });
    expect(chat()?.queue.map((q) => q.id)).toEqual(['q1']);
  });

  // The gateway's buffers are empty after every restart, so every session that
  // advanced meanwhile answers a resubscribe with `resync`. The transcript used
  // to be cleared until the history reply came back, which took the reader's
  // place, the older pages they had loaded and the todos with it.
  describe('a resync', () => {
    it('keeps the transcript while the device is asked for what follows the cursor', async () => {
      const handlers = openSession();
      handlers.onResult({ session, resync: false, events: [todosEvent(4, 1), textEvent(5, 'old')] });
      expect(chat()?.timeline.order).toEqual(['b5']);

      let resolveHistory!: (result: HistoryResult) => void;
      rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolveHistory = resolve; }));
      handlers.onResult({ session: { ...session, last_seq: 20 }, resync: true, events: [] });

      // Nothing goes blank while the answer is on its way.
      expect(chat()?.timeline.order).toEqual(['b5']);
      expect(chat()?.todos).toHaveLength(3);
      expect(rpc).toHaveBeenCalledWith('session.history', {
        session_id: SESSION,
        after_seq: 5,
        limit: 200,
      });

      resolveHistory({ events: [textEvent(20, 'replayed')], has_more: false });
      await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b5', 'b20']));
      expect(chat()?.timeline.contiguousSeq).toBe(20);
      expect(chat()?.error).toBeNull();
    });

    it('keeps the older pages the reader loaded, and where the transcript begins', async () => {
      const handlers = openFresh();
      rpc.mockResolvedValueOnce({ events: [textEvent(40, 'newest')], has_more: true });
      handlers.onResult({ session, resync: false, events: [] });
      await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b40']));
      rpc.mockResolvedValueOnce({ events: [textEvent(10, 'older')], has_more: false });
      await useChat.getState().loadOlder(KEY);
      expect(chat()?.timeline.order).toEqual(['b10', 'b40']);

      rpc.mockResolvedValueOnce({ events: [textEvent(50, 'after')], has_more: false });
      handlers.onResult({ session: { ...session, last_seq: 50 }, resync: true, events: [] });
      await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b10', 'b40', 'b50']));
      expect(chat()?.historyHasMore).toBe(false);
      expect(chat()?.timeline.oldestSeq).toBe(10);
    });

    it('keeps a message that was sent and not yet confirmed', async () => {
      const handlers = openSession();
      handlers.onResult({ session, resync: false, events: [textEvent(5, 'old')] });
      rpc.mockReturnValueOnce(new Promise(() => undefined));
      void useChat.getState().send(KEY, { text: 'still on its way', mode: 'auto' });
      expect(chat()?.timeline.optimistic).toHaveLength(1);

      handlers.onResult({ session: { ...session, last_seq: 20 }, resync: true, events: [] });

      expect(chat()?.timeline.optimistic.map((block) => block.text)).toEqual(['still on its way']);
      expect(chat()?.timeline.order).toEqual(['b5']);
    });

    it('leaves the transcript as it was, and says so, when the device cannot be asked', async () => {
      const handlers = openSession();
      handlers.onResult({ session, resync: false, events: [textEvent(5, 'old')] });
      rpc.mockRejectedValueOnce(new RequestError({ code: 'device_offline', message: '' }));

      handlers.onResult({ session: { ...session, last_seq: 20 }, resync: true, events: [] });

      await vi.waitFor(() => expect(chat()?.error).toBe(strings.errors.deviceOffline));
      expect(chat()?.timeline.order).toEqual(['b5']);
    });

    it('asks for the newest page when there is nothing to keep', async () => {
      const handlers = openFresh();
      handlers.onResult({ session, resync: false, events: [] });
      await vi.waitFor(() => expect(chat()?.historyHasMore).toBe(false));

      rpc.mockResolvedValueOnce({ events: [textEvent(20, 'arrived while away')], has_more: false });
      handlers.onResult({ session: { ...session, last_seq: 20 }, resync: true, events: [] });

      await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b20']));
      // Nothing to join to, so no `after_seq`: the newest page it is.
      expect(rpc.mock.calls.at(-1)).toEqual(['session.history', { session_id: SESSION, limit: 200 }]);
    });
  });

  it('records a subscribe failure without wedging the view', () => {
    const handlers = openSession();
    handlers.onError(new Error('device_offline'));
    expect(chat()?.ready).toBe(true);
    expect(chat()?.error).toBe('device_offline');
  });
});

describe('history paging', () => {
  it('drops history from a signed-out account after the same key is opened again', async () => {
    let resolveHistory!: (result: HistoryResult) => void;
    rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolveHistory = resolve; }));
    openSession().onResult({ session, resync: false, events: [textEvent(10, 'old')] });
    const history = useChat.getState().loadOlder(KEY);
    expect(chat()?.historyLoading).toBe(true);

    useChat.getState().reset();
    const current: Session = { ...session, title: 'New account session', last_seq: 100 };
    openSession().onResult({ session: current, resync: false, events: [textEvent(100, 'new')] });
    const next = chat();
    resolveHistory({ events: [textEvent(1, 'old account')], has_more: false });

    await history;
    expect(chat()).toBe(next);
    expect(chat()?.timeline.order).toEqual(['b100']);
    expect(summary()).toBe(current);
  });

  it('applies the todos snapshot from the newest page', async () => {
    const handlers = openFresh();
    rpc.mockResolvedValueOnce({
      events: [todosEvent(3, 1), textEvent(4, 'first answer')],
      has_more: true,
    });
    handlers.onResult({ session, resync: false, events: [] });

    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b4']));
    expect(chat()?.todos).toHaveLength(3);
    expect(summary()?.todos).toEqual(session.todos);
  });

  it('never clears the current turn while loading completed turns from history', async () => {
    const handlers = openSession();
    const current: Session = {
      ...session,
      last_seq: 30,
      turn: { turn_id: 'current-turn', started_at: 1_030 },
    };
    rpc.mockResolvedValueOnce({ events: [turnCompleted(20, 'previous-turn')], has_more: false });
    handlers.onResult({ session: current, resync: false, events: [] });

    await vi.waitFor(() => expect(chat()?.historyLoading).toBe(false));
    expect(summary()).toBe(current);
  });

  it('keeps live snapshots when an earlier history request returns later', async () => {
    let resolveHistory!: (result: HistoryResult) => void;
    rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolveHistory = resolve; }));
    const handlers = openFresh();
    handlers.onResult({ session, resync: false, events: [] });
    expect(chat()?.historyLoading).toBe(true);
    useChat.getState().ingestEvent(SESSION, turnStarted(30, 'terminal', 'current-turn'), DEVICE);
    useChat.getState().ingestEvent(SESSION, todosEvent(31, 2), DEVICE);
    useChat.getState().ingestEvent(SESSION, { seq: 32, ts: 1_032, kind: 'meta', model: 'current-model' }, DEVICE);
    const current = summary();
    resolveHistory({ events: [todosEvent(10, 0), turnCompleted(20, 'previous-turn')], has_more: false });

    await vi.waitFor(() => expect(chat()?.historyLoading).toBe(false));
    expect(summary()).toBe(current);
    expect(summary()?.turn?.turn_id).toBe('current-turn');
    expect(summary()?.model).toBe('current-model');
    expect(chat()?.todos[1]?.status).toBe('completed');
  });

  it('never lets an older page roll the todos snapshot back', async () => {
    const handlers = openFresh();
    rpc.mockResolvedValueOnce({ events: [todosEvent(30, 2), textEvent(31, 'newest')], has_more: true });
    handlers.onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.todos[1]?.status).toBe('completed'));

    rpc.mockResolvedValueOnce({ events: [todosEvent(2, 0), textEvent(3, 'oldest')], has_more: false });
    await useChat.getState().loadOlder(KEY);

    expect(chat()?.timeline.order).toEqual(['b3', 'b31']);
    // Still the newer snapshot: two done, not zero.
    expect(chat()?.todos[0]?.status).toBe('completed');
    expect(chat()?.todos[1]?.status).toBe('completed');
    expect(chat()?.historyHasMore).toBe(false);
  });

  it('sends before_seq only once an older page is requested', async () => {
    const handlers = openFresh();
    rpc.mockResolvedValueOnce({ events: [textEvent(40, 'newest')], has_more: true });
    handlers.onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b40']));

    const firstCall = rpc.mock.calls.find((c) => c[0] === 'session.history');
    expect(firstCall?.[1]).not.toHaveProperty('before_seq');

    rpc.mockResolvedValueOnce({ events: [], has_more: false });
    await useChat.getState().loadOlder(KEY);
    const secondCall = rpc.mock.calls.filter((c) => c[0] === 'session.history').at(-1);
    expect(secondCall?.[1]).toMatchObject({ before_seq: 40 });
  });
});

describe('account scope', () => {
  it('drops an expanded block from the previous account after the same key is reopened', async () => {
    let resolveBlock!: (result: { event: SessionEvent }) => void;
    rpc.mockReturnValueOnce(new Promise<{ event: SessionEvent }>((resolve) => { resolveBlock = resolve; }));
    openSession().onResult({ session, resync: false, events: [textEvent(100, 'old')] });
    const expansion = useChat.getState().expandBlock(KEY, 'b100');

    useChat.getState().reset();
    openSession().onResult({ session, resync: false, events: [textEvent(100, 'new')] });
    resolveBlock({ event: textEvent(100, 'old account detail') });
    await expansion;

    expect(chat()?.timeline.items.b100?.event).toMatchObject({ text: 'new' });
  });
});

describe('live events', () => {
  it('still projects live status and metadata onto the session summary', () => {
    const handlers = openSession();
    handlers.onResult({ session: { ...session, state: 'idle' }, resync: false, events: [] });
    useChat.getState().ingestEvent(SESSION, statusEvent(10, 'running'), DEVICE);
    expect(summary()?.state).toBe('running');
    useChat.getState().ingestEvent(SESSION, { seq: 11, ts: 1_011, kind: 'meta', model: 'current-model' }, DEVICE);
    expect(summary()?.model).toBe('current-model');
    useChat.getState().ingestEvent(SESSION, statusEvent(12, 'idle'), DEVICE);
    expect(summary()?.state).toBe('idle');
  });

  it('folds todos and queue and advances the socket cursor', () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });

    useChat.getState().ingestEvent(SESSION, todosEvent(7, 1), DEVICE);
    useChat.getState().ingestEvent(SESSION, queueEvent(8, ['q9']), DEVICE);

    expect(chat()?.todos).toHaveLength(3);
    expect(chat()?.queue.map((q) => q.id)).toEqual(['q9']);
    expect(updateCursor).toHaveBeenLastCalledWith(SESSION, 8);
  });

  it('routes by session_id whether or not device_id is stamped (A5)', () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });

    useChat.getState().ingestEvent(SESSION, textEvent(2, 'without device'));
    expect(chat()?.timeline.order).toEqual(['b2']);

    useChat.getState().ingestEvent(SESSION, textEvent(3, 'with device'), DEVICE);
    expect(chat()?.timeline.order).toEqual(['b2', 'b3']);

    // A device_id that does not match still resolves through session_id.
    useChat.getState().ingestEvent(SESSION, textEvent(4, 'other device'), 'dev-other');
    expect(chat()?.timeline.order).toEqual(['b2', 'b3', 'b4']);
  });

  it('drops an event whose seq was already applied', () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [textEvent(9, 'kept')] });
    const before = chat();
    useChat.getState().ingestEvent(SESSION, textEvent(9, 'duplicate'));
    expect(chat()).toBe(before);
    expect(updateCursor).not.toHaveBeenCalled();
  });

  // The gateway fans out what a device reconnect backfilled after live events
  // were already flowing, so an event can arrive behind newer ones. It used to
  // be dropped as late, which left the hole for good.
  it('merges an event that arrives behind newer ones instead of dropping it', () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [textEvent(9, 'kept')] });
    useChat.getState().ingestEvent(SESSION, textEvent(4, 'behind'));

    expect(chat()?.timeline.order).toEqual(['b4', 'b9']);
    // It moves nothing: not the cursor the socket resumes from, not the summary.
    expect(chat()?.timeline.lastSeq).toBe(9);
    expect(updateCursor).not.toHaveBeenCalled();
  });

  it('ignores events for a session that is not open', () => {
    useChat.getState().ingestEvent('other-session', textEvent(1, 'nope'));
    expect(useChat.getState().sessions).toEqual({});
  });

  // A30: a turn another agent's message started is a turn like any other — the
  // status line reads it as it reads a terminal-started one.
  it('runs a turn another agent started exactly as a terminal-started one', () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });

    useChat.getState().ingestEvent(SESSION, turnStarted(20, 'agent', 'turn-agent'), DEVICE);
    expect(summary()?.turn).toEqual({ turn_id: 'turn-agent', started_at: 1_020 });
    // And the bubble that started it is a block like any other.
    useChat.getState().ingestEvent(SESSION, agentMessage(21, 'ba', 'recon-ios: done.'), DEVICE);
    expect(chat()?.timeline.order).toEqual(['ba']);

    useChat.getState().ingestEvent(SESSION, turnCompleted(22, 'turn-agent'), DEVICE);
    expect(summary()?.turn).toBeNull();

    useChat.getState().ingestEvent(SESSION, turnStarted(23, 'terminal', 'turn-terminal'), DEVICE);
    expect(summary()?.turn).toEqual({ turn_id: 'turn-terminal', started_at: 1_023 });
  });
});

describe('gap repair', () => {
  const historyCalls = () => rpc.mock.calls.filter((c) => c[0] === 'session.history').map((c) => c[1]);

  /** An open conversation that has applied events 1 and 2. */
  function openAtTwo() {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [textEvent(1, 'a'), textEvent(2, 'b')] });
    return handlers;
  }

  it('asks the device for what lies after the last contiguous event and merges it by block', async () => {
    openAtTwo();
    rpc.mockResolvedValueOnce({ events: [textEvent(3, 'c'), textEvent(4, 'd')], has_more: false });

    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);

    expect(historyCalls()).toEqual([{ session_id: SESSION, after_seq: 2, limit: 200 }]);
    await vi.waitFor(() =>
      expect(chat()?.timeline.order).toEqual(['b1', 'b2', 'b3', 'b4', 'b5']),
    );
    expect(hasGap(chat()!.timeline)).toBe(false);
    expect(chat()?.timeline.contiguousSeq).toBe(5);
    expect(chat()?.error).toBeNull();
    expect(historyCalls()).toHaveLength(1);
  });

  it('replaces the stale copy of a block with the page’s newer one, in the place it began', async () => {
    const handlers = openSession();
    handlers.onResult({
      session,
      resync: false,
      events: [textEvent(1, 'a'), toolEvent(2, 'running')],
    });
    rpc.mockResolvedValueOnce({ events: [toolEvent(4, 'succeeded')], has_more: false });

    useChat.getState().ingestEvent(SESSION, textEvent(6, 'f'), DEVICE);

    await vi.waitFor(() => expect(chat()?.timeline.items.t1?.event).toMatchObject({ status: 'succeeded' }));
    expect(chat()?.timeline.order).toEqual(['b1', 't1', 'b6']);
  });

  // A conversation with no rows is not known to be empty while the device is being
  // asked what it missed, and the page needs to know that to keep from saying so.
  it('says a repair is under way from the moment it asks until it has been answered', async () => {
    openAtTwo();
    let resolvePage!: (result: HistoryResult) => void;
    rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolvePage = resolve; }));
    expect(chat()?.repairing).toBe(false);

    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);
    expect(chat()?.repairing).toBe(true);

    resolvePage({ events: [textEvent(3, 'c'), textEvent(4, 'd')], has_more: false });
    await vi.waitFor(() => expect(chat()?.repairing).toBe(false));
    expect(chat()?.timeline.order).toHaveLength(5);
  });

  it('says so no longer once a repair has failed', async () => {
    openAtTwo();
    rpc.mockRejectedValueOnce(new RequestError({ code: 'device_offline', message: '' }));

    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);
    await vi.waitFor(() => expect(chat()?.error).toBe(strings.errors.deviceOffline));

    expect(chat()?.repairing).toBe(false);
  });

  it('does not take the first event of a conversation for a hole', async () => {
    openSession().onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.historyLoading).toBe(false));
    rpc.mockClear();

    useChat.getState().ingestEvent(SESSION, textEvent(40, 'first'), DEVICE);

    expect(rpc).not.toHaveBeenCalled();
    expect(hasGap(chat()!.timeline)).toBe(false);
  });

  it('trusts a subscribe reply: a jump inside it is not a hole', () => {
    // After a device reconnect the gateway answers from the device's history,
    // which keeps only the latest event of each block, so the numbers jump.
    openSession().onResult({
      session,
      resync: false,
      events: [textEvent(10, 'a'), textEvent(14, 'b')],
    });

    expect(historyCalls()).toEqual([]);
    expect(chat()?.timeline.contiguousSeq).toBe(14);
  });

  // The device keeps the latest event of each block and no `status` at all, so
  // the last events of an idle conversation are ones nobody can fetch. Taking
  // the first live event after them for a skip asked the device for nothing, on
  // nearly every conversation that was opened and then went on.
  it('does not take the events the device never stored for a hole', async () => {
    rpc.mockResolvedValueOnce({ events: [textEvent(109, 'a'), textEvent(110, 'b')], has_more: false });
    openFresh().onResult({ session: { ...session, last_seq: 112 }, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.historyLoaded).toBe(true));
    expect(chat()?.timeline.lastSeq).toBe(112);
    rpc.mockClear();

    useChat.getState().ingestEvent(SESSION, textEvent(113, 'the next turn'), DEVICE);

    expect(rpc).not.toHaveBeenCalled();
    expect(chat()?.timeline.order).toEqual(['b109', 'b110', 'b113']);
    expect(chat()?.timeline.contiguousSeq).toBe(113);
  });

  it('counts the same for a repair that has read everything the device holds', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [textEvent(5, 'old')] });
    rpc.mockResolvedValueOnce({ events: [textEvent(20, 'new')], has_more: false });
    handlers.onResult({ session: { ...session, last_seq: 22 }, resync: true, events: [] });
    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b5', 'b20']));
    expect(chat()?.timeline.lastSeq).toBe(22);
    rpc.mockClear();

    useChat.getState().ingestEvent(SESSION, textEvent(23, 'live'), DEVICE);

    expect(rpc).not.toHaveBeenCalled();
  });

  // Another client behind on the same account makes the gateway fetch events from far below
  // the window this tab has loaded, and fan them out to every subscriber.
  it('does not page back from an event that arrived far below the loaded window', async () => {
    rpc.mockResolvedValueOnce({
      events: Array.from({ length: 100 }, (_, i) => textEvent(5_000 + i, 'p')),
      has_more: true,
    });
    openFresh().onResult({ session: { ...session, last_seq: 5_099 }, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.historyLoaded).toBe(true));
    for (let seq = 4_001; seq <= 4_010; seq += 1) {
      useChat.getState().ingestEvent(SESSION, textEvent(seq, 'behind'), DEVICE);
    }
    expect(chat()?.timeline.oldestSeq).toBe(5_000);

    rpc.mockResolvedValueOnce({ events: [], has_more: false });
    await useChat.getState().loadOlder(KEY);

    // 4011 to 4999 are still to be asked for: the page starts below the window, not below the stray rows.
    expect(historyCalls().at(-1)).toEqual({ session_id: SESSION, before_seq: 5_000, limit: 200 });
  });

  // History carries no queue event (PROTOCOL 6.4), and a page moves the cursor over
  // everything the device had when it answered, so a queue snapshot still on its
  // way — the gateway holds live events while a fetch is out — arrived below the
  // cursor and was dropped as late. A snapshot is judged against the snapshots.
  it('applies a queue snapshot that arrives after a page moved the cursor past it', async () => {
    openAtTwo();
    rpc.mockResolvedValueOnce({ events: [textEvent(3, 'c'), textEvent(6, 'f')], has_more: false });
    useChat.getState().ingestEvent(SESSION, textEvent(4, 'd'), DEVICE);
    await vi.waitFor(() => expect(chat()?.timeline.lastSeq).toBe(6));

    useChat.getState().ingestEvent(SESSION, queueEvent(5, ['q1']), DEVICE);

    expect(chat()?.queue.map((item) => item.id)).toEqual(['q1']);
    // The cursor stays where the page left it.
    expect(chat()?.timeline.lastSeq).toBe(6);
    expect(updateCursor).not.toHaveBeenCalledWith(SESSION, 5);
  });

  it('applies a late todos snapshot that is newer than the one held, and not one that is older', async () => {
    openAtTwo();
    useChat.getState().ingestEvent(SESSION, todosEvent(3, 1), DEVICE);
    rpc.mockResolvedValueOnce({ events: [textEvent(4, 'd'), textEvent(8, 'h')], has_more: false });
    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);
    await vi.waitFor(() => expect(chat()?.timeline.lastSeq).toBe(8));
    expect(chat()?.todos[1]?.status).toBe('in_progress');

    useChat.getState().ingestEvent(SESSION, todosEvent(7, 2), DEVICE);
    expect(chat()?.todos[1]?.status).toBe('completed');
    // An older one, arriving after, does not take it back.
    useChat.getState().ingestEvent(SESSION, todosEvent(6, 0), DEVICE);
    expect(chat()?.todos[1]?.status).toBe('completed');
  });

  it('ignores a late queue snapshot that is older than the one held', async () => {
    openAtTwo();
    useChat.getState().ingestEvent(SESSION, queueEvent(3, ['a']), DEVICE);
    useChat.getState().ingestEvent(SESSION, queueEvent(8, ['b']), DEVICE);
    await vi.waitFor(() => expect(historyCalls()).toHaveLength(1));

    useChat.getState().ingestEvent(SESSION, queueEvent(5, ['stale']), DEVICE);

    expect(chat()?.queue.map((item) => item.id)).toEqual(['b']);
  });

  it('leaves the session summary alone for an event behind the cursor', async () => {
    openAtTwo();
    rpc.mockResolvedValueOnce({ events: [textEvent(3, 'c'), textEvent(6, 'f')], has_more: false });
    useChat.getState().ingestEvent(SESSION, textEvent(4, 'd'), DEVICE);
    await vi.waitFor(() => expect(chat()?.timeline.lastSeq).toBe(6));
    const before = summary();

    useChat.getState().ingestEvent(SESSION, statusEvent(5, 'idle'), DEVICE);

    expect(summary()).toBe(before);
  });

  it('keeps a pending send when a page holds an older message with the same words', async () => {
    openAtTwo();
    rpc.mockReturnValueOnce(new Promise(() => undefined));
    void useChat.getState().send(KEY, { text: 'continue', mode: 'auto' });
    expect(chat()?.timeline.optimistic).toHaveLength(1);

    rpc.mockResolvedValueOnce({ events: [userMessage(4, 'earlier', 'continue')], has_more: false });
    useChat.getState().ingestEvent(SESSION, textEvent(6, 'f'), DEVICE);
    await vi.waitFor(() => expect(chat()?.timeline.items.earlier).toBeDefined());

    // The older message is a row of its own; the send is still waiting for its echo.
    expect(chat()?.timeline.optimistic).toHaveLength(1);
    expect(Object.keys(useOutbox.getState().pending)).toHaveLength(1);
  });

  // Paging back is never capped, so a reader who has scrolled to the top holds more rows
  // than a live timeline may. A block that arrives late takes the cap's toll, and the
  // rows it drops are older history again, which the top of the transcript has to say.
  it('says there is older history again when a late event makes the cap drop rows', () => {
    openSession().onResult({ session, resync: false, events: [] });
    const rows = Array.from({ length: 3_200 }, (_, i) => i + 1)
      .filter((seq) => seq !== 1_600)
      .map((seq) => textEvent(seq, 'x'));
    useChat.setState((s) => ({
      sessions: {
        ...s.sessions,
        [KEY]: {
          ...s.sessions[KEY]!,
          timeline: mergeHistory(emptyTimeline(), rows),
          historyHasMore: false,
        },
      },
    }));
    expect(chat()?.timeline.order).toHaveLength(3_199);

    useChat.getState().ingestEvent(SESSION, textEvent(1_600, 'late'), DEVICE);

    expect(chat()?.timeline.dropped).toBe(200);
    expect(chat()?.historyHasMore).toBe(true);
  });

  // The same events can arrive before the newest page does, while the timeline is empty and
  // they are the first it has seen; the edge is the page's own, not the lowest of them.
  it('does not page back from events that arrived below the window before the newest page did', async () => {
    let resolveNewest!: (result: HistoryResult) => void;
    rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolveNewest = resolve; }));
    openFresh().onResult({ session: { ...session, last_seq: 5_099 }, resync: false, events: [] });
    for (let seq = 4_001; seq <= 4_010; seq += 1) {
      useChat.getState().ingestEvent(SESSION, textEvent(seq, 'behind'), DEVICE);
    }

    resolveNewest({
      events: Array.from({ length: 100 }, (_, i) => textEvent(5_000 + i, 'p')),
      has_more: true,
    });
    await vi.waitFor(() => expect(chat()?.historyLoaded).toBe(true));
    expect(chat()?.timeline.oldestSeq).toBe(5_000);

    rpc.mockResolvedValueOnce({ events: [], has_more: false });
    await useChat.getState().loadOlder(KEY);
    expect(historyCalls().at(-1)).toEqual({ session_id: SESSION, before_seq: 5_000, limit: 200 });
  });

  it('asks one question at a time, and the next for whatever opened meanwhile', async () => {
    openAtTwo();
    let resolveFirst!: (result: HistoryResult) => void;
    rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolveFirst = resolve; }));
    rpc.mockResolvedValueOnce({
      events: [textEvent(6, 'f'), textEvent(7, 'g'), textEvent(8, 'h')],
      has_more: false,
    });

    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);
    // Another event skips ahead while the first question is still out.
    useChat.getState().ingestEvent(SESSION, textEvent(9, 'i'), DEVICE);
    expect(historyCalls()).toHaveLength(1);

    resolveFirst({ events: [textEvent(3, 'c'), textEvent(4, 'd')], has_more: false });
    await vi.waitFor(() => expect(historyCalls()).toHaveLength(2));
    // The second starts where the first stopped, not back at the beginning.
    expect(historyCalls()[1]).toEqual({ session_id: SESSION, after_seq: 5, limit: 200 });
    await vi.waitFor(() => expect(chat()?.timeline.contiguousSeq).toBe(9));
    expect(chat()?.timeline.order).toEqual([
      'b1', 'b2', 'b3', 'b4', 'b5', 'b6', 'b7', 'b8', 'b9',
    ]);
  });

  it('keeps the hole open when the device cannot be asked, and stops asking on every event', async () => {
    openAtTwo();
    rpc.mockRejectedValueOnce(new RequestError({ code: 'device_offline', message: '' }));

    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);
    await vi.waitFor(() => expect(chat()?.error).toBe(strings.errors.deviceOffline));
    expect(chat()?.timeline.contiguousSeq).toBe(2);
    expect(hasGap(chat()!.timeline)).toBe(true);

    useChat.getState().ingestEvent(SESSION, textEvent(6, 'f'), DEVICE);
    expect(historyCalls()).toHaveLength(1);

    rpc.mockResolvedValueOnce({ events: [textEvent(3, 'c'), textEvent(4, 'd')], has_more: false });
    await useChat.getState().repair(KEY);

    expect(historyCalls()[1]).toMatchObject({ after_seq: 2 });
    expect(chat()?.timeline.order).toEqual(['b1', 'b2', 'b3', 'b4', 'b5', 'b6']);
    expect(chat()?.error).toBeNull();
  });

  it('repairs a hole that was still open when the conversation is subscribed again', async () => {
    openAtTwo();
    rpc.mockRejectedValueOnce(new RequestError({ code: 'timeout', message: '' }));
    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);
    await vi.waitFor(() => expect(chat()?.error).not.toBeNull());
    rpc.mockClear();

    useChat.getState().close(DEVICE, SESSION);
    const handlers = openSession();
    // The cursor the subscribe sends is already past the hole, so the gateway
    // has no way to know about it.
    expect(subscribe).toHaveBeenLastCalledWith(SESSION, 5, expect.any(Object));
    rpc.mockResolvedValueOnce({ events: [textEvent(3, 'c'), textEvent(4, 'd')], has_more: false });
    handlers.onResult({ session: { ...session, last_seq: 5 }, resync: false, events: [] });

    expect(historyCalls()).toEqual([{ session_id: SESSION, after_seq: 2, limit: 200 }]);
    await vi.waitFor(() =>
      expect(chat()?.timeline.order).toEqual(['b1', 'b2', 'b3', 'b4', 'b5']),
    );
  });

  it('walks forward a page at a time while the device has more', async () => {
    openAtTwo();
    rpc.mockResolvedValueOnce({
      events: Array.from({ length: 200 }, (_, i) => textEvent(3 + i, `p${3 + i}`)),
      has_more: true,
    });
    rpc.mockResolvedValueOnce({ events: [textEvent(203, 'z')], has_more: false });

    useChat.getState().ingestEvent(SESSION, textEvent(210, 'live'), DEVICE);

    await vi.waitFor(() => expect(chat()?.timeline.items.b203).toBeDefined());
    expect(historyCalls().map((call) => (call as { after_seq: number }).after_seq)).toEqual([2, 202]);
    expect(chat()?.timeline.order).toHaveLength(2 + 200 + 1 + 1);
    expect(chat()?.timeline.order.at(-1)).toBe('b210');
  });

  // The reader scrolls up while a hole too long to join is being given up on: the older
  // page that was on its way was asked for the rows that are about to be cleared.
  it('asks for the newest page after giving up on a repair, whatever older page was on its way', async () => {
    openSession().onResult({ session, resync: false, events: [textEvent(1_000, 'a')] });
    let resolveOlder!: (result: HistoryResult) => void;
    rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolveOlder = resolve; }));
    const older = useChat.getState().loadOlder(KEY);
    expect(chat()?.historyLoading).toBe(true);

    for (let page = 0; page < 5; page += 1) {
      rpc.mockResolvedValueOnce({
        events: Array.from({ length: 200 }, (_, i) => textEvent(1_100 + page * 200 + i, `p${i}`)),
        has_more: true,
      });
    }
    rpc.mockResolvedValueOnce({ events: [textEvent(3_000, 'newest')], has_more: false });
    useChat.getState().ingestEvent(SESSION, textEvent(1_002, 'live'), DEVICE);
    await vi.waitFor(() => expect(historyCalls().at(-1)).toEqual({ session_id: SESSION, limit: 200 }));

    // The older page answers late, for rows that are gone.
    resolveOlder({ events: [textEvent(900, 'old')], has_more: false });
    await older;
    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b3000']));
    expect(chat()?.historyLoaded).toBe(true);
    expect(chat()?.historyLoading).toBe(false);
  });

  it('starts the transcript again from the newest page when the stretch is too long to join', async () => {
    openAtTwo();
    for (let page = 0; page < 5; page += 1) {
      rpc.mockResolvedValueOnce({
        events: Array.from({ length: 200 }, (_, i) => textEvent(100 + page * 200 + i, `p${i}`)),
        has_more: true,
      });
    }
    rpc.mockResolvedValueOnce({ events: [textEvent(2_000, 'newest')], has_more: false });

    useChat.getState().ingestEvent(SESSION, textEvent(2_001, 'live'), DEVICE);

    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b2000']));
    const calls = historyCalls() as { after_seq?: number }[];
    expect(calls.map((call) => call.after_seq)).toEqual([2, 299, 499, 699, 899, undefined]);
    expect(chat()?.historyHasMore).toBe(false);
    // The repair that gave up is over, and the newest page is what is being read.
    expect(chat()?.repairing).toBe(false);
  });

  it('takes the snapshots a page carries only while nothing newer has arrived', async () => {
    openAtTwo();
    rpc.mockResolvedValueOnce({ events: [todosEvent(3, 2)], has_more: false });
    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);
    await vi.waitFor(() => expect(chat()?.todos[1]?.status).toBe('completed'));

    // A live snapshot that lands while the next page is out is newer than it.
    let resolvePage!: (result: HistoryResult) => void;
    rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolvePage = resolve; }));
    useChat.getState().ingestEvent(SESSION, textEvent(8, 'h'), DEVICE);
    useChat.getState().ingestEvent(SESSION, todosEvent(9, 1), DEVICE);
    expect(chat()?.todos[1]?.status).toBe('in_progress');
    resolvePage({ events: [todosEvent(7, 2)], has_more: false });

    await vi.waitFor(() => expect(chat()?.timeline.contiguousSeq).toBe(9));
    expect(chat()?.todos[1]?.status).toBe('in_progress');
  });
});

/**
 * A conversation the device could not be asked for. It used to show "No
 * messages yet" and never ask again; now it says what happened, can be retried,
 * and is told when the answer exists.
 */
describe('a conversation that could not be read', () => {
  const historyCalls = () => rpc.mock.calls.filter((c) => c[0] === 'session.history').map((c) => c[1]);
  const offline = () => new RequestError({ code: 'device_offline', message: '' });

  it('says why, and does not count the newest page as read', async () => {
    rpc.mockRejectedValueOnce(offline());
    openFresh().onResult({ session, resync: false, events: [] });

    await vi.waitFor(() => expect(chat()?.error).toBe(strings.errors.deviceOffline));
    expect(chat()?.historyLoading).toBe(false);
    expect(chat()?.historyLoaded).toBe(false);
    // Still owed, so a scroll or a retry can ask again.
    expect(chat()?.historyHasMore).toBe(true);
  });

  it('counts an empty conversation as read once the device has said so', async () => {
    openFresh().onResult({ session, resync: false, events: [] });

    await vi.waitFor(() => expect(chat()?.historyLoaded).toBe(true));
    expect(chat()?.error).toBeNull();
    expect(chat()?.timeline.order).toEqual([]);
  });

  it('reads the newest page when it is tried again', async () => {
    rpc.mockRejectedValueOnce(offline());
    openFresh().onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.error).not.toBeNull());

    rpc.mockResolvedValueOnce({ events: [textEvent(8, 'there you are')], has_more: false });
    useChat.getState().retry(KEY);
    const again = subscribe.mock.calls.at(-1)![2] as Handlers;
    again.onResult({ session, resync: false, events: [] });

    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b8']));
    expect(chat()?.error).toBeNull();
    expect(chat()?.historyLoaded).toBe(true);
  });

  // The gateway restart: the page reloads while every device is offline, the
  // device comes back and streams again, and only then is the history asked for.
  it('reads the history that comes before the rows that arrived live in the meantime', async () => {
    rpc.mockRejectedValueOnce(offline());
    openFresh().onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.error).not.toBeNull());
    useChat.getState().ingestEvent(SESSION, textEvent(30, 'live'), DEVICE);

    rpc.mockResolvedValueOnce({ events: [textEvent(20, 'before'), textEvent(21, 'before too')], has_more: false });
    useChat.getState().retry(KEY);
    (subscribe.mock.calls.at(-1)![2] as Handlers).onResult({ session, resync: false, events: [] });

    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b20', 'b21', 'b30']));
    expect(historyCalls().at(-1)).toEqual({ session_id: SESSION, before_seq: 30, limit: 200 });
    expect(chat()?.error).toBeNull();
  });

  it('repairs a hole that opened while the first read was failing, once it has succeeded', async () => {
    rpc.mockRejectedValueOnce(offline());
    openFresh().onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.error).not.toBeNull());
    // The device is back and streaming, and a burst skips ahead. The failure
    // holds the repair back: it would only fail the same way.
    useChat.getState().ingestEvent(SESSION, textEvent(30, 'live'), DEVICE);
    useChat.getState().ingestEvent(SESSION, textEvent(34, 'live again'), DEVICE);
    expect(historyCalls()).toHaveLength(1);

    rpc.mockResolvedValueOnce({ events: [textEvent(20, 'before')], has_more: false });
    rpc.mockResolvedValueOnce({ events: [textEvent(31, 'gap'), textEvent(33, 'gap too')], has_more: false });
    useChat.getState().retry(KEY);
    (subscribe.mock.calls.at(-1)![2] as Handlers).onResult({ session, resync: false, events: [] });

    await vi.waitFor(() =>
      expect(chat()?.timeline.order).toEqual(['b20', 'b30', 'b31', 'b33', 'b34']),
    );
    expect(historyCalls().at(-1)).toEqual({ session_id: SESSION, after_seq: 30, limit: 200 });
    expect(chat()?.error).toBeNull();
  });

  // A conversation that was read and has no rows owes the device nothing by being empty, so
  // a failure that nothing asks about could never be cleared and its Retry sent no request.
  it('clears the failure of a subscribe on a conversation that was read empty', async () => {
    const handlers = openFresh();
    handlers.onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.historyLoaded).toBe(true));
    handlers.onError(new RequestError({ code: 'internal', message: '' }));
    expect(chat()?.error).toBe(strings.errors.historyFailed);

    rpc.mockClear();
    useChat.getState().retry(KEY);
    (subscribe.mock.calls.at(-1)![2] as Handlers).onResult({ session, resync: false, events: [] });

    expect(chat()?.error).toBeNull();
    // The empty read is not asked again: it has no newest page to ask for.
    expect(rpc).not.toHaveBeenCalled();
  });

  it('repairs a hole in a conversation with no rows once the device answers', async () => {
    openFresh().onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.historyLoaded).toBe(true));
    // Two state-only events with numbers skipped between them: no row, but a hole.
    useChat.getState().ingestEvent(SESSION, statusEvent(1, 'running'), DEVICE);
    rpc.mockRejectedValueOnce(offline());
    useChat.getState().ingestEvent(SESSION, statusEvent(4, 'idle'), DEVICE);
    await vi.waitFor(() => expect(chat()?.error).toBe(strings.errors.deviceOffline));
    expect(hasGap(chat()!.timeline)).toBe(true);

    rpc.mockClear();
    rpc.mockResolvedValueOnce({ events: [], has_more: false });
    useChat.getState().retry(KEY);
    (subscribe.mock.calls.at(-1)![2] as Handlers).onResult({ session, resync: false, events: [] });

    await vi.waitFor(() => expect(chat()?.error).toBeNull());
    expect(historyCalls()).toEqual([{ session_id: SESSION, after_seq: 1, limit: 200 }]);
    expect(hasGap(chat()!.timeline)).toBe(false);
  });

  it('says why a subscribe failed, and subscribes again on retry', () => {
    openFresh().onError(new RequestError({ code: 'timeout', message: '' }));
    expect(chat()?.error).toBe(strings.errors.timeout);
    expect(chat()?.ready).toBe(true);

    subscribe.mockClear();
    useChat.getState().retry(KEY);
    expect(subscribe).toHaveBeenCalledWith(SESSION, undefined, expect.any(Object));
  });

  it('does not report an older page that fails: scrolling up asks for it again', async () => {
    const handlers = openFresh();
    rpc.mockResolvedValueOnce({ events: [textEvent(40, 'newest')], has_more: true });
    handlers.onResult({ session, resync: false, events: [] });
    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b40']));

    rpc.mockRejectedValueOnce(offline());
    await useChat.getState().loadOlder(KEY);

    expect(chat()?.error).toBeNull();
    expect(chat()?.historyLoading).toBe(false);
    expect(chat()?.historyHasMore).toBe(true);
  });

  it('keeps a failure in view until what the subscribe still owes has been paid', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [textEvent(1, 'a'), textEvent(2, 'b')] });
    rpc.mockRejectedValueOnce(offline());
    useChat.getState().ingestEvent(SESSION, textEvent(5, 'e'), DEVICE);
    await vi.waitFor(() => expect(chat()?.error).toBe(strings.errors.deviceOffline));

    let resolveRepair!: (result: HistoryResult) => void;
    rpc.mockReturnValueOnce(new Promise<HistoryResult>((resolve) => { resolveRepair = resolve; }));
    useChat.getState().retry(KEY);
    (subscribe.mock.calls.at(-1)![2] as Handlers).onResult({ session, resync: false, events: [] });
    // The subscribe worked, but the hole is still open: no flicker of "all well".
    expect(chat()?.error).toBe(strings.errors.deviceOffline);

    resolveRepair({ events: [textEvent(3, 'c'), textEvent(4, 'd')], has_more: false });
    await vi.waitFor(() => expect(chat()?.error).toBeNull());
  });

  it('clears a failure at once when the subscribe leaves nothing to fetch', () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [textEvent(1, 'a')] });
    handlers.onError(offline());
    expect(chat()?.error).toBe(strings.errors.deviceOffline);

    handlers.onResult({ session, resync: false, events: [] });
    expect(chat()?.error).toBeNull();
  });

  it('does not pile a retry on a load or a repair that is already on its way', async () => {
    rpc.mockReturnValueOnce(new Promise(() => undefined));
    openFresh().onResult({ session, resync: false, events: [] });
    expect(chat()?.historyLoading).toBe(true);

    subscribe.mockClear();
    useChat.getState().retry(KEY);
    expect(subscribe).not.toHaveBeenCalled();
  });
});

describe('sending', () => {
  it('clears the outbox once the gateway confirms', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockResolvedValueOnce({ accepted: 'queued', queued_id: 'q1' });

    await useChat.getState().send(KEY, { text: 'hello', mode: 'auto' });

    expect(rpc).toHaveBeenCalledWith(
      'session.send',
      expect.objectContaining({ session_id: SESSION, text: 'hello', mode: 'auto' }),
      expect.objectContaining({ id: expect.any(String) }),
    );
    expect(useOutbox.getState().pending).toEqual({});
  });

  it('drops a definitely-rejected message and rethrows', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockRejectedValueOnce(new RequestError({ code: 'conflict', message: 'terminal' }));

    await expect(
      useChat.getState().send(KEY, { text: 'nope', mode: 'auto' }),
    ).rejects.toBeInstanceOf(RequestError);
    expect(useOutbox.getState().pending).toEqual({});
  });

  it('keeps an uncertain send for a manual retry with the same request id', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockRejectedValueOnce(new RequestError({ code: 'timeout', message: 'no reply' }));

    await useChat.getState().send(KEY, { text: 'maybe', mode: 'auto' });
    const entries = Object.values(useOutbox.getState().pending);
    expect(entries).toHaveLength(1);
    expect(entries[0]?.error).toBe('no reply');
    const id = entries[0]?.id as string;

    rpc.mockResolvedValueOnce({ accepted: 'sent' });
    await useChat.getState().retrySend(id);

    const sends = rpc.mock.calls.filter((c) => c[0] === 'session.send');
    expect(sends).toHaveLength(2);
    expect((sends[0]?.[2] as { id: string }).id).toBe(id);
    expect((sends[1]?.[2] as { id: string }).id).toBe(id);
    expect(useOutbox.getState().pending).toEqual({});
  });

  it('shows the message before the request is answered, under the request id', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });

    let release = (_: unknown) => {};
    rpc.mockImplementationOnce(() => new Promise((resolve) => (release = resolve)));
    const sending = useChat.getState().send(KEY, { text: 'run the tests', mode: 'auto' });

    // Synchronously, before the gateway has replied.
    const optimistic = chat()?.timeline.optimistic ?? [];
    expect(optimistic).toHaveLength(1);
    expect(optimistic[0]?.text).toBe('run the tests');
    const id = optimistic[0]?.id as string;
    expect(rpc.mock.calls.at(-1)?.[2]).toMatchObject({ id });
    // It is not a device event, so the replay cursor stays where it was.
    expect(chat()?.timeline.lastSeq).toBe(0);

    release({ accepted: 'sent' });
    await sending;
    expect(chat()?.timeline.optimistic).toHaveLength(1);

    // A12: the device echoes the request id, so the row is replaced in place.
    useChat.getState().ingestEvent(SESSION, userMessage(3, id, 'run the tests'), DEVICE);
    expect(chat()?.timeline.optimistic).toEqual([]);
    expect(chat()?.timeline.order).toEqual([id]);
  });

  it('records the attachment metadata on the optimistic row', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockResolvedValueOnce({ accepted: 'sent' });

    await useChat.getState().send(KEY, {
      text: 'look at this',
      mode: 'auto',
      attachments: [{ name: 'shot.png', mime: 'image/png', data_base64: 'AAAA' }],
    });

    expect(chat()?.timeline.optimistic[0]?.attachments).toEqual([
      { name: 'shot.png', mime: 'image/png', size: 3 },
    ]);
  });

  it('gives the row up to the queue when the device queues the message', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockResolvedValueOnce({ accepted: 'queued', queued_id: 'q-1' });

    await useChat.getState().send(KEY, { text: 'and then lint', mode: 'auto' });
    // The composer's Up next list stands for it from here on.
    expect(chat()?.timeline.optimistic).toEqual([]);
  });

  it('puts an edited message back under its queue_ts, and sends none otherwise (A43)', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockResolvedValue({ accepted: 'queued', queued_id: 'q-2' });

    await useChat.getState().send(KEY, { text: 'edited', mode: 'queue', queueTs: 1_000 });
    expect(rpc.mock.calls.at(-1)?.[1]).toEqual({
      session_id: SESSION,
      text: 'edited',
      mode: 'queue',
      queue_ts: 1_000,
    });

    await useChat.getState().send(KEY, { text: 'plain', mode: 'auto' });
    expect(rpc.mock.calls.at(-1)?.[1]).not.toHaveProperty('queue_ts');
  });

  it('keeps the queue_ts for a Retry of an uncertain put-back (A43)', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockRejectedValueOnce(new RequestError({ code: 'timeout', message: 'no reply' }));

    await useChat.getState().send(KEY, { text: 'edited', mode: 'queue', queueTs: 1_000 });
    const entry = Object.values(useOutbox.getState().pending)[0];
    expect(entry?.queueTs).toBe(1_000);

    rpc.mockResolvedValueOnce({ accepted: 'queued', queued_id: entry?.id });
    await useChat.getState().retrySend(entry?.id as string);
    const retried = rpc.mock.calls.filter((c) => c[0] === 'session.send').at(-1);
    expect(retried?.[1]).toMatchObject({ mode: 'queue', queue_ts: 1_000 });
    expect((retried?.[2] as { id: string }).id).toBe(entry?.id);
  });

  /**
   * A19: on a shared session a message sent into a running terminal turn is a
   * queue entry and nothing else. The block arrives when the CLI takes it, with
   * a `first_seq` after the output of the turn it waited for.
   */
  it('holds a message sent into a running terminal turn in the queue alone', async () => {
    useSessions.setState({
      sessions: { [KEY]: { ...session, control: 'shared', origin: 'terminal' } },
      loaded: true,
    });
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    useChat.getState().ingestEvent(SESSION, textEvent(10, 'Working on the refactor.'), DEVICE);

    rpc.mockResolvedValueOnce({ accepted: 'queued', queued_id: 'q-held' });
    await useChat.getState().send(KEY, { text: 'and then lint', mode: 'auto' });
    const id = (rpc.mock.calls.at(-1)?.[2] as { id: string }).id;

    useChat.getState().ingestEvent(SESSION, queueEvent(11, [id]), DEVICE);
    expect(chat()?.queue.map((q) => q.id)).toEqual([id]);
    // No bubble anywhere: not optimistic, not a block.
    expect(chat()?.timeline.optimistic).toEqual([]);
    expect(chat()?.timeline.order).toEqual(['b10']);

    // The CLI took it when the turn ended.
    useChat.getState().ingestEvent(SESSION, queueEvent(12, []), DEVICE);
    useChat.getState().ingestEvent(
      SESSION,
      {
        seq: 14,
        ts: 1_014,
        kind: 'user_message',
        first_seq: 13,
        block_id: id,
        text: 'and then lint',
        source: 'remote',
        delivery: 'delivered',
      } as SessionEvent,
      DEVICE,
    );
    expect(chat()?.queue).toEqual([]);
    expect(chat()?.timeline.order).toEqual(['b10', id]);
  });

  it('gives the row up to a queue snapshot that names it, after an uncertain send', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockRejectedValueOnce(new RequestError({ code: 'timeout', message: 'no reply' }));

    await useChat.getState().send(KEY, { text: 'and then lint', mode: 'auto' });
    const id = chat()?.timeline.optimistic[0]?.id as string;

    // The device did get it: its snapshot names the request id (A12).
    useChat.getState().ingestEvent(SESSION, queueEvent(4, [id]), DEVICE);
    expect(chat()?.queue.map((q) => q.id)).toEqual([id]);
    expect(chat()?.timeline.optimistic).toEqual([]);
  });

  it('stops offering a Retry for a message removed from the queue', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockRejectedValueOnce(new RequestError({ code: 'timeout', message: 'no reply' }));
    await useChat.getState().send(KEY, { text: 'never mind', mode: 'auto' });
    const id = Object.keys(useOutbox.getState().pending)[0] as string;

    rpc.mockResolvedValueOnce({});
    await useChat.getState().removeQueued(KEY, id);
    expect(useOutbox.getState().pending).toEqual({});
  });

  it('removes the row when the gateway definitely refuses the message', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockRejectedValueOnce(new RequestError({ code: 'conflict', message: 'terminal' }));

    await expect(
      useChat.getState().send(KEY, { text: 'nope', mode: 'auto' }),
    ).rejects.toBeInstanceOf(RequestError);
    expect(chat()?.timeline.optimistic).toEqual([]);
  });

  it('keeps the row when delivery is merely uncertain, and Retry reuses it', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockRejectedValueOnce(new RequestError({ code: 'timeout', message: 'no reply' }));

    await useChat.getState().send(KEY, { text: 'maybe', mode: 'auto' });
    const id = chat()?.timeline.optimistic[0]?.id as string;
    expect(id).toBeTruthy();

    rpc.mockResolvedValueOnce({ accepted: 'sent' });
    await useChat.getState().retrySend(id);
    expect(chat()?.timeline.optimistic.map((b) => b.id)).toEqual([id]);
  });

  it('keeps the row across a resync and drops it once history confirms it', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockResolvedValueOnce({ accepted: 'sent' });
    await useChat.getState().send(KEY, { text: 'survive the reconnect', mode: 'auto' });
    const id = chat()?.timeline.optimistic[0]?.id as string;

    // The resync empties the timeline and pages history again.
    rpc.mockResolvedValueOnce({
      events: [userMessage(7, id, 'survive the reconnect')],
      has_more: false,
    });
    handlers.onResult({ session, resync: true, events: [] });
    expect(chat()?.timeline.order).toEqual([]);
    expect(chat()?.timeline.optimistic.map((b) => b.id)).toEqual([id]);

    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual([id]));
    expect(chat()?.timeline.optimistic).toEqual([]);
  });

  it('forwards attachments only when there are any', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [] });
    rpc.mockResolvedValue({ accepted: 'sent' });

    await useChat.getState().send(KEY, { text: 'bare', mode: 'auto' });
    expect(rpc.mock.calls.at(-1)?.[1]).not.toHaveProperty('attachments');

    await useChat.getState().send(KEY, {
      text: 'with file',
      mode: 'interrupt',
      attachments: [{ name: 'a.png', mime: 'image/png', data_base64: 'AA==' }],
    });
    expect(rpc.mock.calls.at(-1)?.[1]).toMatchObject({
      mode: 'interrupt',
      attachments: [{ name: 'a.png', mime: 'image/png', data_base64: 'AA==' }],
    });
  });
});

/**
 * A tab used to keep a full `ChatSession` for every conversation it had ever
 * opened. `close` unsubscribed and deleted nothing, so an afternoon of moving
 * between sessions was an afternoon of transcripts held in memory.
 */
describe('closed conversations', () => {
  const open = (id: string): void => {
    useChat.getState().open(DEVICE, id);
    useChat.getState().close(DEVICE, id);
  };

  it('keeps the recent ones and evicts the rest with their command lists', async () => {
    const { useCommands } = await import('../src/stores/commands');
    useCommands.setState({
      entries: Object.fromEntries(
        ['ses-1', 'ses-2', 'ses-3', 'ses-4', 'ses-5'].map((id) => [
          id,
          { commands: [], at: 1, loading: false },
        ]),
      ),
    });

    for (const id of ['ses-1', 'ses-2', 'ses-3', 'ses-4', 'ses-5']) open(id);

    const kept = Object.keys(useChat.getState().sessions).sort();
    expect(kept).toEqual([`${DEVICE}/ses-3`, `${DEVICE}/ses-4`, `${DEVICE}/ses-5`]);
    expect(Object.keys(useCommands.getState().entries).sort()).toEqual(['ses-3', 'ses-4', 'ses-5']);
  });

  it('never evicts the conversation on screen, which closes on every reconnect', () => {
    for (const id of ['ses-1', 'ses-2', 'ses-3', 'ses-4']) open(id);
    // The one the reader is on: closed by the effect's cleanup, then opened
    // again the moment the socket is back.
    useChat.getState().open(DEVICE, 'ses-1');
    for (const id of ['ses-5', 'ses-6', 'ses-7']) open(id);

    expect(useChat.getState().sessions[`${DEVICE}/ses-1`]).toBeDefined();
  });
});
