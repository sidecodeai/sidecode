/**
 * The assistant's voice, and the only jitter buffer between the gateway's stream and the speaker.
 *
 * The Live API's audio is a continuous stream at exactly real time, digital silence between
 * replies, that stalls now and then and never catches up, so the gateway can build no lead and the
 * queue kept here is all the room there is. Each frame is one `AudioBufferSourceNode` scheduled on
 * the audio context's own clock, so nothing here waits on rendering.
 *
 * - A frame that finds the queue dry starts `target` ahead of now. The target is 0.4 s at the start
 *   of a call and grows by 0.2 s with each underrun, to 1.0 s; the frame that finds the stall
 *   already starts at the grown one.
 * - Silence can be stretched or dropped without being missed. A silent frame that finds the queue
 *   shallower than the target is moved out to the target (the pause it adds is silent), which
 *   restores the buffer after a stall; one that finds it deeper than the target by 0.3 s is
 *   dropped, which bounds the delay after a burst.
 * - Any other frame goes back to back at the end of what is queued.
 * - A flush silences everything not yet heard, for when the person talks over it.
 */
import { SAMPLE_RATE, pcm16ToFloat32 } from './pcm';

/** The target at the start of a call, in ms: how far ahead of now a frame that finds the queue dry plays. */
export const START_DELAY_MS = 400;
/** What each underrun adds to the target, and the most it can reach. */
export const DELAY_STEP_MS = 200;
export const MAX_DELAY_MS = 1_000;
/** A silent frame that finds the queue deeper than the target by this much is dropped. */
const TRIM_MARGIN_SECONDS = 0.3;
/** A dry start this soon after the last audio ended is a stall; after longer it is a pause between replies. */
const STALL_SECONDS = 2;
/** A frame whose mean |sample| is at most 32 of 32768 is digital silence. */
const SILENCE_LEVEL = 32 / 0x8000;

/** What the voice has been through in a call, cumulative; the gateway is told it as it stands. */
export interface PlaybackStats {
  /** Times the queue ran dry in the middle of a reply's stream. */
  underruns: number;
  /** The silence those stalls made, as it was heard, in ms. */
  gap_ms: number;
  /** Every start from a dry queue after the call's first, stalls and pauses alike. */
  restarts: number;
  /** The target now in force, in ms. */
  delay_ms: number;
}

function isSilence(samples: Float32Array): boolean {
  let sum = 0;
  for (const sample of samples) sum += Math.abs(sample);
  return sum <= SILENCE_LEVEL * samples.length;
}

export class PcmPlayer {
  private nextTime = 0;
  private readonly sources = new Set<AudioBufferSourceNode>();
  private delayMs = START_DELAY_MS;
  private underruns = 0;
  private gapMs = 0;
  private restarts = 0;
  /** Audio has been scheduled in this call: the next start from a dry queue is a restart. */
  private started = false;
  /** The queue was emptied on purpose: the next start from a dry queue is not a stall. */
  private flushed = false;

  constructor(private readonly context: AudioContext) {}

  /** Seconds of audio scheduled and not yet played. */
  get queuedSeconds(): number {
    return Math.max(0, this.nextTime - this.context.currentTime);
  }

  get stats(): PlaybackStats {
    return {
      underruns: this.underruns,
      gap_ms: Math.round(this.gapMs),
      restarts: this.restarts,
      delay_ms: this.delayMs,
    };
  }

  enqueue(pcm: ArrayBuffer): void {
    const samples = pcm16ToFloat32(pcm);
    if (samples.length === 0) return;
    const now = this.context.currentTime;
    const target = this.delayMs / 1000;
    const depth = this.nextTime - now;
    const silent = isSilence(samples);
    if (silent && depth > target + TRIM_MARGIN_SECONDS) return;

    let startAt = this.nextTime;
    if (depth <= 0) startAt = now + this.startFresh(now);
    else if (silent && depth < target) startAt = now + target;

    const buffer = this.context.createBuffer(1, samples.length, SAMPLE_RATE);
    buffer.copyToChannel(samples, 0);
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    source.start(startAt);
    this.nextTime = startAt + buffer.duration;
    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      source.disconnect();
    };
  }

  /**
   * A frame found the queue dry: count it. A stall deepens the target at once, and the frame that
   * found it starts at the deeper one, so stalls that come in a cluster meet the buffer already built.
   * Returns how far ahead of now the frame starts, in seconds.
   */
  private startFresh(now: number): number {
    if (this.started) {
      this.restarts += 1;
      const sinceEnd = now - this.nextTime;
      if (!this.flushed && sinceEnd < STALL_SECONDS) {
        this.delayMs = Math.min(MAX_DELAY_MS, this.delayMs + DELAY_STEP_MS);
        this.underruns += 1;
        // The silence heard: the dry time, and then the delay this frame starts at.
        this.gapMs += sinceEnd * 1000 + this.delayMs;
      }
    }
    this.started = true;
    this.flushed = false;
    return this.delayMs / 1000;
  }

  /** Stop what is playing and drop what is scheduled. The next audio starts fresh, and is not a stall. */
  flush(): void {
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        /* a source that already ended cannot be stopped again */
      }
      source.disconnect();
    }
    this.sources.clear();
    this.nextTime = 0;
    this.flushed = true;
  }
}
