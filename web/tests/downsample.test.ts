import { describe, expect, it } from 'vitest';
import {
  FRAME_SAMPLES,
  PcmChunker,
  TARGET_RATE,
  downsampleTo16k,
  floatToPcm16,
  peakLevel,
} from '../src/features/voice/downsample';

describe('voice downsampler', () => {
  it('clamps and scales floats into signed 16-bit range', () => {
    expect(floatToPcm16(0)).toBe(0);
    expect(floatToPcm16(1)).toBe(32_767);
    expect(floatToPcm16(-1)).toBe(-32_768);
    expect(floatToPcm16(4)).toBe(32_767);
    expect(floatToPcm16(-4)).toBe(-32_768);
  });

  it('passes 16 kHz input through untouched', () => {
    const input = new Float32Array([0.1, 0.2, 0.3]);
    expect(downsampleTo16k(input, TARGET_RATE)).toBe(input);
  });

  it('box-filters 48 kHz down to a third of the samples', () => {
    const input = new Float32Array(48_000);
    for (let i = 0; i < input.length; i += 1) input[i] = 1;
    const out = downsampleTo16k(input, 48_000);
    expect(out.length).toBe(16_000);
    expect(out[0]).toBeCloseTo(1, 6);
    expect(out.at(-1)).toBeCloseTo(1, 6);
  });

  it('averages each source window rather than picking one sample', () => {
    // 44.1 kHz alternating +1/-1 must average towards zero, not alias to +1.
    const input = new Float32Array(44_100);
    for (let i = 0; i < input.length; i += 1) input[i] = i % 2 === 0 ? 1 : -1;
    const out = downsampleTo16k(input, 44_100);
    expect(out.length).toBe(16_000);
    const peak = peakLevel(out);
    expect(peak).toBeLessThan(0.7);
  });

  it('refuses input below the target rate', () => {
    expect(() => downsampleTo16k(new Float32Array(10), 8_000)).toThrow(RangeError);
  });

  it('reports the peak level for the waveform', () => {
    expect(peakLevel(new Float32Array([0.1, -0.6, 0.3]))).toBeCloseTo(0.6, 6);
    expect(peakLevel(new Float32Array([]))).toBe(0);
    expect(peakLevel(new Float32Array([9]))).toBe(1);
  });
});

describe('PcmChunker', () => {
  it('emits fixed 120 ms frames of little-endian PCM16', () => {
    const chunker = new PcmChunker(TARGET_RATE);
    expect(chunker.push(new Float32Array(1_000))).toHaveLength(0);
    const frames = chunker.push(new Float32Array(FRAME_SAMPLES));
    expect(frames).toHaveLength(1);
    expect(frames[0]?.byteLength).toBe(FRAME_SAMPLES * 2);
  });

  it('writes samples little-endian', () => {
    const chunker = new PcmChunker(TARGET_RATE, 2);
    const frames = chunker.push(new Float32Array([1, -1]));
    const view = new DataView(frames[0] as ArrayBuffer);
    expect(view.getInt16(0, true)).toBe(32_767);
    expect(view.getInt16(2, true)).toBe(-32_768);
  });

  it('keeps the remainder until the next push', () => {
    const chunker = new PcmChunker(TARGET_RATE, 4);
    expect(chunker.push(new Float32Array(6))).toHaveLength(1);
    expect(chunker.push(new Float32Array(2))).toHaveLength(1);
    expect(chunker.flush()).toBeNull();
  });

  it('flushes a partial trailing frame exactly once', () => {
    const chunker = new PcmChunker(TARGET_RATE, 4);
    chunker.push(new Float32Array(3));
    const tail = chunker.flush();
    expect(tail?.byteLength).toBe(6);
    expect(chunker.flush()).toBeNull();
  });

  it('resamples on the way in so frames are always 16 kHz', () => {
    const chunker = new PcmChunker(48_000, 100);
    const frames = chunker.push(new Float32Array(30_000));
    // 30 000 samples at 48 kHz is 10 000 samples at 16 kHz -> 100 frames.
    expect(frames).toHaveLength(100);
  });
});
