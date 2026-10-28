/**
 * Mute replaces the microphone with digital silence and keeps the cadence: the same 20 ms frames
 * leave at the same pace, so the gateway's silent-browser watchdog does not end the call, and
 * nothing the person says reaches the model or the barge-in detector.
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { FakeAudioContext, FakeWebSocket, READY } from './audioFakes';
import { afterLive, beforeLive, dial, en, endCall, openPanel, startCall, worklet } from './harness';

beforeEach(() => {
  beforeLive();
});

afterEach(afterLive);

/** Thirty blocks of 128 samples of a steady tone: four frames of 20 ms once resampled. */
function speak(): ArrayBuffer[] {
  const before = FakeWebSocket.instances[0]?.frames().length ?? 0;
  for (let i = 0; i < 30; i += 1) worklet().post(new Float32Array(128).fill(0.1));
  return (FakeWebSocket.instances[0] as FakeWebSocket).frames().slice(before);
}

const samples = (frame: ArrayBuffer): Int16Array => new Int16Array(frame);
const isSilent = (frame: ArrayBuffer): boolean => samples(frame).every((sample) => sample === 0);

const liveCall = startCall;

describe('mute', () => {
  it('sends the same frames at the same cadence, all of digital silence, while muted', async () => {
    await liveCall();
    const open = speak();
    expect(open).toHaveLength(4);
    expect(open.every((frame) => !isSilent(frame))).toBe(true);

    fireEvent.click(screen.getByRole('button', { name: en.mute }));
    const muted = speak();
    // Same number of frames for the same stretch of microphone, each 20 ms long, each all zeros.
    expect(muted).toHaveLength(open.length);
    for (const frame of muted) {
      expect(frame.byteLength).toBe(960);
      expect(isSilent(frame)).toBe(true);
    }

    fireEvent.click(screen.getByRole('button', { name: en.unmute }));
    const again = speak();
    expect(again).toHaveLength(open.length);
    expect(again.every((frame) => !isSilent(frame))).toBe(true);
  });

  it('keeps the frames coming one for one over a long stretch, so the watchdog stays quiet', async () => {
    const ws = await liveCall();
    fireEvent.click(screen.getByRole('button', { name: en.mute }));
    const before = ws.frames().length;
    // Ten seconds of microphone, in the blocks the worklet posts: 128 samples at 48 kHz.
    const blocks = Math.round((10 * 48_000) / 128);
    for (let i = 0; i < blocks; i += 1) worklet().post(new Float32Array(128).fill(0.3));
    const sent = ws.frames().slice(before);
    expect(sent.length).toBe(500);
    expect(sent.every(isSilent)).toBe(true);
  });

  it('tells the gateway nothing: it is the same call, with a silent microphone', async () => {
    const ws = await liveCall();
    const before = ws.commands().length;
    fireEvent.click(screen.getByRole('button', { name: en.mute }));
    fireEvent.click(screen.getByRole('button', { name: en.unmute }));
    expect(ws.commands()).toHaveLength(before);
  });

  it('shows muted in the panel and in the indicator, and shows it gone on unmute', async () => {
    await liveCall();
    const entry = screen.getByRole('button', { name: en.openInCall });
    expect(entry).toHaveAttribute('data-muted', 'false');

    fireEvent.click(screen.getByRole('button', { name: en.mute }));
    expect(screen.getByText(en.muted)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.openInCallMuted })).toHaveAttribute('data-muted', 'true');
    expect(screen.queryByText(en.listening)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: en.unmute }));
    expect(screen.getByText(en.listening)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.openInCall })).toHaveAttribute('data-muted', 'false');
  });

  it('says so on the indicator with the panel closed', async () => {
    await liveCall();
    fireEvent.click(screen.getByRole('button', { name: en.mute }));
    fireEvent.click(screen.getByRole('button', { name: en.close }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: en.openInCallMuted })).toHaveAttribute('data-active', 'true');
  });

  it('still shows the assistant speaking while muted', async () => {
    const ws = await liveCall();
    fireEvent.click(screen.getByRole('button', { name: en.mute }));
    act(() => ws.serverSend({ type: 'state', speaker: 'assistant' }));
    expect(screen.getByText(en.speaking)).toBeInTheDocument();
    act(() => ws.serverSend({ type: 'state', speaker: 'none' }));
    expect(screen.getByText(en.muted)).toBeInTheDocument();
  });

  it('says checking while muted when the assistant is working, and muted again when it is not', async () => {
    const ws = await liveCall();
    fireEvent.click(screen.getByRole('button', { name: en.mute }));
    act(() => ws.serverSend({ type: 'working', on: true }));
    expect(screen.getByText(en.checking)).toBeInTheDocument();
    act(() => ws.serverSend({ type: 'working', on: false }));
    expect(screen.getByText(en.muted)).toBeInTheDocument();
  });

  it('is off at every call start', async () => {
    const first = await liveCall();
    fireEvent.click(screen.getByRole('button', { name: en.mute }));
    fireEvent.click(screen.getByRole('button', { name: en.hangUp }));
    endCall(first, 'hangup');

    fireEvent.click(screen.getByRole('button', { name: en.start }));
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    const second = FakeWebSocket.instances[1] as FakeWebSocket;
    second.open();
    act(() => second.serverSend(READY));
    // "Mute", not "Unmute": the new call starts with the microphone open.
    await screen.findByRole('button', { name: en.mute });
    await waitFor(() => expect(FakeAudioContext.instances[1]?.worklets).toHaveLength(1));
    const tap = FakeAudioContext.instances[1]?.worklets[0];
    for (let i = 0; i < 30; i += 1) tap?.post(new Float32Array(128).fill(0.1));
    expect(second.frames()).toHaveLength(4);
    expect(second.frames().every((frame) => !isSilent(frame))).toBe(true);
  });

  it('is not offered while the call is still connecting', async () => {
    await openPanel();
    await dial();
    expect(screen.getByRole('button', { name: en.mute })).toBeDisabled();
  });
});
