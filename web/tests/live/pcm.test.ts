import { describe, expect, it } from 'vitest';
import {
  FRAME_SAMPLES,
  MicEncoder,
  Resampler,
  SAMPLE_RATE,
  floatToPcm16,
  pcm16ToFloat32,
} from '../../src/features/live/pcm';

function sine(rate: number, seconds: number, hz = 440, amplitude = 0.5): Float32Array {
  const out = new Float32Array(Math.round(rate * seconds));
  for (let i = 0; i < out.length; i += 1) out[i] = amplitude * Math.sin((2 * Math.PI * hz * i) / rate);
  return out;
}

function concat(parts: Float32Array[]): Float32Array {
  const out = new Float32Array(parts.reduce((n, part) => n + part.length, 0));
  let at = 0;
  for (const part of parts) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

function rms(samples: Float32Array): number {
  let sum = 0;
  for (const sample of samples) sum += sample * sample;
  return Math.sqrt(sum / samples.length);
}

describe('floatToPcm16', () => {
  it('maps the range onto signed 16 bits and clamps what is outside it', () => {
    expect(floatToPcm16(0)).toBe(0);
    expect(floatToPcm16(1)).toBe(0x7fff);
    expect(floatToPcm16(-1)).toBe(-0x8000);
    expect(floatToPcm16(2)).toBe(0x7fff);
    expect(floatToPcm16(-3)).toBe(-0x8000);
    expect(floatToPcm16(0.5)).toBe(Math.round(0.5 * 0x7fff));
  });
});

describe('Resampler', () => {
  it('halves 48 kHz by averaging pairs', () => {
    const out = new Resampler(48_000).push(Float32Array.from([1, 3, 5, 7, 9, 11]));
    expect(Array.from(out)).toEqual([2, 6, 10]);
  });

  it('passes audio already at the target rate through unchanged', () => {
    const input = Float32Array.from([0.1, -0.2, 0.3, -0.4]);
    expect(Array.from(new Resampler(SAMPLE_RATE).push(input))).toEqual(Array.from(input));
  });

  it('turns 44.1 kHz into 24 kHz in the right amount and keeps the loudness', () => {
    const input = sine(44_100, 1);
    const out = new Resampler(44_100).push(input);
    expect(Math.abs(out.length - 24_000)).toBeLessThanOrEqual(2);
    expect(rms(out)).toBeCloseTo(rms(input), 1);
  });

  it('gives the same output however the input is cut into chunks', () => {
    const input = sine(44_100, 0.5, 300);
    const whole = new Resampler(44_100).push(input);
    const pieces = new Resampler(44_100);
    const parts: Float32Array[] = [];
    for (let at = 0; at < input.length; ) {
      const size = 1 + ((at * 7) % 311);
      parts.push(pieces.push(input.slice(at, at + size)));
      at += size;
    }
    const joined = concat(parts);
    expect(joined.length).toBe(whole.length);
    for (let i = 0; i < whole.length; i += 1) expect(joined[i]).toBeCloseTo(whole[i] ?? 0, 5);
  });

  it('does not alias a tone above the target rate back into the band', () => {
    // 20 kHz is above half of 24 kHz: a box filter attenuates it a lot instead of folding it.
    const out = new Resampler(48_000).push(sine(48_000, 0.5, 20_000, 0.8));
    expect(rms(out)).toBeLessThan(0.25);
  });

  it('upsamples a lower rate by holding and averaging', () => {
    const out = new Resampler(16_000).push(sine(16_000, 0.1, 200));
    expect(Math.abs(out.length - 2_400)).toBeLessThanOrEqual(2);
  });

  it('refuses a rate that is not positive', () => {
    expect(() => new Resampler(0)).toThrow(RangeError);
  });
});

describe('MicEncoder', () => {
  it('cuts the microphone into frames of 20 ms of little-endian PCM16', () => {
    const encoder = new MicEncoder(48_000);
    const frames = encoder.push(sine(48_000, 0.1));
    expect(frames).toHaveLength(5);
    for (const frame of frames) expect(frame.byteLength).toBe(FRAME_SAMPLES * 2);
  });

  it('holds back a partial frame until the next block completes it', () => {
    const encoder = new MicEncoder(48_000);
    expect(encoder.push(new Float32Array(128))).toHaveLength(0);
    let frames = 0;
    for (let i = 0; i < 100; i += 1) frames += encoder.push(new Float32Array(128)).length;
    // 101 blocks of 128 samples at 48 kHz is 12 928 samples: 6 464 at 24 kHz, 13 whole frames.
    expect(frames).toBe(13);
  });

  it('encodes the samples it was given', () => {
    const encoder = new MicEncoder(SAMPLE_RATE);
    const input = new Float32Array(FRAME_SAMPLES).fill(0.5);
    const [frame] = encoder.push(input);
    const view = new DataView(frame as ArrayBuffer);
    expect(view.getInt16(0, true)).toBe(Math.round(0.5 * 0x7fff));
    expect(view.getInt16(FRAME_SAMPLES * 2 - 2, true)).toBe(Math.round(0.5 * 0x7fff));
  });

  it('loses nothing across frames', () => {
    const encoder = new MicEncoder(SAMPLE_RATE);
    const input = sine(SAMPLE_RATE, 0.2);
    const frames = [...encoder.push(input.slice(0, 1000)), ...encoder.push(input.slice(1000))];
    const decoded = concat(frames.map((frame) => pcm16ToFloat32(frame)));
    expect(decoded.length).toBe(Math.floor(input.length / FRAME_SAMPLES) * FRAME_SAMPLES);
    for (let i = 0; i < decoded.length; i += 97) expect(decoded[i]).toBeCloseTo(input[i] ?? 0, 3);
  });
});

describe('pcm16ToFloat32', () => {
  it('reads little-endian samples into the float range', () => {
    const buffer = new ArrayBuffer(8);
    const view = new DataView(buffer);
    view.setInt16(0, 0, true);
    view.setInt16(2, 0x4000, true);
    view.setInt16(4, -0x8000, true);
    view.setInt16(6, 0x7fff, true);
    const out = pcm16ToFloat32(buffer);
    expect(Array.from(out)).toEqual([0, 0.5, -1, 0x7fff / 0x8000]);
  });

  it('ignores a trailing odd byte', () => {
    expect(pcm16ToFloat32(new ArrayBuffer(5))).toHaveLength(2);
    expect(pcm16ToFloat32(new ArrayBuffer(1))).toHaveLength(0);
  });
});
