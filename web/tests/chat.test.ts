import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { HistoryResult, SubscribeResult } from '../src/protocol/frames';
import type { Session, SessionEvent, Trigger } from '../src/protocol/types';

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

/** Runs `open()` and hands back the subscribe handlers the store registered. */
function openSession(): {
  onResult: (result: SubscribeResult) => void;
  onError: (err: unknown) => void;
} {
  useChat.getState().open(DEVICE, SESSION);
  const call = subscribe.mock.calls.at(-1);
  if (!call) throw new Error('open() did not subscribe');
  return call[2] as ReturnType<typeof openSession>;
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

  it('resets the timeline on resync and then pages history', async () => {
    const handlers = openSession();
    handlers.onResult({ session, resync: false, events: [textEvent(5, 'old')] });
    expect(chat()?.timeline.order).toEqual(['b5']);

    rpc.mockResolvedValueOnce({ events: [textEvent(20, 'replayed')], has_more: false });
    handlers.onResult({ session, resync: true, events: [] });

    expect(chat()?.timeline.order).toEqual([]);
    expect(chat()?.todos).toEqual([]);
    await vi.waitFor(() => expect(chat()?.timeline.order).toEqual(['b20']));
    expect(rpc).toHaveBeenCalledWith(
      'session.history',
      expect.objectContaining({ session_id: SESSION }),
    );
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
    const handlers = openSession();
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
    const handlers = openSession();
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
    const handlers = openSession();
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
    const handlers = openSession();
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
    useChat.getState().ingestEvent(SESSION, textEvent(9, 'duplicate'));
    useChat.getState().ingestEvent(SESSION, textEvent(4, 'late'));
    expect(chat()?.timeline.order).toEqual(['b9']);
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
