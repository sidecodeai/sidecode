/**
 * The call's wiring as the app makes it: through the real `App`, which calls `useLiveHost`, which
 * installs the watchers. Nothing here stands in for any of it, so taking the hook out of `App`, or
 * its effect out of the hook, fails these and nothing else has to.
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router';
import { App } from '../../src/App';
import { useAuth } from '../../src/stores/auth';
import { useConnection } from '../../src/stores/connection';
import { useDevices } from '../../src/stores/devices';
import { useSessions } from '../../src/stores/sessions';
import { devices } from '../../mock/fixtures';
import { FakeWebSocket, READY } from './audioFakes';
import { afterLive, available, beforeLive, context, en, type LiveEnv, status } from './harness';

let env: LiveEnv;
const connect = useConnection.getState().connect;

beforeEach(() => {
  env = beforeLive();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://gateway.test').pathname;
      if (path === '/api/live/status') return status(available);
      if (path === '/api/session') return status({ ok: true, user: { username: 'admin', role: 'admin' }, exp: 0 });
      return status({});
    }),
  );
  // The app's own socket is not what is under test: it would only be one more fake in the list.
  useConnection.setState({ connect: vi.fn() });
  useAuth.setState({ status: 'signed-in', username: 'admin', role: 'admin' });
  useSessions.setState({ loaded: true, sessions: {} });
  useDevices.setState({ devices: [devices[0]!], loaded: true });
});

afterEach(() => {
  afterLive();
  useConnection.setState({ connect });
});

/** A call started from the top bar of the real app, live and tapping the microphone. */
async function liveInApp(): Promise<FakeWebSocket> {
  render(
    <MemoryRouter initialEntries={['/sessions']}>
      <App />
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole('button', { name: en.open }));
  fireEvent.click(screen.getByRole('button', { name: en.start }));
  await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
  const ws = FakeWebSocket.instances[0] as FakeWebSocket;
  ws.open();
  act(() => ws.serverSend(READY));
  await screen.findByRole('button', { name: en.hangUp });
  await waitFor(() => expect(context().worklets).toHaveLength(1));
  return ws;
}

describe('the call in the real app', () => {
  it('is hung up when the page is closed or reloaded', async () => {
    const ws = await liveInApp();
    act(() => void window.dispatchEvent(new Event('pagehide')));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(ws.closedWith).toBe(1000);
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
  });

  it('is hung up when the person signs out', async () => {
    const ws = await liveInApp();
    act(() => useAuth.setState({ status: 'signed-out', username: null, role: null }));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(ws.closedWith).toBe(1000);
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
  });

  it('is hung up when a different account takes the page', async () => {
    const ws = await liveInApp();
    act(() => useAuth.setState({ username: 'dana' }));
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(ws.closedWith).toBe(1000);
  });

  it('goes on through the real app when nothing of the kind happens', async () => {
    const ws = await liveInApp();
    act(() => useAuth.setState({ status: 'signed-in', username: 'admin', role: 'admin' }));
    expect(ws.closedWith).toBeNull();
    expect(screen.getByRole('button', { name: en.hangUp })).toBeInTheDocument();
  });
});
