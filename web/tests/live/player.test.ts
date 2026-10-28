/**
 * The voice's jitter buffer, driven by a fake audio clock: where each frame is scheduled, what
 * counts as a stall, how the target grows, and which frames silence may stretch or drop.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { PcmPlayer } from '../../src/features/live/player';
import { FakeAudioContext } from './audioFakes';

/** 40 ms of 24 kHz PCM16 at a constant sample: one frame from the gateway. */
const frameOf = (level: number): ArrayBuffer => new Int16Array(960).fill(level).buffer;
const VOICE = frameOf(1000);
const SILENCE = frameOf(0);

let context: FakeAudioContext;
let player: PcmPlayer;

beforeEach(() => {
  context = new FakeAudioContext();
  player = new PcmPlayer(context as unknown as AudioContext);
});

const starts = (): number[] => context.sources.map((source) => source.startedAt ?? -1);

/** The queue at time `now` holds `frames` frames of voice, after a first one that started it. */
function queue(now: number, frames: number): void {
  context.currentTime = now;
  for (let i = 0; i < frames; i += 1) player.enqueue(VOICE);
}

describe('scheduling', () => {
  it('plays a frame as a mono 24 kHz buffer', () => {
    player.enqueue(VOICE);
    const [source] = context.sources;
    expect(source?.buffer?.numberOfChannels).toBe(1);
    expect(source?.buffer?.sampleRate).toBe(24_000);
    expect(source?.buffer?.length).toBe(960);
    expect(source?.buffer?.duration).toBeCloseTo(0.04);
  });

  it('schedules voice back to back with no gap and no overlap', () => {
    queue(0, 5);
    const at = starts();
    for (let i = 1; i < at.length; i += 1) {
      expect((at[i] ?? 0) - (at[i - 1] ?? 0)).toBeCloseTo(0.04, 6);
    }
    // 0.4 s of target in front of the first frame, then five frames of 40 ms.
    expect(player.queuedSeconds).toBeCloseTo(0.4 + 0.2, 6);
  });

  it('starts the target ahead of now when the queue has run dry', () => {
    context.currentTime = 10;
    player.enqueue(VOICE);
    expect(context.sources[0]?.startedAt).toBeCloseTo(10.4, 6);
    // Time passes beyond what was queued: the next frame starts afresh, not in the past.
    context.currentTime = 20;
    player.enqueue(VOICE);
    expect(context.sources[1]?.startedAt).toBeCloseTo(20.4, 6);
  });

  it('flush silences everything scheduled at once and starts fresh afterwards', () => {
    queue(0, 10);
    expect(player.queuedSeconds).toBeGreaterThan(0.3);
    player.flush();
    expect(context.sources.every((source) => source.stopped && source.disconnected)).toBe(true);
    expect(player.queuedSeconds).toBe(0);

    context.currentTime = 1;
    player.enqueue(VOICE);
    expect(context.sources[10]?.startedAt).toBeCloseTo(1.4, 6);
    expect(context.sources[10]?.stopped).toBe(false);
  });

  it('flush is safe when nothing plays, and when a source already ended', () => {
    player.flush();
    player.enqueue(VOICE);
    const [source] = context.sources;
    source?.finish();
    expect(() => player.flush()).not.toThrow();
  });

  it('forgets a source once it has played to its end', () => {
    player.enqueue(VOICE);
    const [source] = context.sources;
    source?.finish();
    expect(source?.disconnected).toBe(true);
    player.flush();
    expect(source?.stopped).toBe(false);
  });

  it('ignores an empty frame and the odd byte at the end of one', () => {
    player.enqueue(new ArrayBuffer(0));
    player.enqueue(new ArrayBuffer(1));
    expect(context.sources).toHaveLength(0);
    player.enqueue(new ArrayBuffer(3));
    expect(context.sources[0]?.buffer?.length).toBe(1);
  });

  it('reports how much audio is still to be heard', () => {
    expect(player.queuedSeconds).toBe(0);
    player.enqueue(VOICE);
    context.currentTime = 0.03;
    // Scheduled from 0.4 s to 0.44 s: 0.41 s of it is still ahead of 0.03 s.
    expect(player.queuedSeconds).toBeCloseTo(0.41, 6);
    context.currentTime = 5;
    expect(player.queuedSeconds).toBe(0);
  });
});

