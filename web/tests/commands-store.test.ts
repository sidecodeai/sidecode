/**
 * A27 — the two stores behind the panel: when the list is asked for, and what
 * running one command does to the timeline before the device answers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '../src/protocol/types';

const rpc = vi.fn();
const subscribe = vi.fn();
const unsubscribe = vi.fn();
const updateCursor = vi.fn();

vi.mock('../src/lib/gateway', () => ({
  rpc: (...args: unknown[]) => rpc(...args),
  getSocket: () => ({ subscribe, unsubscribe, updateCursor }),
  setSocket: vi.fn(),
}));

const { COMMANDS_TTL_MS, useCommands } = await import('../src/stores/commands');
const { useChat } = await import('../src/stores/chat');
const { useSessions } = await import('../src/stores/sessions');
const { emptyTimeline, selectView } = await import('../src/stores/timeline');
const { RequestError } = await import('../src/lib/ws');

const DEVICE = 'dev-mac';
const SESSION = 'ses-codex';
const KEY = `${DEVICE}/${SESSION}`;

const session: Session = {
  session_id: SESSION,
  device_id: DEVICE,
  agent: 'codex',
  title: 'Typecheck the web app',
  cwd: '/Users/me/dev/remote-control/web',
  git: null,
  state: 'idle',
  state_detail: null,
  origin: 'remote',
  control: 'remote',
  model: 'gpt-5.4-codex',
  permission_mode: 'on-request',
  effort: 'medium',
  created_at: 1,
  updated_at: 2,
  last_seq: 0,
  archived: false,
  turn: null,
  todos: null,
  usage: null,
  queued: 0,
};

const list = [{ name: 'compact', description: 'Summarise the conversation' }];

beforeEach(() => {
  rpc.mockReset();
  useCommands.setState({ entries: {} });
  useChat.setState({ sessions: {} });
  useSessions.setState({ sessions: { [KEY]: session }, loaded: true, agentFilter: null });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('the command list', () => {
  it('asks the device once when the conversation opens', async () => {
    rpc.mockResolvedValue({ commands: list });
    useCommands.getState().open(SESSION);
    await vi.waitFor(() => expect(rpc).toHaveBeenCalledTimes(1));
    expect(rpc).toHaveBeenCalledWith('session.commands', { session_id: SESSION });
    useCommands.getState().open(SESSION);
    expect(rpc).toHaveBeenCalledTimes(1);
  });

  it('keeps an answer that is under a minute old when `/` is typed', async () => {
    rpc.mockResolvedValue({ commands: list });
    await useCommands.getState().fetch(SESSION);
    useCommands.getState().refresh(SESSION);
    expect(rpc).toHaveBeenCalledTimes(1);
  });

  it('asks again when the answer has gone stale', async () => {
    vi.useFakeTimers();
    rpc.mockResolvedValue({ commands: list });
    await useCommands.getState().fetch(SESSION);
    vi.setSystemTime(Date.now() + COMMANDS_TTL_MS + 1);
    useCommands.getState().refresh(SESSION);
    expect(rpc).toHaveBeenCalledTimes(2);
  });

  it('asks again after an empty answer, however fresh it is', async () => {
    rpc.mockResolvedValue({ commands: [] });
    await useCommands.getState().fetch(SESSION);
    useCommands.getState().refresh(SESSION);
    expect(rpc).toHaveBeenCalledTimes(2);
  });

  it('leaves the list empty when the device refuses, without throwing', async () => {
    rpc.mockRejectedValue(new RequestError({ code: 'unsupported', message: 'no commands' }));
    await useCommands.getState().fetch(SESSION);
    expect(useCommands.getState().entries[SESSION]?.commands).toEqual([]);
    expect(useCommands.getState().entries[SESSION]?.loading).toBe(false);
  });
});

describe('running one command', () => {
  const rows = () =>
    selectView(useChat.getState().sessions[KEY]?.timeline ?? emptyTimeline(), 'detailed').roots;

  beforeEach(() => {
    useChat.getState().open(DEVICE, SESSION);
  });

  it('draws the row under the request id and sends the name and argument', async () => {
    rpc.mockResolvedValue({});
    await useChat.getState().runCommand(KEY, 'review', 'focus on the retry logic');

    const [type, params, options] = rpc.mock.calls[0] ?? [];
    expect(type).toBe('session.command');
    expect(params).toEqual({
      session_id: SESSION,
      name: 'review',
      argument: 'focus on the retry logic',
    });
    // A12: the request id is the block id the device will echo.
    const row = rows().at(-1);
    expect(row?.key).toBe((options as { id: string }).id);
    expect(row?.event).toMatchObject({
      kind: 'user_message',
      text: '/review focus on the retry logic',
      source: 'remote',
    });
    // The gateway answered, so the row is no longer an uncertain delivery.
    expect(row?.pending?.accepted).toBe('sent');
  });

  it('leaves the argument out when the command takes none', async () => {
    rpc.mockResolvedValue({});
    await useChat.getState().runCommand(KEY, 'compact');
    expect(rpc.mock.calls[0]?.[1]).toEqual({ session_id: SESSION, name: 'compact' });
    expect(rows().at(-1)?.event).toMatchObject({ text: '/compact' });
  });

  it('takes the row away again when the device refuses', async () => {
    rpc.mockRejectedValue(new RequestError({ code: 'conflict', message: 'wait for the turn' }));
    await expect(useChat.getState().runCommand(KEY, 'compact')).rejects.toThrow('wait for the turn');
    expect(rows()).toHaveLength(0);
  });
});
