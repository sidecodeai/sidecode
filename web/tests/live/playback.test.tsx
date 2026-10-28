/**
 * What the page tells the gateway about the voice: `playback` right after ready, every 5 s at most
 * while something changed, once in front of the hang-up, never after `ended`, and never by way of
 * anything that renders. The player's own accounting is in `player.test.ts`.
 */
import { act, fireEvent, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useLive } from '../../src/features/live/store';
import type { FakeWebSocket } from './audioFakes';
import { afterLive, beforeLive, context, dial, en, endCall, openPanel, redial, startCall } from './harness';

const VOICE = 1000;
const START = { type: 'playback', underruns: 0, gap_ms: 0, restarts: 0, delay_ms: 400 };

beforeEach(() => {
  beforeLive();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(afterLive);

const reports = (ws: FakeWebSocket) => ws.commands().filter((command) => command.type === 'playback');
const hangUp = (): void => void fireEvent.click(screen.getByRole('button', { name: en.hangUp }));

/** Time passes with the gateway ticking, as it does every 3 s: a silent gateway would be hung up on. */
async function pass(ws: FakeWebSocket, ms: number): Promise<void> {
  for (let left = ms; left > 0; left -= 1_000) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(Math.min(1_000, left));
      ws.serverSend({ type: 'time', seconds_left: 1000 });
    });
  }
}

/** A frame of the assistant's voice arrives while the audio clock reads `at`. */
function voiceAt(ws: FakeWebSocket, at: number): void {
  context().currentTime = at;
  act(() => ws.serverAudio(1920, VOICE));
}

/**
 * One stall: a reply starts at 0, and the next frame comes at 0.6 s, after the queue ran dry at
 * 0.44 s. The silence heard was 0.16 s and the 0.6 s the stall deepened the target to: 760 ms.
 */
function firstStall(ws: FakeWebSocket): void {
  voiceAt(ws, 0);
  voiceAt(ws, 0.6);
}

const STALLED = { type: 'playback', underruns: 1, gap_ms: 760, restarts: 1, delay_ms: 600 };

describe('playback telemetry', () => {
  it('tells the gateway right after ready where the voice stands, after the captions and the focus', async () => {
    const ws = await startCall();
    expect(ws.commands().slice(0, 3)).toEqual([
      { type: 'captions', enabled: true },
      { type: 'focus', device_id: null, session_id: null },
      START,
    ]);
    expect(reports(ws)).toHaveLength(1);
  });

  it('is quiet while nothing changes', async () => {
    const ws = await startCall();
    await pass(ws, 20_000);
    expect(reports(ws)).toHaveLength(1);
  });

  it('reports a change within 5 s of the last report, and not before', async () => {
    const ws = await startCall();
    firstStall(ws);
    await pass(ws, 4_000);
    expect(reports(ws)).toHaveLength(1);
    await pass(ws, 1_100);
    expect(reports(ws)).toHaveLength(2);
    expect(reports(ws)[1]).toEqual(STALLED);
  });

  it('reports at most once every 5 s however much changes, with the totals so far', async () => {
    const ws = await startCall();
    firstStall(ws); // the queue ends at 1.24
    await pass(ws, 5_100);
    expect(reports(ws)).toHaveLength(2);

    voiceAt(ws, 2); // dry since 1.24: a second stall, the delay now 0.8 s; this frame ends at 2.84
    voiceAt(ws, 4); // dry since 2.84: a third, the delay now 1.0 s
    await pass(ws, 3_000);
    expect(reports(ws)).toHaveLength(2);
    await pass(ws, 2_000);
    expect(reports(ws)).toHaveLength(3);
    const third = reports(ws)[2] as Record<string, unknown>;
    expect(third).toMatchObject({ underruns: 3, restarts: 3, delay_ms: 1000 });
    for (const key of ['underruns', 'gap_ms', 'restarts', 'delay_ms']) {
      expect(Number.isInteger(third[key])).toBe(true);
    }

    // Nothing changed since: quiet again.
    await pass(ws, 15_000);
    expect(reports(ws)).toHaveLength(3);
  });

  it('says where the voice stands once, right before the hang-up', async () => {
    const ws = await startCall();
    firstStall(ws);
    hangUp();
    expect(ws.commands().at(-1)).toEqual({ type: 'hangup' });
    expect(ws.commands().at(-2)).toEqual(STALLED);
    expect(reports(ws)).toHaveLength(2);
  });

  it('says it in front of the hang-up even when nothing changed since the last report', async () => {
    const ws = await startCall();
    hangUp();
    expect(ws.commands().slice(-3)).toEqual([START, START, { type: 'hangup' }]);
  });

  it('says it in front of the hang-up when the page is closed, too', async () => {
    const ws = await startCall();
    firstStall(ws);
    act(() => void window.dispatchEvent(new Event('pagehide')));
    expect(ws.commands().slice(-2)).toEqual([STALLED, { type: 'hangup' }]);
  });

  it('says nothing of the voice for a call that never got as far as ready', async () => {
    await openPanel();
    const ws = await dial();
    ws.open();
    act(() => void window.dispatchEvent(new Event('pagehide')));
    expect(ws.commands()).toEqual([{ type: 'hangup' }]);
  });

  it('says nothing more while the gateway is waited on for its goodbye', async () => {
    const ws = await startCall();
    hangUp();
    const before = ws.commands().length;
    firstStall(ws);
    await pass(ws, 12_000);
    expect(reports(ws)).toHaveLength(2);
    expect(ws.commands()).toHaveLength(before);
  });

  it('never says it after the gateway has said the call ended', async () => {
    const ws = await startCall();
    firstStall(ws);
    act(() => ws.serverSend({ type: 'ended', reason: 'idle' }));
    await pass(ws, 12_000);
    expect(reports(ws)).toHaveLength(1);
    // Not even by a hang-up pressed in the moment before the socket closes.
    hangUp();
    expect(reports(ws)).toHaveLength(1);
  });

  it('starts every call from the first delay, whatever the last one ended at', async () => {
    const first = await startCall();
    firstStall(first);
    voiceAt(first, 2);
    hangUp();
    expect(reports(first).at(-1)).toMatchObject({ underruns: 2, delay_ms: 800 });
    endCall(first, 'hangup');

    const second = await redial();
    expect(reports(second)).toEqual([START]);
  });

  it('keeps the audio path off anything that renders', async () => {
    const ws = await startCall();
    const seen = vi.fn();
    const stop = useLive.subscribe(seen);
    // Outside `act` on purpose: a frame is scheduled the moment it arrives, with no render to wait for.
    ws.serverAudio(1920, VOICE);
    expect(context().sources).toHaveLength(1);
    for (let i = 0; i < 24; i += 1) ws.serverAudio(1920, VOICE);
    context().currentTime = 9;
    ws.serverAudio(1920, VOICE);
    stop();
    expect(context().sources).toHaveLength(26);
    expect(seen).not.toHaveBeenCalled();
  });
});
