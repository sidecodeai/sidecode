/**
 * The conversation record: the transcript in the panel while a call runs, the history written
 * down while it runs and when it ends, and what the panel shows with no call on.
 */
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { forgetHistory, keepRecord, type CallRecord } from '../../src/features/live/history';
import { useLive } from '../../src/features/live/store';
import { liveTables } from '../../src/features/live/strings';
import { useAuth } from '../../src/stores/auth';
import { useSettings } from '../../src/stores/settings';
import { type FakeWebSocket } from './audioFakes';
import {
  afterLive,
  beforeLive,
  endCall,
  en,
  type LiveEnv,
  openPanel,
  renderEntry,
  startCall,
} from './harness';

let env: LiveEnv;

beforeEach(() => {
  env = beforeLive();
});

afterEach(afterLive);

const say = (ws: FakeWebSocket, speaker: 'user' | 'assistant', delta: string): void =>
  act(() => ws.serverSend({ type: 'caption', speaker, delta }));

const saved = (username = 'admin'): CallRecord[] =>
  JSON.parse(env.storage.get(`live.history:${username}`) ?? '[]') as CallRecord[];

/** A storage that counts the writes the history makes. */
function countWrites(): ReturnType<typeof vi.fn> {
  const setItem = vi.fn((key: string, value: string) => void env.storage.set(key, value));
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => env.storage.get(key) ?? null,
    setItem,
    removeItem: (key: string) => void env.storage.delete(key),
  });
  return setItem;
}

const historyWrites = (setItem: ReturnType<typeof vi.fn>): number =>
  setItem.mock.calls.filter(([key]) => key === 'live.history:admin').length;

function record(n: number, text = `call ${n}`): CallRecord {
  const startedAt = Date.UTC(2023, 10, 14, 12, n);
  return {
    id: String(startedAt),
    startedAt,
    endedAt: startedAt + 30_000,
    reason: 'hangup',
    entries: [
      { kind: 'turn', speaker: 'user', text },
      { kind: 'turn', speaker: 'assistant', text: `answer ${n}` },
    ],
  };
}

describe('the conversation while a call runs', () => {
  it('shows what both sides say, a turn at a time, as it is said', async () => {
    const ws = await startCall();
    const log = screen.getByRole('log', { name: en.conversation });
    expect(within(log).getByText(en.transcriptEmpty)).toBeInTheDocument();

    say(ws, 'user', 'What is ');
    say(ws, 'user', 'running?');
    say(ws, 'assistant', 'Two sessions: ');
    say(ws, 'assistant', 'Billing and Search.');
    say(ws, 'user', 'Thanks');

    expect(within(log).queryByText(en.transcriptEmpty)).not.toBeInTheDocument();
    const turns = [...log.querySelectorAll('.live-turn')].map((turn) => turn.textContent);
    expect(turns).toEqual([
      `${en.you}What is running?`,
      `${en.assistant}Two sessions: Billing and Search.`,
      `${en.you}Thanks`,
    ]);
  });

  it('keeps it when the panel is closed and opened again', async () => {
    const ws = await startCall();
    say(ws, 'user', 'Hello there');
    fireEvent.click(screen.getByRole('button', { name: en.close }));
    fireEvent.click(screen.getByRole('button', { name: en.openInCall }));
    expect(screen.getByText('Hello there')).toBeInTheDocument();
  });

  it('keeps the whole of a long conversation', async () => {
    const ws = await startCall();
    for (let i = 0; i < 60; i += 1) {
      say(ws, i % 2 === 0 ? 'user' : 'assistant', `line ${i}`);
    }
    const log = screen.getByRole('log');
    expect(log.querySelectorAll('.live-turn')).toHaveLength(60);
    expect(within(log).getByText('line 0')).toBeInTheDocument();
  });
});

describe('the conversation sticks to its end', () => {
  const CLIENT = 200;
  let scrollHeight = 1000;
  const original = {
    scrollHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight'),
    clientHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight'),
  };

  beforeAll(() => {
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { get: () => scrollHeight, configurable: true });
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { get: () => CLIENT, configurable: true });
  });

  afterAll(() => {
    for (const key of ['scrollHeight', 'clientHeight'] as const) {
      const was = original[key];
      if (was) Object.defineProperty(HTMLElement.prototype, key, was);
      else Reflect.deleteProperty(HTMLElement.prototype, key);
    }
  });

  it('follows the newest words until the person scrolls up, and again once they are back at the end', async () => {
    const ws = await startCall();
    const log = screen.getByRole('log');

    scrollHeight = 1000;
    say(ws, 'user', 'first');
    expect(log.scrollTop).toBe(1000);
    scrollHeight = 1200;
    say(ws, 'assistant', 'second');
    expect(log.scrollTop).toBe(1200);

    // Reading something above: new words do not pull the reader away.
    log.scrollTop = 100;
    fireEvent.scroll(log);
    scrollHeight = 1400;
    say(ws, 'user', 'third');
    expect(log.scrollTop).toBe(100);

    // Back at the end: following again.
    log.scrollTop = 1400 - CLIENT;
    fireEvent.scroll(log);
    scrollHeight = 1600;
    say(ws, 'assistant', 'fourth');
    expect(log.scrollTop).toBe(1600);
  });
});