describe('what the voice has been through', () => {
  it('counts nothing for the first start of a call, and begins at a delay of 0.4 s', () => {
    expect(player.stats).toEqual({ underruns: 0, gap_ms: 0, restarts: 0, delay_ms: 400 });
    player.enqueue(VOICE);
    expect(player.stats).toEqual({ underruns: 0, gap_ms: 0, restarts: 0, delay_ms: 400 });
  });

  it('counts a stall, the silence it made, and grows the delay', () => {
    player.enqueue(VOICE); // scheduled 0.40 to 0.44
    context.currentTime = 0.6; // the queue ran dry at 0.44, 0.16 s ago
    player.enqueue(VOICE);
    // The silence heard: 0.16 s of nothing, then the 0.6 s the stall deepened the target to.
    expect(player.stats).toEqual({ underruns: 1, gap_ms: 760, restarts: 1, delay_ms: 600 });
    expect(context.sources[1]?.startedAt).toBeCloseTo(1.2, 6);
  });

  it('starts the frame that found a stall at the deeper target, so that buffer is in place at once', () => {
    player.enqueue(VOICE);
    context.currentTime = 0.6;
    player.enqueue(VOICE);
    // 0.6 s of target, and the frame itself.
    expect(player.queuedSeconds).toBeCloseTo(0.6 + 0.04, 6);
  });

  it('adds 0.2 s to the delay with each stall, to 1.0 s and no more', () => {
    let end = 0;
    const stall = (after: number): number => {
      context.currentTime = end + after;
      player.enqueue(VOICE);
      const delay = player.stats.delay_ms;
      // The frame that found the stall already starts at the deeper target.
      expect(context.sources.at(-1)?.startedAt).toBeCloseTo(context.currentTime + delay / 1000, 6);
      end = (context.sources.at(-1)?.startedAt ?? 0) + 0.04;
      return delay;
    };
    player.enqueue(VOICE);
    end = 0.44;
    expect([stall(0.1), stall(0.1), stall(0.1), stall(0.1), stall(0.1)]).toEqual([
      600, 800, 1000, 1000, 1000,
    ]);
    expect(player.stats.underruns).toBe(5);
    expect(player.stats.restarts).toBe(5);
  });

  it('sums the silence of every stall, each with the target it was started at', () => {
    player.enqueue(VOICE); // ends at 0.44
    context.currentTime = 0.54; // stall 1: 0.10 s dry, then 0.6 s: 700 ms heard; the frame ends at 1.18
    player.enqueue(VOICE);
    context.currentTime = 1.38; // stall 2: 0.20 s dry, then 0.8 s: 1000 ms heard
    player.enqueue(VOICE);
    expect(player.stats.gap_ms).toBe(1700);
    expect(player.stats.underruns).toBe(2);
  });

  it('counts a pause of two seconds or more as a restart, not a stall', () => {
    player.enqueue(VOICE); // ends at 0.44
    context.currentTime = 3;
    player.enqueue(VOICE);
    expect(player.stats).toEqual({ underruns: 0, gap_ms: 0, restarts: 1, delay_ms: 400 });
    context.currentTime = 3.44 + 1.9; // a stall of 1.9 s is still a stall
    player.enqueue(VOICE);
    expect(player.stats.underruns).toBe(1);
  });

  it('does not count the first start after a flush as a stall, and counts the ones after it again', () => {
    player.enqueue(VOICE);
    player.flush();
    context.currentTime = 0.1;
    player.enqueue(VOICE); // 0.5 to 0.54
    expect(player.stats).toEqual({ underruns: 0, gap_ms: 0, restarts: 1, delay_ms: 400 });
    context.currentTime = 1;
    player.enqueue(VOICE);
    expect(player.stats).toMatchObject({ underruns: 1, restarts: 2, delay_ms: 600 });
  });

  it('flushing before any audio has played changes nothing', () => {
    player.flush();
    player.enqueue(VOICE);
    expect(player.stats).toEqual({ underruns: 0, gap_ms: 0, restarts: 0, delay_ms: 400 });
  });

  it('keeps its counters for the whole call, and a new call starts from nothing', () => {
    player.enqueue(VOICE);
    context.currentTime = 0.6;
    player.enqueue(VOICE);
    player.flush();
    expect(player.stats).toMatchObject({ underruns: 1, restarts: 1, delay_ms: 600 });
    const next = new PcmPlayer(context as unknown as AudioContext);
    expect(next.stats).toEqual({ underruns: 0, gap_ms: 0, restarts: 0, delay_ms: 400 });
  });

  it('reports whole numbers', () => {
    player.enqueue(VOICE);
    context.currentTime = 0.44 + 0.1234;
    player.enqueue(VOICE);
    for (const value of Object.values(player.stats)) {
      expect(Number.isInteger(value)).toBe(true);
      expect(value).toBeGreaterThanOrEqual(0);
    }
    // 123.4 ms of nothing, then the 0.6 s the stall deepened the target to.
    expect(player.stats.gap_ms).toBe(723);
  });
});

