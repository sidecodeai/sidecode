/**
 * A57 — the listening row's level model (`docs/DESIGN.md` § "The composer"):
 * one bar every 80 ms whose level is the RMS of the audio its
 * window held — every block's energy summed, not the loudest block's peak —
 * mapped from dBFS so −50 is a dot and −10 is full height, and released slowly
 * so a word's tail decays instead of dropping. The row starts full of dots.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  LevelSampler,
  RELEASE,
  SAMPLE_MS,
  levelOf,
  release,
} from '../src/features/voice/waveformSampler';

/** The amplitude of a level in dBFS, full scale 1. */
const dbfs = (db: number): number => 10 ** (db / 20);

/** A block of `samples` at a steady RMS of `db` dBFS: its energy, and its length. */
const block = (db: number, samples = 128): [number, number] => [
  samples * dbfs(db) ** 2,
  samples,
];

describe('the level mapping', () => {
  it('draws −50 dBFS and anything quieter as a dot', () => {
    expect(levelOf(dbfs(-50))).toBeCloseTo(0, 10);
    expect(levelOf(dbfs(-72))).toBe(0);
    expect(levelOf(0)).toBe(0);
  });

  it('draws −10 dBFS and anything louder at full height', () => {
    expect(levelOf(dbfs(-10))).toBeCloseTo(1, 10);
    expect(levelOf(dbfs(-3))).toBe(1);
    expect(levelOf(1)).toBe(1);
  });

  it('is linear in decibels between the two', () => {
    expect(levelOf(dbfs(-30))).toBeCloseTo(0.5, 10);
    expect(levelOf(dbfs(-40))).toBeCloseTo(0.25, 10);
    expect(levelOf(dbfs(-20))).toBeCloseTo(0.75, 10);
  });

  it('reads anything that is not a level as silence', () => {
    expect(levelOf(Number.NaN)).toBe(0);
    expect(levelOf(-0.2)).toBe(0);
  });
});

describe('the release', () => {
  it('shows a rise at once', () => {
    expect(release(0.9, 0.2)).toBe(0.9);
    expect(release(0.5, 0.5)).toBe(0.5);
  });

  it('lets a fall keep 55% of the bar before it, and no more than the fall needs', () => {
    expect(release(0, 0.8)).toBeCloseTo(0.8 * RELEASE, 10);
    expect(release(0.6, 0.8)).toBe(0.6);
  });
});