describe('writing the call down while it runs', () => {
  it('saves within two seconds of the words, and not more often than every two seconds', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    const writes = countWrites();

    say(ws, 'user', 'What is running?');
    await act(async () => void (await vi.advanceTimersByTimeAsync(500)));
    say(ws, 'assistant', 'Two sessions.');
    await act(async () => void (await vi.advanceTimersByTimeAsync(500)));
    say(ws, 'user', 'Thanks');
    expect(historyWrites(writes)).toBe(0);

    await act(async () => void (await vi.advanceTimersByTimeAsync(1_100)));
    expect(historyWrites(writes)).toBe(1);
    expect(saved()[0]?.entries.map((entry) => (entry.kind === 'turn' ? entry.text : ''))).toEqual([
      'What is running?',
      'Two sessions.',
      'Thanks',
    ]);

    // Nothing new, nothing written; then new words are written within the next two seconds.
    await act(async () => void (await vi.advanceTimersByTimeAsync(5_000)));
    expect(historyWrites(writes)).toBe(1);
    say(ws, 'assistant', 'Anything else?');
    await act(async () => void (await vi.advanceTimersByTimeAsync(2_100)));
    expect(historyWrites(writes)).toBe(2);
    expect(saved()[0]?.entries).toHaveLength(4);
  });

  it('keeps what was said when the page dies without a word, and shows it as an interrupted call', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    say(ws, 'user', 'Check the build');
    say(ws, 'assistant', 'It is green.');
    await act(async () => void (await vi.advanceTimersByTimeAsync(2_100)));
    expect(saved()[0]).toMatchObject({ reason: null });

    // The page is gone without a word; a new one starts with nothing in memory, only the browser's copy.
    cleanup();
    useLive.getState().reset();
    forgetHistory();
    vi.useRealTimers();
    renderEntry();
    fireEvent.click(await screen.findByRole('button', { name: en.open }));
    expect(await screen.findByText('Check the build')).toBeInTheDocument();
    expect(screen.getByText('It is green.')).toBeInTheDocument();
    expect(screen.getByText(en.endedLost, { selector: '.live-record-note' })).toBeInTheDocument();
  });

  it('writes the whole call at once when it ends, with why', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    say(ws, 'user', 'Any news?');
    // Less than two seconds: the end does not wait for the next save.
    endCall(ws, 'idle');
    expect(saved()).toHaveLength(1);
    expect(saved()[0]).toMatchObject({ reason: 'idle' });
    expect(saved()[0]?.endedAt).toBeGreaterThanOrEqual(saved()[0]?.startedAt ?? Infinity);
    expect(saved()[0]?.entries).toEqual([{ kind: 'turn', speaker: 'user', text: 'Any news?' }]);
  });

  it('writes the call when the person hangs up', async () => {
    const ws = await startCall();
    say(ws, 'user', 'Bye');
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    endCall(ws, 'hangup');
    expect(saved()[0]).toMatchObject({ reason: 'hangup' });
  });

  it('writes a message sent by a spoken yes into the record, once', async () => {
    const ws = await startCall();
    const draft = { id: 'd1', session: 'Billing', text: 'Skip the migration tests' };
    act(() => {
      ws.serverSend({ type: 'draft', draft });
      ws.serverSend({ type: 'draft.result', draft_id: 'd1', ok: true, session: 'Billing' });
      ws.serverSend({ type: 'draft', draft: null });
    });
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    endCall(ws, 'hangup');
    expect(saved()[0]?.entries).toEqual([
      { kind: 'sent', session: 'Billing', text: 'Skip the migration tests' },
    ]);
  });

  it('writes the call even when the gateway never says goodbye and the 1.5 s farewell ends it', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    say(ws, 'user', 'Bye then');
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    expect(saved()).toEqual([]);
    await act(async () => void (await vi.advanceTimersByTimeAsync(1_600)));
    expect(ws.closedWith).toBe(1000);
    expect(saved()[0]).toMatchObject({ reason: 'hangup' });
    expect(saved()[0]?.entries).toEqual([{ kind: 'turn', speaker: 'user', text: 'Bye then' }]);
    // And it is what the idle panel shows as the last conversation.
    expect(screen.getByText(en.lastConversation)).toBeInTheDocument();
    expect(screen.getByText('Bye then')).toBeInTheDocument();
  });

  it('writes the call when the page is closed or reloaded', async () => {
    const ws = await startCall();
    say(ws, 'user', 'Still here');
    act(() => void window.dispatchEvent(new Event('pagehide')));
    expect(saved()[0]).toMatchObject({ reason: 'page_closed' });
  });

  it('keeps it under the account it belongs to when the account changes', async () => {
    const ws = await startCall();
    say(ws, 'user', 'Mine');
    act(() => useAuth.setState({ username: 'dana' }));
    expect(saved('admin')[0]?.entries).toHaveLength(1);
    expect(saved('dana')).toEqual([]);
    expect(env.storage.has('live.history:dana')).toBe(false);
  });

  it('leaves no record of a call nobody spoke in', async () => {
    const ws = await startCall();
    endCall(ws, 'idle');
    expect(env.storage.has('live.history:admin')).toBe(false);
  });

  it('is not disturbed when the browser cannot keep it: the call goes on and the record stays in memory', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new DOMException('full', 'QuotaExceededError');
      },
      removeItem: () => undefined,
    });
    say(ws, 'user', 'Remember this');
    await act(async () => void (await vi.advanceTimersByTimeAsync(2_100)));
    expect(screen.getByRole('button', { name: en.hangUp })).toBeInTheDocument();
    expect(screen.getByText('Remember this')).toBeInTheDocument();

    endCall(ws, 'hangup');
    // The panel still has the call, from memory.
    expect(screen.getByText(en.lastConversation)).toBeInTheDocument();
    expect(screen.getByText('Remember this')).toBeInTheDocument();
  });
});