describe('silence is stretched and dropped, voice never', () => {
  it('moves a silent frame that finds the queue shallower than the target out to the target', () => {
    queue(0, 1); // ends at 0.44
    context.currentTime = 0.1; // 0.34 s queued, less than 0.4 s
    player.enqueue(SILENCE);
    expect(context.sources[1]?.startedAt).toBeCloseTo(0.5, 6);
    // The queue is deep again: 0.5 + 0.04 - 0.1.
    expect(player.queuedSeconds).toBeCloseTo(0.44, 6);
    // Nothing ran dry, so nothing is counted.
    expect(player.stats).toEqual({ underruns: 0, gap_ms: 0, restarts: 0, delay_ms: 400 });
  });

  it('plays a voice frame that finds the queue shallow back to back, never later', () => {
    queue(0, 1);
    context.currentTime = 0.1;
    player.enqueue(VOICE);
    expect(context.sources[1]?.startedAt).toBeCloseTo(0.44, 6);
  });

  it('starts a silent frame that finds the queue dry at the target, as any dry start is', () => {
    queue(0, 1);
    context.currentTime = 0.6;
    player.enqueue(SILENCE);
    expect(context.sources[1]?.startedAt).toBeCloseTo(1.2, 6);
    expect(player.stats).toMatchObject({ underruns: 1, restarts: 1, delay_ms: 600 });
  });

  it('puts a silent frame at the end of the queue when the queue is at the target or deeper', () => {
    queue(0, 3); // 0.40 + 3 x 0.04: ends at 0.52, 0.52 s deep
    player.enqueue(SILENCE);
    expect(context.sources[3]?.startedAt).toBeCloseTo(0.52, 6);
  });

  it('drops a silent frame that finds the queue deeper than the target by more than 0.3 s', () => {
    queue(0, 7); // ends at 0.68: 0.68 s deep, within the target and 0.3 s (0.7 s)
    player.enqueue(SILENCE);
    expect(context.sources).toHaveLength(8);
    player.enqueue(VOICE); // the silence ended at 0.72, this ends at 0.76: deeper than 0.7 s
    expect(context.sources).toHaveLength(9);
    const queued = player.queuedSeconds;
    player.enqueue(SILENCE);
    expect(context.sources).toHaveLength(9);
    expect(player.queuedSeconds).toBe(queued);
    expect(player.stats).toEqual({ underruns: 0, gap_ms: 0, restarts: 0, delay_ms: 400 });
  });

  it('holds the delay after a burst: the silence in it goes, the voice stays', () => {
    queue(0, 12); // a burst after a stall: 0.88 s deep
    for (let i = 0; i < 5; i += 1) player.enqueue(SILENCE);
    expect(context.sources).toHaveLength(12);
    for (let i = 0; i < 5; i += 1) player.enqueue(VOICE);
    expect(context.sources).toHaveLength(17);
  });

  it('never drops or moves voice, however deep the queue', () => {
    queue(0, 40);
    expect(context.sources).toHaveLength(40);
    const at = starts();
    for (let i = 1; i < at.length; i += 1) {
      expect((at[i] ?? 0) - (at[i - 1] ?? 0)).toBeCloseTo(0.04, 6);
    }
  });

  // The queue is shallower than the target, which is where silence and voice part ways: silence
  // is moved out to the target, and voice plays on at the end of what is queued.
  it.each([
    ['32 of 32768 is silent, and moves out to the target', 32, 0.5],
    ['33 is not silent, and plays back to back', 33, 0.44],
  ])('judges silence by the mean |sample|: %s', (_name, level, startedAt) => {
    queue(0, 1); // ends at 0.44
    context.currentTime = 0.1; // 0.34 s queued, under the target
    player.enqueue(frameOf(level));
    expect(context.sources[1]?.startedAt).toBeCloseTo(startedAt, 6);
  });

  it.each([
    ['swinging between +32 and -32 is silent', 32, 0.5],
    ['swinging between +33 and -33 is not', 33, 0.44],
  ])('judges a frame that swings both ways by its mean |sample|: %s', (_name, level, startedAt) => {
    queue(0, 1);
    context.currentTime = 0.1;
    player.enqueue(new Int16Array(960).map((_, i) => (i % 2 === 0 ? level : -level)).buffer);
    expect(context.sources[1]?.startedAt).toBeCloseTo(startedAt, 6);
  });
});
