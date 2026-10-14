/**
 * Audio conversion for the STT socket: any-rate mono Float32 in, 16 kHz
 * PCM16LE out, batched into fixed-size frames. Pure and unit-tested.
 */

export const TARGET_RATE = 16_000;
/** ~120 ms per frame at 16 kHz, inside the 100–200 ms the contract recommends. */
export const FRAME_SAMPLES = 1920;

export function floatToPcm16(sample: number): number {
  const clamped = Math.max(-1, Math.min(1, sample));
  return Math.round(clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff);
}

/** Box-filter resampler: averages each source window, so it does not alias. */
export function downsampleTo16k(input: Float32Array, inputRate: number): Float32Array {
  if (inputRate === TARGET_RATE || input.length === 0) return input;
  if (inputRate < TARGET_RATE) throw new RangeError('input rate below 16 kHz');
  const ratio = inputRate / TARGET_RATE;
  const outLength = Math.floor(input.length / ratio);
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    let count = 0;
    for (let j = start; j < end; j += 1) {
      sum += input[j] ?? 0;
      count += 1;
    }
    out[i] = count > 0 ? sum / count : 0;
  }
  return out;
}

/** Peak level of a block, used to drive the waveform bars. */
export function peakLevel(input: Float32Array): number {
  let peak = 0;
  for (let i = 0; i < input.length; i += 1) {
    const v = Math.abs(input[i] ?? 0);
    if (v > peak) peak = v;
  }
  return Math.min(1, peak);
}

/**
 * Accumulates resampled audio and emits fixed-size little-endian PCM16 frames.
 */
export class PcmChunker {
  private buffer: number[] = [];

  constructor(
    private readonly inputRate: number,
    private readonly frameSamples: number = FRAME_SAMPLES,
  ) {}

  push(block: Float32Array): ArrayBuffer[] {
    const resampled = downsampleTo16k(block, this.inputRate);
    for (let i = 0; i < resampled.length; i += 1) this.buffer.push(resampled[i] ?? 0);
    const frames: ArrayBuffer[] = [];
    while (this.buffer.length >= this.frameSamples) {
      const slice = this.buffer.splice(0, this.frameSamples);
      frames.push(encodeFrame(slice));
    }
    return frames;
  }

  /** Remaining audio, zero-padded to nothing (partial frames are allowed). */
  flush(): ArrayBuffer | null {
    if (this.buffer.length === 0) return null;
    const slice = this.buffer.splice(0, this.buffer.length);
    return encodeFrame(slice);
  }
}

function encodeFrame(samples: number[]): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < samples.length; i += 1) {
    view.setInt16(i * 2, floatToPcm16(samples[i] ?? 0), true);
  }
  return buffer;
}