describe('with no call on', () => {
  const keep = (username: string, ...records: CallRecord[]): void => {
    env.storage.set(`live.history:${username}`, JSON.stringify(records));
  };

  it('shows the latest conversation, and says the history is kept in this browser only', async () => {
    keep('admin', record(3), record(2), record(1));
    await openPanel();
    expect(await screen.findByText(en.lastConversation)).toBeInTheDocument();
    const log = screen.getByRole('log', { name: en.conversation });
    expect(within(log).getByText('call 3')).toBeInTheDocument();
    expect(within(log).getByText('answer 3')).toBeInTheDocument();
    expect(screen.getByText(en.keptHere)).toBeInTheDocument();
  });

  it('lists the calls with their date, time and length, and opens the one that is chosen', async () => {
    keep('admin', record(3), record(2), record(1));
    await openPanel();
    await screen.findByText(en.lastConversation);
    const list = screen.getByRole('list');
    const rows = within(list).getAllByRole('button');
    expect(rows).toHaveLength(3);
    for (const row of rows) expect(row.textContent).toMatch(/^Nov 1[45], .*\d:\d\d.* · 30s$/);
    expect(rows[0]).toHaveAttribute('aria-current', 'true');

    fireEvent.click(rows[2] as HTMLElement);
    expect(screen.getByText(en.earlierConversation)).toBeInTheDocument();
    const log = screen.getByRole('log', { name: en.conversation });
    expect(within(log).getByText('call 1')).toBeInTheDocument();
    expect(within(log).queryByText('call 3')).not.toBeInTheDocument();
    expect(within(list).getAllByRole('button')[2]).toHaveAttribute('aria-current', 'true');

    fireEvent.click(within(list).getAllByRole('button')[0] as HTMLElement);
    expect(screen.getByText(en.lastConversation)).toBeInTheDocument();
    expect(within(screen.getByRole('log')).getByText('call 3')).toBeInTheDocument();
  });

  it('shows no list for a single call, and nothing at all but the note for none', async () => {
    keep('admin', record(1));
    await openPanel();
    await screen.findByText(en.lastConversation);
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.clearHistory })).toBeInTheDocument();
  });

  it('has a quiet panel before the first call: the intro, Start, and where conversations are kept', async () => {
    await openPanel();
    expect(screen.getByText(en.intro)).toBeInTheDocument();
    expect(screen.getByText(en.keptHere)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: en.clearHistory })).not.toBeInTheDocument();
    expect(screen.queryByText(en.lastConversation)).not.toBeInTheDocument();
  });

  it('asks before it clears, and clears only on the confirmation', async () => {
    keep('admin', record(2), record(1));
    await openPanel();
    fireEvent.click(await screen.findByRole('button', { name: en.clearHistory }));
    expect(screen.getByText(en.clearAsk)).toBeInTheDocument();
    expect(env.storage.has('live.history:admin')).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: en.cancel }));
    expect(screen.queryByText(en.clearAsk)).not.toBeInTheDocument();
    expect(env.storage.has('live.history:admin')).toBe(true);
    expect(screen.getByText(en.lastConversation)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: en.clearHistory }));
    fireEvent.click(screen.getByRole('button', { name: en.clearConfirm }));
    expect(env.storage.has('live.history:admin')).toBe(false);
    expect(screen.queryByText(en.lastConversation)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: en.clearHistory })).not.toBeInTheDocument();
    expect(screen.getByText(en.keptHere)).toBeInTheDocument();
  });

  it('clears only the signed-in account\'s calls', async () => {
    keep('admin', record(1));
    keep('dana', record(2));
    await openPanel();
    fireEvent.click(await screen.findByRole('button', { name: en.clearHistory }));
    fireEvent.click(screen.getByRole('button', { name: en.clearConfirm }));
    expect(env.storage.has('live.history:admin')).toBe(false);
    expect(env.storage.has('live.history:dana')).toBe(true);
  });

  it('shows the signed-in account\'s calls and nobody else\'s', async () => {
    keep('dana', record(2, 'dana was here'));
    await openPanel();
    expect(screen.queryByText('dana was here')).not.toBeInTheDocument();
    expect(screen.queryByText(en.lastConversation)).not.toBeInTheDocument();

    act(() => useAuth.setState({ username: 'dana' }));
    expect(await screen.findByText('dana was here')).toBeInTheDocument();
    act(() => useAuth.setState({ username: 'admin' }));
    await waitFor(() => expect(screen.queryByText('dana was here')).not.toBeInTheDocument());
  });

  it('shows the call that just ended as the latest, and its note when it was cut short', async () => {
    const ws = await startCall();
    say(ws, 'user', 'Anything pending?');
    say(ws, 'assistant', 'One approval.');
    endCall(ws, 'idle');
    expect(screen.getByText(en.endedIdle, { selector: '.live-note' })).toBeInTheDocument();
    expect(screen.getByText(en.lastConversation)).toBeInTheDocument();
    const log = screen.getByRole('log');
    expect(within(log).getByText('Anything pending?')).toBeInTheDocument();
    expect(within(log).getByText('One approval.')).toBeInTheDocument();
    expect(screen.getByText(en.endedIdle, { selector: '.live-record-note' })).toBeInTheDocument();
  });

  it('does not take the app down for a stored call with a time no date can hold', async () => {
    const absurd = { ...record(2, 'absurd call'), startedAt: 1e20, endedAt: 1e20 + 1000 };
    keep('admin', absurd, record(1, 'sound call'));
    await openPanel();
    // The sound one is shown, and Clear history is there to reach.
    expect(await screen.findByText('sound call')).toBeInTheDocument();
    expect(screen.queryByText('absurd call')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: en.clearHistory }));
    fireEvent.click(screen.getByRole('button', { name: en.clearConfirm }));
    expect(env.storage.has('live.history:admin')).toBe(false);
  });

  it('draws a call held in memory with a time no date can hold as its length alone', async () => {
    // A browser that cannot be read keeps what the page holds, which nothing has checked.
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
      removeItem: () => undefined,
    });
    // Just past what a Date holds, where a difference of 5 s still is one.
    keepRecord('admin', { ...record(1, 'held call'), startedAt: 8.64e15 + 1000, endedAt: 8.64e15 + 6000 });
    await openPanel();
    expect(await screen.findByText('held call')).toBeInTheDocument();
    expect(document.querySelector('.live-record-when')?.textContent).toBe('5.0s');
  });

  it('shows no history during a call', async () => {
    keep('admin', record(2), record(1));
    await startCall();
    expect(screen.queryByText(en.lastConversation)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: en.clearHistory })).not.toBeInTheDocument();
    expect(screen.queryByText(en.keptHere)).not.toBeInTheDocument();
  });

  it('speaks Chinese when the interface language is Chinese', async () => {
    useSettings.setState({ language: 'zh-Hans' });
    const zh = liveTables['zh-Hans'];
    keep('admin', record(1));
    await openPanel(zh.open);
    expect(await screen.findByText(zh.lastConversation)).toBeInTheDocument();
    expect(screen.getByText(zh.keptHere)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: zh.clearHistory })).toBeInTheDocument();
  });
});
