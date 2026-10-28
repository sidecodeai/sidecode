/**
 * Shared set-up for the voice assistant's component tests: the audio and socket fakes, a gateway
 * that offers the feature, and an in-memory `localStorage`. The page-level watchers are the
 * production ones, installed by the real `useLiveHost` that `renderEntry` mounts beside the entry,
 * so nothing here stands in for the app's own wiring. The call is module state, so every test ends
 * by closing the page.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { expect, vi } from 'vitest';
import { forgetLiveStatus } from '../../src/features/live/api';
import { forgetHistory } from '../../src/features/live/history';
import { watchLifecycle } from '../../src/features/live/liveCall';
import { LiveEntry } from '../../src/features/live/LiveEntry';
import { useLive } from '../../src/features/live/store';
import { liveTables } from '../../src/features/live/strings';
import { useAuth } from '../../src/stores/auth';
import { useSettings } from '../../src/stores/settings';
import {
  FakeAudioContext,
  FakeWebSocket,
  type FakeWorkletNode,
  READY,
  installAudioFakes,
} from './audioFakes';
import { Host } from './Host';

export const en = liveTables.en;

export interface LiveEnv {
  fakes: ReturnType<typeof installAudioFakes>;
  /** This jsdom build has no `localStorage`: an in-memory one stands in. */
  storage: Map<string, string>;
}

export function status(body: unknown, init: ResponseInit = { status: 200 }): Response {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** The gateway answers each ask for the live status with the next of `bodies`, and the last one for ever. */
export function gatewaySays(...bodies: unknown[]) {
  let asked = 0;
  const fetcher = vi.fn(async () => status(bodies[Math.min(asked++, bodies.length - 1)]));
  vi.stubGlobal('fetch', fetcher);
  return fetcher;
}

/** The status of an account that is offered the assistant and can have a call. */
export const available = { enabled: true, available: true, reason: null };

/** The status of an account that is offered the assistant and cannot have it, for `reason`. */
export const cannot = (reason: string | null) => ({ enabled: true, available: false, reason });

export function beforeLive(): LiveEnv {
  forgetLiveStatus();
  forgetHistory();
  useLive.getState().reset();
  useAuth.setState({ username: 'admin' });
  useSettings.setState({ language: 'en' });
  const fakes = installAudioFakes();
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => status(available)),
  );
  const storage = new Map<string, string>();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => void storage.set(key, value),
    removeItem: (key: string) => void storage.delete(key),
  });
  return { fakes, storage };
}

/** The page closes: a call still on is hung up and let go, by the watcher the app installs. */
export function afterLive(): void {
  act(() => void window.dispatchEvent(new Event('pagehide')));
  cleanup();
  // A call a test left with nothing mounted (the watcher went with the screens) goes all the same.
  const stop = watchLifecycle();
  window.dispatchEvent(new Event('pagehide'));
  stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
}

/** The entry with the real host beside it: the watchers and the focus are the production ones. */
export function renderEntry() {
  return render(
    <MemoryRouter>
      <Host />
      <LiveEntry />
    </MemoryRouter>,
  );
}

export async function openPanel(name = en.open): Promise<void> {
  renderEntry();
  fireEvent.click(await screen.findByRole('button', { name }));
}

/** Open the panel of an account that cannot have a call, and let the gateway's second look at that land. */
export async function openUnavailable(): Promise<void> {
  await openPanel();
  await act(async () => undefined);
}

export async function dial(): Promise<FakeWebSocket> {
  fireEvent.click(screen.getByRole('button', { name: en.start }));
  await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
  return FakeWebSocket.instances[0] as FakeWebSocket;
}

export function context(): FakeAudioContext {
  return FakeAudioContext.instances[0] as FakeAudioContext;
}

export function worklet(): FakeWorkletNode {
  return context().worklets[0] as FakeWorkletNode;
}

/** The panel is open, the call is live and the microphone is being tapped. */
export async function startCall(): Promise<FakeWebSocket> {
  await openPanel();
  const ws = await dial();
  ws.open();
  act(() => ws.serverSend(READY));
  await screen.findByRole('button', { name: en.hangUp });
  await waitFor(() => expect(context().worklets).toHaveLength(1));
  return ws;
}

/** The panel offers Start again after a call: dial another, let it go live and tap the microphone. */
export async function redial(): Promise<FakeWebSocket> {
  const count = FakeWebSocket.instances.length;
  fireEvent.click(screen.getByRole('button', { name: en.start }));
  await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(count + 1));
  const ws = FakeWebSocket.instances[count] as FakeWebSocket;
  ws.open();
  act(() => ws.serverSend(READY));
  await screen.findByRole('button', { name: en.hangUp });
  return ws;
}

/** What the gateway says of a call it is ending, then the close. */
export function endCall(ws: FakeWebSocket, reason: string, code = 1000): void {
  act(() => {
    ws.serverSend({ type: 'ended', reason });
    ws.serverClose(code);
  });
}
