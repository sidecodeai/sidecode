/**
 * How a call ends and what it leaves: nothing that plays or sends once it is over, no timer behind
 * it whichever way it ended, the microphone let go before anything else is attempted, and a browser
 * that refuses to make the context or the socket told to the person with nothing left held.
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setFocus } from '../../src/features/live/liveCall';
import { SILENCE_TIMEOUT_MS } from '../../src/features/live/liveSocket';
import { CallRecorder } from '../../src/features/live/recorder';
import { useAuth } from '../../src/stores/auth';
import { FakeAudioContext, FakeWebSocket, type FakeWorkletNode } from './audioFakes';
import {
  afterLive,
  beforeLive,
  context,
  dial,
  en,
  endCall,
  type LiveEnv,
  openPanel,
  startCall,
  worklet,
} from './harness';

let env: LiveEnv;

beforeEach(() => {
  env = beforeLive();
});

afterEach(() => {
  vi.restoreAllMocks();
  afterLive();
});

const hangUp = (): void => void fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
const tap = (node: FakeWorkletNode): void => {
  for (let i = 0; i < 30; i += 1) node.post(new Float32Array(128).fill(0.1));
};

describe('a call that is over plays and sends nothing more', () => {
  it('plays nothing that comes after Hang up, while the gateway has not yet said goodbye', async () => {
    const ws = await startCall();
    act(() => ws.serverAudio(1920, 1000));
    expect(context().sources).toHaveLength(1);
    hangUp();
    expect(context().sources.every((source) => source.stopped)).toBe(true);
    act(() => ws.serverAudio(1920, 1000));
    act(() => ws.serverAudio(1920, 1000));
    expect(context().sources).toHaveLength(1);
  });

  it('sends nothing after Hang up: no microphone, no focus, no playback', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    hangUp();
    const commands = ws.commands().length;
    const frames = ws.frames().length;
    tap(worklet());
    act(() => setFocus('dev-mac', 'ses-a'));
    await act(async () => void (await vi.advanceTimersByTimeAsync(1_000)));
    expect(ws.commands()).toHaveLength(commands);
    expect(ws.frames()).toHaveLength(frames);
  });

  it('lets the microphone go the moment the gateway says ended, before the socket closes', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'ended', reason: 'idle' }));
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(worklet().disconnected).toBe(true);
    const frames = ws.frames().length;
    tap(worklet());
    expect(ws.frames()).toHaveLength(frames);
  });

  it('plays and sends nothing after ended: no voice, no focus, no playback', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'ended', reason: 'idle' }));
    const commands = ws.commands().length;
    act(() => ws.serverAudio(1920, 1000));
    act(() => setFocus('dev-mac', 'ses-a'));
    await act(async () => void (await vi.advanceTimersByTimeAsync(6_000)));
    expect(context().sources).toHaveLength(0);
    expect(ws.commands()).toHaveLength(commands);
  });

  it('plays nothing before the gateway has said ready', async () => {
    await openPanel();
    const ws = await dial();
    ws.open();
    act(() => ws.serverAudio(1920, 1000));
    expect(context().sources).toHaveLength(0);
  });
});

describe('every way a call ends leaves no timer behind', () => {
  /** A call with something pending in every place a timer can be: the silence watch, the reports, a save, a notice. */
  async function busyCall(): Promise<FakeWebSocket> {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'caption', speaker: 'user', delta: 'Something said' }));
    act(() => ws.serverSend({ type: 'notice', code: 'server_error' }));
    // The silence watch, the playback reports, the save of what was said, the notice's own expiry.
    expect(vi.getTimerCount()).toBeGreaterThanOrEqual(4);
    return ws;
  }

  const endings: [string, (ws: FakeWebSocket) => void | Promise<void>][] = [
    ['the person hangs up and the gateway says goodbye', (ws) => {
      hangUp();
      endCall(ws, 'hangup');
    }],
    ['the person hangs up and the gateway says nothing', async () => {
      hangUp();
      await act(async () => void (await vi.advanceTimersByTimeAsync(1_600)));
    }],
    ['the gateway ends it', (ws) => endCall(ws, 'idle')],
    ['the gateway drops the socket without a word', (ws) => void act(() => ws.serverClose(1006))],
    ['the gateway goes silent', async () => {
      await act(async () => void (await vi.advanceTimersByTimeAsync(SILENCE_TIMEOUT_MS + 100)));
    }],
    ['the page is closed', () => void act(() => void window.dispatchEvent(new Event('pagehide')))],
    ['the account changes', () => void act(() => useAuth.setState({ username: 'dana' }))],
    ['the person signs out', () => void act(() => useAuth.setState({ status: 'signed-out', username: null, role: null }))],
  ];

  it.each(endings)('when %s', async (_name, end) => {
    const ws = await busyCall();
    await end(ws);
    // The call is let go of, and nothing it started is still waiting to fire.
    expect(context().closed).toBe(true);
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('when the microphone cannot be tapped after ready', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await openPanel();
    const ws = await dial();
    context().audioWorklet.addModule.mockRejectedValue(new Error('404'));
    ws.open();
    act(() => ws.serverSend({ type: 'ready', sample_rate: 24_000, frame_ms: 20, seconds_left: 1200 }));
    expect(await screen.findByText(en.errorMicUnavailable)).toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('when the gateway refuses before ready', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await openPanel();
    const ws = await dial();
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    act(() => {
      ws.serverSend({ type: 'ended', reason: 'cooldown' });
      ws.serverClose(4429);
    });
    expect(vi.getTimerCount()).toBe(0);
  });

  it('when the person gives up while it is still connecting', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    await openPanel();
    const ws = await dial();
    ws.open();
    await act(async () => void (await vi.advanceTimersByTimeAsync(800)));
    hangUp();
    expect(ws.closedWith).toBe(1000);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('a refusal in the recording cannot keep the call from letting go', () => {
  it('lets the microphone, the voice and the context go before it writes the call down', async () => {
    const ws = await startCall();
    act(() => ws.serverSend({ type: 'caption', speaker: 'user', delta: 'Something said' }));
    vi.spyOn(CallRecorder.prototype, 'finish').mockImplementation(() => {
      throw new Error('the record cannot be written');
    });
    expect(() => ws.serverClose(1000)).toThrow('the record cannot be written');
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);
    expect(worklet().disconnected).toBe(true);
  });
});

describe('a browser that refuses', () => {
  it('words an audio context that cannot be made, and leaves nothing held or hanging', async () => {
    vi.stubGlobal(
      'AudioContext',
      class {
        constructor() {
          throw new DOMException('too many contexts', 'NotSupportedError');
        }
      },
    );
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    expect(await screen.findByText(en.errorGeneric)).toBeInTheDocument();
    expect(screen.queryByText(en.connecting)).not.toBeInTheDocument();
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(env.fakes.getUserMedia).not.toHaveBeenCalled();

    // Not stuck: a browser that can make one then starts a call.
    vi.stubGlobal('AudioContext', FakeAudioContext);
    const ws = await dial();
    expect(ws.closedWith).toBeNull();
  });

  it('words a socket that cannot be made, and lets the microphone and the context go', async () => {
    vi.stubGlobal('WebSocket', function Refusing() {
      throw new DOMException('blocked as mixed content', 'SecurityError');
    });
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: en.start }));
    expect(await screen.findByText(en.errorService)).toBeInTheDocument();
    expect(screen.queryByText(en.connecting)).not.toBeInTheDocument();
    expect(env.fakes.stream.track.stopped).toBe(true);
    expect(context().closed).toBe(true);

    // Not stuck: the call that could not be dialled is not still held, and the next one is.
    vi.stubGlobal('WebSocket', FakeWebSocket);
    const ws = await dial();
    expect(ws.closedWith).toBeNull();
    await waitFor(() => expect(FakeAudioContext.instances).toHaveLength(2));
  });
});
