/**
 * Amendment A14: a steered message is drawn where the agent reads it, not where
 * it was sent. This replays the session that exposed the defect (a shared Codex
 * turn) and pins both halves of the ruling: the optimistic row stays at the
 * bottom for the whole wait, and the device's block lands in terminal order.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session, SessionEvent } from '../src/protocol/types';

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
const { emptyTimeline, selectView } = await import('../src/stores/timeline');

const DEVICE = 'dev-1';
const SESSION = 'ses-1';
const KEY = `${DEVICE}/${SESSION}`;

const session: Session = {
  session_id: SESSION,
  device_id: DEVICE,
  agent: 'codex',
  title: 'Shared Codex session',
  cwd: '/Users/me',
  git: null,
  state: 'running',
  state_detail: null,
  origin: 'terminal',
  control: 'shared',
  model: 'gpt-5-codex',
  permission_mode: 'default',
  effort: 'high',
  created_at: 1,
  updated_at: 2,
  last_seq: 45,
  archived: false,
  turn: { turn_id: 't-1', started_at: 3 },
  todos: { total: 0, done: 0 },
  usage: null,
  queued: 0,
};

const assistantText = (seq: number, firstSeq: number, blockId: string, text: string): SessionEvent =>
  ({
    seq,
    ts: 1_000 + seq,
    kind: 'assistant_text',
    block_id: blockId,
    first_seq: firstSeq,
    text,
    done: true,
  }) as SessionEvent;

const assistantDelta = (seq: number, firstSeq: number, blockId: string, delta: string): SessionEvent =>
  ({
    seq,
    ts: 1_000 + seq,
    kind: 'assistant_text',
    block_id: blockId,
    first_seq: firstSeq,
    delta,
  }) as SessionEvent;

const toolCall = (seq: number, firstSeq: number, blockId: string): SessionEvent =>
  ({
    seq,
    ts: 1_000 + seq,
    kind: 'tool_call',
    block_id: blockId,
    first_seq: firstSeq,
    tool: 'shell',
    tool_kind: 'shell',
    title: 'pwd',
    status: 'succeeded',
    started_at: 1_000 + firstSeq,
  }) as SessionEvent;

const remoteUserMessage = (seq: number, firstSeq: number, blockId: string, text: string): SessionEvent =>
  ({
    seq,
    ts: 1_000 + seq,
    kind: 'user_message',
    block_id: blockId,
    first_seq: firstSeq,
    text,
    source: 'remote',
    delivery: 'delivered',
  }) as SessionEvent;

const turnCompleted = (seq: number): SessionEvent =>
  ({ seq, ts: 1_000 + seq, kind: 'turn_completed', turn_id: 't-1', stop_reason: 'completed' }) as SessionEvent;

const chat = () => useChat.getState().sessions[KEY];
const roots = () => selectView(chat()?.timeline ?? emptyTimeline(), 'detailed').roots;
const lastRow = () => roots().at(-1);

/** Runs `open()` and hands back the subscribe handlers the store registered. */
function openSession() {
  useChat.getState().open(DEVICE, SESSION);
  const call = subscribe.mock.calls.at(-1);
  if (!call) throw new Error('open() did not subscribe');
  const handlers = call[2] as { onResult: (result: unknown) => void };
  handlers.onResult({ session, resync: false, events: [] });
  return handlers;
}

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

describe('a steered send (A14)', () => {
  it('keeps the pending row at the bottom until the device emits the block, then orders it as the terminal does', async () => {
    openSession();
    rpc.mockResolvedValueOnce({ accepted: 'steered' });

    await useChat.getState().send(KEY, { text: '有什么项目', mode: 'auto' });
    const id = chat()?.timeline.optimistic[0]?.id as string;
    expect(id).toBeTruthy();
    // `steered` is not `queued`: the row is the message until the device echoes it.
    expect(lastRow()?.pending?.text).toBe('有什么项目');
    // The gateway answered, so the wait is the agent's, not a delivery doubt.
    expect(chat()?.timeline.optimistic[0]?.accepted).toBe('steered');

    // The step Codex was already running streams on, with an earlier `first_seq`.
    useChat.getState().ingestEvent(SESSION, assistantDelta(46, 46, 'a1', '我确认一下'), DEVICE);
    useChat.getState().ingestEvent(SESSION, assistantDelta(49, 46, 'a1', '当前工作目录。'), DEVICE);
    expect(lastRow()?.pending?.id).toBe(id);

    useChat.getState().ingestEvent(SESSION, toolCall(51, 50, 'tool-pwd'), DEVICE);
    expect(lastRow()?.pending?.id).toBe(id);

    useChat.getState().ingestEvent(
      SESSION,
      assistantText(58, 53, 'a2', '当前工作目录是 /Users/dev。'),
      DEVICE,
    );
    // Seconds of the running turn later, the row is still the last thing drawn.
    expect(lastRow()?.pending?.id).toBe(id);
    expect(roots().map((r) => r.key)).toEqual(['a1', 'tool-pwd', 'a2', id]);

    // Codex reads the steered message at its next step: the device emits it now.
    useChat.getState().ingestEvent(SESSION, remoteUserMessage(60, 57, id, '有什么项目'), DEVICE);
    expect(chat()?.timeline.optimistic).toEqual([]);
    expect(lastRow()?.pending).toBeUndefined();

    useChat.getState().ingestEvent(SESSION, assistantText(65, 59, 'a3', '我查看一下当前目录…'), DEVICE);

    // The terminal's order: the prompt sits after the answer that preceded it.
    expect(roots().map((r) => r.key)).toEqual(['a1', 'tool-pwd', 'a2', id, 'a3']);
    expect(roots().map((r) => r.event.kind)).toEqual([
      'assistant_text',
      'tool_call',
      'assistant_text',
      'user_message',
      'assistant_text',
    ]);
  });

  it('survives a turn end and a resync while the agent has not taken it', async () => {
    const handlers = openSession();
    rpc.mockResolvedValueOnce({ accepted: 'steered' });
    await useChat.getState().send(KEY, { text: 'and then lint', mode: 'auto' });
    const id = chat()?.timeline.optimistic[0]?.id as string;

    useChat.getState().ingestEvent(SESSION, turnCompleted(70), DEVICE);
    expect(chat()?.timeline.optimistic.map((b) => b.id)).toEqual([id]);

    rpc.mockResolvedValueOnce({ events: [], has_more: false });
    handlers.onResult({ session, resync: true, events: [] });
    expect(chat()?.timeline.optimistic.map((b) => b.id)).toEqual([id]);
    expect(lastRow()?.pending?.text).toBe('and then lint');
  });
});
