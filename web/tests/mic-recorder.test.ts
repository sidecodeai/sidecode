/**
 * The dictation recorder where a browser has no AudioWorklet: a
 * ScriptProcessor in 2048-frame blocks, about 43 ms at 48 kHz, so the
 * waveform hears a level in every 80 ms window.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MicRecorder } from '../src/features/voice/recorder';

const blockSizes: number[] = [];

class NoWorkletAudioContext {
  sampleRate = 48_000;
  state = 'running';
  destination = {};
  resume = () => Promise.resolve();
  close = () => Promise.resolve();
  createMediaStreamSource = () => ({ connect: () => undefined, disconnect: () => undefined });
  createScriptProcessor = (size: number) => {
    blockSizes.push(size);
    return { connect: () => undefined, disconnect: () => undefined, onaudioprocess: null };
  };
  createGain = () => ({ gain: { value: 1 }, connect: () => undefined });
}

beforeEach(() => {
  blockSizes.length = 0;
  vi.stubGlobal('AudioContext', NoWorkletAudioContext);
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: async () => ({ getTracks: () => [] }) },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

describe('the recorder without an AudioWorklet', () => {
  it('takes the microphone in 2048-frame blocks', async () => {
    const recorder = new MicRecorder({
      onFrame: () => undefined,
      onLevel: () => undefined,
      onError: () => undefined,
    });

    expect(await recorder.start()).toBe(true);
    expect(blockSizes).toEqual([2048]);
    await recorder.stop();
  });
});
