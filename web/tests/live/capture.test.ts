import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MIC_CONSTRAINTS, MicCapture, MicError, micError, openMicrophone } from '../../src/features/live/capture';
import { FakeAudioContext, FakeStream, type FakeWorkletNode, installAudioFakes } from './audioFakes';

beforeEach(() => {
  installAudioFakes();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the microphone', () => {
  it('asks the browser for echo cancellation, noise suppression and level control', () => {
    expect(MIC_CONSTRAINTS).toEqual({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
  });

  it('opens a stream with those constraints', async () => {
    const { getUserMedia, stream } = installAudioFakes();
    expect(await openMicrophone()).toBe(stream);
    expect(getUserMedia).toHaveBeenCalledWith(MIC_CONSTRAINTS);
  });

  it.each([
    ['NotAllowedError', 'denied'],
    ['SecurityError', 'denied'],
    ['NotFoundError', 'unsupported'],
    ['OverconstrainedError', 'unsupported'],
    ['AbortError', 'failed'],
  ])('names %s as %s', async (name, reason) => {
    const { getUserMedia } = installAudioFakes();
    getUserMedia.mockRejectedValue(new DOMException('no', name));
    await expect(openMicrophone()).rejects.toMatchObject({ reason });
    expect(micError(new DOMException('no', name)).reason).toBe(reason);
  });

  it('reports a browser with no microphone API', async () => {
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: undefined });
    await expect(openMicrophone()).rejects.toBeInstanceOf(MicError);
  });
});

describe('MicCapture', () => {
  async function started(frames: ArrayBuffer[] = []) {
    const context = new FakeAudioContext();
    const stream = new FakeStream();
    const capture = new MicCapture(context as unknown as AudioContext, stream as unknown as MediaStream, (f) =>
      frames.push(f),
    );
    await capture.start();
    return { context, stream, capture, frames, node: context.worklets[0] as FakeWorkletNode };
  }

  it('taps the microphone with the shared worklet', async () => {
    const { context, node } = await started();
    expect(context.audioWorklet.addModule).toHaveBeenCalledWith('/audio-worklet.js');
    expect(node.name).toBe('rc-capture');
  });

  it('turns the worklet blocks into frames of 20 ms at 24 kHz', async () => {
    const { node, frames } = await started();
    // 48 kHz context: 960 input samples are 20 ms.
    for (let i = 0; i < 15; i += 1) node.post(new Float32Array(128).fill(0.25));
    expect(frames).toHaveLength(2);
    for (const frame of frames) expect(frame.byteLength).toBe(960);
  });

  it('releases the microphone and stops delivering on stop', async () => {
    const { capture, stream, node, frames } = await started();
    capture.stop();
    expect(stream.track.stopped).toBe(true);
    expect(node.disconnected).toBe(true);
    expect(node.port.close).toHaveBeenCalled();
    node.post(new Float32Array(2000));
    expect(frames).toHaveLength(0);
  });

  it('says so when worklets are not supported', async () => {
    const context = new FakeAudioContext();
    (context as unknown as { audioWorklet: undefined }).audioWorklet = undefined;
    const capture = new MicCapture(context as unknown as AudioContext, new FakeStream() as unknown as MediaStream, () => {});
    await expect(capture.start()).rejects.toMatchObject({ reason: 'unsupported' });
  });

  it('says so when the worklet cannot be loaded', async () => {
    const context = new FakeAudioContext();
    context.audioWorklet.addModule.mockRejectedValue(new Error('404'));
    const capture = new MicCapture(context as unknown as AudioContext, new FakeStream() as unknown as MediaStream, () => {});
    await expect(capture.start()).rejects.toMatchObject({ reason: 'failed' });
  });
});