describe('the sampler', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** A sampler on the fake clock, `lag` ms behind which its own timer may fire. */
  function sampler(capacity = 64) {
    const clock = { lag: 0 };
    const levels = new LevelSampler({ capacity, now: () => Date.now() + clock.lag });
    const origin = Date.now();
    /** The bars of windows that closed since it started, past the dots it began with. */
    const recorded = () => levels.bars.filter((bar) => bar.at > origin);
    return { levels, clock, origin, recorded };
  }

  it('starts full of resting dots, as if silence had been recorded', () => {
    const { levels, origin, recorded } = sampler(64);
    levels.start();

    expect(levels.bars).toHaveLength(64);
    expect(levels.bars.every((bar) => bar.level === 0)).toBe(true);
    // One a slot, up to the moment it started: the newest sits at the trailing edge.
    expect(levels.bars.at(-1)?.at).toBe(origin);
    expect(levels.bars[0]?.at).toBe(origin - 63 * SAMPLE_MS);
    expect(recorded()).toEqual([]);
  });

  it("takes each window's level from the energy of all its audio", () => {
    const { levels, recorded } = sampler();
    levels.start();
    // Half the window at −20 dBFS and half silent is −23 dBFS over the window,
    // where the loudest block alone would have said −20.
    for (let i = 0; i < 10; i += 1) levels.push(...block(-20));
    for (let i = 0; i < 10; i += 1) levels.push(0, 128);

    vi.advanceTimersByTime(SAMPLE_MS - 1);
    expect(recorded()).toHaveLength(0);
    vi.advanceTimersByTime(1);

    expect(recorded()).toHaveLength(1);
    expect(recorded()[0]?.level).toBeCloseTo((-20 - 10 * Math.log10(2) + 50) / 40, 10);
  });

  it('weighs blocks by their length', () => {
    const { levels, recorded } = sampler();
    levels.start();
    levels.push(...block(-30, 3_840));
    levels.push(...block(-10, 0));
    vi.advanceTimersByTime(SAMPLE_MS);

    expect(recorded()[0]?.level).toBeCloseTo(0.5, 10);
  });

  it('keeps sampling through silence, which draws a row of dots', () => {
    const { levels, recorded } = sampler();
    levels.start();

    vi.advanceTimersByTime(SAMPLE_MS * 5);

    expect(recorded().map((bar) => bar.level)).toEqual([0, 0, 0, 0, 0]);
  });

  it('lets a word end over the windows after it rather than at once', () => {
    const { levels, recorded } = sampler();
    levels.start();
    levels.push(...block(-10));
    vi.advanceTimersByTime(SAMPLE_MS * 3);

    const shown = recorded().map((bar) => bar.level);
    expect(shown[0]).toBeCloseTo(1, 10);
    expect(shown[1]).toBeCloseTo(RELEASE, 10);
    expect(shown[2]).toBeCloseTo(RELEASE * RELEASE, 10);
  });

  it('starts every window fresh', () => {
    const { levels, recorded } = sampler();
    levels.start();
    levels.push(...block(-10));
    vi.advanceTimersByTime(SAMPLE_MS);
    levels.push(...block(-30));
    vi.advanceTimersByTime(SAMPLE_MS);

    // Half height after a full bar is a fall to 0.5, which the release lifts to 0.55.
    expect(recorded()[1]?.level).toBeCloseTo(RELEASE, 10);
  });

  it('stamps each bar on its 80 ms slot, however late its timer fires', () => {
    const { levels, clock, origin, recorded } = sampler();
    levels.start();

    clock.lag = 30;
    vi.advanceTimersByTime(SAMPLE_MS);
    expect(recorded()[0]?.at).toBe(origin + SAMPLE_MS);

    // A tick so late that the next slot is due takes it, and fills its own.
    clock.lag = 100;
    vi.advanceTimersByTime(SAMPLE_MS);
    expect(recorded().map((bar) => bar.at - origin)).toEqual([80, 160, 240]);

    // Back on time, the tick for the slot already taken adds nothing, and the
    // one after takes the next slot: the bars never run ahead of the clock.
    clock.lag = 0;
    vi.advanceTimersByTime(SAMPLE_MS);
    expect(recorded()).toHaveLength(3);
    vi.advanceTimersByTime(SAMPLE_MS);
    expect(recorded().map((bar) => bar.at - origin)).toEqual([80, 160, 240, 320]);
  });

  it('fills the slots a throttled timer skipped with the last bar decaying to dots', () => {
    const { levels, clock, origin, recorded } = sampler();
    levels.start();
    levels.push(...block(-10));
    vi.advanceTimersByTime(SAMPLE_MS);

    // A background tab: the next tick comes 400 ms late, four slots later.
    levels.push(...block(-30));
    clock.lag = 400;
    vi.advanceTimersByTime(SAMPLE_MS);

    expect(recorded().map((bar) => bar.at - origin)).toEqual([80, 160, 240, 320, 400, 480, 560]);
    const shown = recorded().map((bar) => bar.level);
    expect(shown.slice(0, 6)).toEqual(
      [1, RELEASE, RELEASE ** 2, RELEASE ** 3, RELEASE ** 4, RELEASE ** 5].map((level) =>
        expect.closeTo(level, 10),
      ),
    );
    // No bar repeats the one before it; the window's own level comes last.
    expect(shown[6]).toBeCloseTo(0.5, 10);
  });

  it('takes a tick that fires a little early for its own slot', () => {
    const { levels, clock, origin, recorded } = sampler();
    levels.start();
    clock.lag = -3;
    vi.advanceTimersByTime(SAMPLE_MS * 3);

    expect(recorded().map((bar) => bar.at - origin)).toEqual([80, 160, 240]);
  });

  it('keeps no more bars than a row can show, dropping the oldest', () => {
    const { levels, origin } = sampler(4);
    levels.start();
    vi.advanceTimersByTime(SAMPLE_MS * 6);

    expect(levels.bars.map((bar) => bar.at - origin)).toEqual([240, 320, 400, 480]);
  });

  it('takes no new bars once stopped, and keeps the ones it has', () => {
    const { levels, recorded } = sampler();
    levels.start();
    vi.advanceTimersByTime(SAMPLE_MS * 2);

    levels.stop();
    levels.push(...block(-10));
    vi.advanceTimersByTime(SAMPLE_MS * 4);

    expect(recorded()).toHaveLength(2);
    expect(vi.getTimerCount()).toBe(0);
  });
});
