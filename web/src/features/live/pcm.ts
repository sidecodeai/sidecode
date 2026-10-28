/**
 * Audio conversion for the `/ws/live` socket. Pure and unit-tested.
 *
 * Both directions are raw mono signed 16-bit little-endian PCM at 24 kHz. The microphone arrives
 * at whatever rate the browser's audio context runs at, so it is resampled and cut into frames of
 * 20 ms; assistant audio comes down in frames the player turns back into floats.
 */

export const SAMPLE_RATE = 24_000;
/** 20 ms of audio: what the gateway asks the browser to send. */
export const FRAME_SAMPLES = 480;

export function floatToPcm16(sample: number): number {
  const clamped = Math.max(-1, Math.min(1, sample));
  return Math.round(clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff);
}

/**
 * Streaming box-filter resampler: each output sample is the average of the input over the span it
 * covers, with fractional weight at the edges, so it does not alias and any rate pair works. Chunks
 * of any size may be pushed; the output is the same as if the input had come in one piece.
 */
export class Resampler {
  private pending = new Float32Array(0);
  /** Where the next output sample's span starts, in input samples from the start of `pending`. */
  private cursor = 0;
  private readonly step: number;

  constructor(inputRate: number, outputRate: number = SAMPLE_RATE) {
    if (!(inputRate > 0) || !(outputRate > 0)) throw new RangeError('sample rates must be positive');
    this.step = inputRate / outputRate;
  }

  push(input: Float32Array): Float32Array {
    const joined = new Float32Array(this.pending.length + input.length);
    joined.set(this.pending);
    joined.set(input, this.pending.length);
    const out: number[] = [];
    // The span [cursor, cursor + step) needs every input sample it touches to have arrived.
    while (this.cursor + this.step <= joined.length) {
      out.push(this.average(joined, this.cursor, this.cursor + this.step));
      this.cursor += this.step;
    }
    const consumed = Math.floor(this.cursor);
    this.pending = joined.slice(consumed);
    this.cursor -= consumed;
    return Float32Array.from(out);
  }

  private average(samples: Float32Array, from: number, to: number): number {
    let sum = 0;
    for (let i = Math.floor(from); i < Math.ceil(to); i += 1) {
      const overlap = Math.min(i + 1, to) - Math.max(i, from);
      sum += (samples[i] ?? 0) * overlap;
    }
    return sum / (to - from);
  }
}

/** Microphone blocks in, frames of 20 ms of PCM16LE at 24 kHz out. */
export class MicEncoder {
  private readonly resampler: Resampler;
  private queued: number[] = [];

  constructor(
    inputRate: number,
    private readonly frameSamples: number = FRAME_SAMPLES,
  ) {
    this.resampler = new Resampler(inputRate);
  }

  push(block: Float32Array): ArrayBuffer[] {
    for (const sample of this.resampler.push(block)) this.queued.push(sample);
    const frames: ArrayBuffer[] = [];
    while (this.queued.length >= this.frameSamples) {
      frames.push(encode(this.queued.splice(0, this.frameSamples)));
    }
    return frames;
  }
}

function encode(samples: number[]): ArrayBuffer {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  samples.forEach((sample, index) => view.setInt16(index * 2, floatToPcm16(sample), true));
  return buffer;
}

/** PCM16LE to floats in [-1, 1). A trailing odd byte, which no whole sample can use, is ignored. */
export function pcm16ToFloat32(pcm: ArrayBuffer): Float32Array<ArrayBuffer> {
  const view = new DataView(pcm);
  const out = new Float32Array(Math.floor(pcm.byteLength / 2));
  for (let i = 0; i < out.length; i += 1) out[i] = view.getInt16(i * 2, true) / 0x8000;
  return out;
}
