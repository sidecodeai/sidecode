/**
 * The gateway is told which session's chat the person has on screen, while a call is live:
 * `focus {device_id, session_id}` on a session's page and the same with nulls anywhere else, right
 * after `ready` and again on every change, never twice in a row.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { Link, MemoryRouter, Route, Routes } from 'react-router';
import { LiveEntry } from '../../src/features/live/LiveEntry';
import { setFocus } from '../../src/features/live/liveCall';
import { FakeWebSocket, READY } from './audioFakes';
import { Host } from './Host';
import { afterLive, beforeLive, context, dial, en } from './harness';

beforeEach(() => {
  beforeLive();
});

afterEach(afterLive);

function App({ start = '/sessions' }: { start?: string }) {
  return (
    <MemoryRouter initialEntries={[start]}>
      <Host />
      <LiveEntry />
      <nav>
        <Link to="/sessions">list</Link>
        <Link to="/sessions/dev-mac/ses-a">session a</Link>
        <Link to="/sessions/dev-mac/ses-b">session b</Link>
        <Link to="/sessions/dev%20ci/a%2Fb">session with odd ids</Link>
        <Link to="/devices/dev-mac/terminal">terminal</Link>
      </nav>
      <Routes>
        <Route path="*" element={null} />
      </Routes>
    </MemoryRouter>
  );
}

const focusOf = (ws: FakeWebSocket) => ws.commands().filter((command) => command.type === 'focus');
const go = (name: string): void => {
  fireEvent.click(screen.getByRole('link', { name }));
};

async function connect(start?: string): Promise<FakeWebSocket> {
  render(<App {...(start ? { start } : {})} />);
  fireEvent.click(await screen.findByRole('button', { name: en.open }));
  const ws = await dial();
  ws.open();
  return ws;
}

async function goLive(ws: FakeWebSocket): Promise<void> {
  act(() => ws.serverSend(READY));
  await screen.findByRole('button', { name: en.hangUp });
  await waitFor(() => expect(context().worklets).toHaveLength(1));
}

describe('which session is on screen', () => {
  it('says nowhere in particular right after ready, from a page that is not a session', async () => {
    const ws = await connect();
    expect(focusOf(ws)).toEqual([]);
    await goLive(ws);
    expect(focusOf(ws)).toEqual([{ type: 'focus', device_id: null, session_id: null }]);
    // After the captions request, before anything else the page says.
    expect(ws.commands().slice(0, 2)).toEqual([
      { type: 'captions', enabled: true },
      { type: 'focus', device_id: null, session_id: null },
    ]);
  });

  it('says which session a call that starts on its page is looking at', async () => {
    const ws = await connect('/sessions/dev-mac/ses-a');
    await goLive(ws);
    expect(focusOf(ws)).toEqual([{ type: 'focus', device_id: 'dev-mac', session_id: 'ses-a' }]);
  });

  it('follows the person from page to page, once for each change', async () => {
    const ws = await connect();
    await goLive(ws);

    go('session a');
    expect(focusOf(ws).at(-1)).toEqual({ type: 'focus', device_id: 'dev-mac', session_id: 'ses-a' });
    go('session b');
    expect(focusOf(ws).at(-1)).toEqual({ type: 'focus', device_id: 'dev-mac', session_id: 'ses-b' });
    go('terminal');
    expect(focusOf(ws).at(-1)).toEqual({ type: 'focus', device_id: null, session_id: null });
    go('list');
    go('session a');
    expect(focusOf(ws).at(-1)).toEqual({ type: 'focus', device_id: 'dev-mac', session_id: 'ses-a' });

    expect(focusOf(ws).map((c) => c.session_id)).toEqual([null, 'ses-a', 'ses-b', null, 'ses-a']);
  });

  it('does not say it again when nothing changed', async () => {
    const ws = await connect('/sessions/dev-mac/ses-a');
    await goLive(ws);
    go('session a');
    act(() => ws.serverSend({ type: 'time', seconds_left: 1100 }));
    act(() => ws.serverSend({ type: 'state', speaker: 'user' }));
    go('session a');
    expect(focusOf(ws)).toHaveLength(1);
    // Two pages that are both "nowhere" are one value, too.
    go('list');
    go('terminal');
    expect(focusOf(ws).map((c) => c.session_id)).toEqual(['ses-a', null]);
  });

  it('does not repeat a focus it has just said, however often it is told', async () => {
    const ws = await connect();
    await goLive(ws);
    const ids = (): unknown[] => focusOf(ws).map((command) => command.session_id);

    act(() => {
      setFocus(null, null);
      setFocus(null, null);
    });
    expect(ids()).toEqual([null]);

    act(() => {
      setFocus('dev-mac', 'ses-a');
      setFocus('dev-mac', 'ses-a');
      setFocus('dev-mac', 'ses-a');
    });
    expect(ids()).toEqual([null, 'ses-a']);

    // A place named by one id only is nowhere, and nowhere is said once, however it is put.
    act(() => {
      setFocus('dev-mac', null);
      setFocus(null, 'ses-a');
      setFocus(null, null);
    });
    expect(ids()).toEqual([null, 'ses-a', null]);

    // Changes are each said, even when they go back to what was said before.
    act(() => {
      setFocus('dev-mac', 'ses-b');
      setFocus('dev-mac', 'ses-a');
    });
    expect(ids()).toEqual([null, 'ses-a', null, 'ses-b', 'ses-a']);
  });

  it('gives the ids as the gateway knows them, not as the address writes them', async () => {
    const ws = await connect();
    await goLive(ws);
    go('session with odd ids');
    expect(focusOf(ws).at(-1)).toEqual({ type: 'focus', device_id: 'dev ci', session_id: 'a/b' });
  });

  it('says nothing before ready, and nothing once the call is being hung up', async () => {
    const ws = await connect();
    go('session a');
    go('session b');
    expect(focusOf(ws)).toEqual([]);
    await goLive(ws);
    // What was on screen when the call became live, and no more.
    expect(focusOf(ws)).toEqual([{ type: 'focus', device_id: 'dev-mac', session_id: 'ses-b' }]);

    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    go('session a');
    go('list');
    expect(focusOf(ws)).toHaveLength(1);
  });

  it('starts every call from where the person is now', async () => {
    const first = await connect();
    await goLive(first);
    go('session a');
    act(() => {
      first.serverSend({ type: 'ended', reason: 'idle' });
      first.serverClose(1000);
    });
    go('session b');
    fireEvent.click(await screen.findByRole('button', { name: en.start }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    const second = FakeWebSocket.instances[1] as FakeWebSocket;
    second.open();
    act(() => second.serverSend(READY));
    await waitFor(() => expect(focusOf(second)).toHaveLength(1));
    expect(focusOf(second)).toEqual([{ type: 'focus', device_id: 'dev-mac', session_id: 'ses-b' }]);
  });
});
