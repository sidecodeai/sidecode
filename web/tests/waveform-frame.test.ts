/**
 * A57 — where the listening row draws each bar at one instant
 * (`docs/DESIGN.md` § "The composer"): a continuous sub-pixel glide
 * at 75 px/s, bars growing in over 120 ms to a height that follows level^1.5,
 * faded at both edges; after Done a glide that decelerates to a stop, a
 * highlight that sweeps the rested bars at a constant speed, and a clean cut
 * where the label begins; and, under Reduce Motion, bars that step one slot
 * per window.
 */
import { describe, expect, it } from 'vitest';
import {
  BAR_WIDTH,
  COVER_MS,
  FADE_LEFT,
  FADE_RIGHT,
  GROW_MS,
  HEIGHT_CURVE,
  MAX_SHARE,
  MIN_HEIGHT,
  PITCH,
  REST_ALPHA,
  REST_MS,
  SPEED,
  SWEEP_MS,
  SWEEP_SPEED,
  SWEEP_WIDTH,
  glideClock,
  paintBars,
  sweepAlpha,
  sweepPeriod,
  sweepX,
  type FrameMotion,
} from '../src/features/voice/waveformFrame';
import { SAMPLE_MS, type Bar } from '../src/features/voice/waveformSampler';

const WIDTH = 300;
const HEIGHT = 34;
/** Where a bar sits once it has emerged from the trailing fade. */
const SETTLED = WIDTH - FADE_RIGHT - BAR_WIDTH / 2;

interface Drawn {
  x: number;
  height: number;
  alpha: number;
}

function frame(bars: Bar[], motion: Partial<FrameMotion> & { now: number }): Drawn[] {
  const drawn: Drawn[] = [];
  paintBars(
    bars,
    { width: WIDTH, height: HEIGHT },
    { restAt: null, cover: 0, reduced: false, ...motion },
    (x, height, alpha) => drawn.push({ x, height, alpha }),
  );
  return drawn;
}

/** One bar a window, the newest born at `last`. */
const row = (count: number, last: number, level = 1): Bar[] =>
  Array.from({ length: count }, (_, index) => ({
    level,
    at: last - (count - 1 - index) * SAMPLE_MS,
  }));

describe('the glide', () => {
  it('moves the row 75 px a second, in sub-pixel steps', () => {
    const bars = [{ level: 1, at: 0 }];
    const before = frame(bars, { now: 500 })[0]!.x;
    const after = frame(bars, { now: 516 })[0]!.x;

    expect(SPEED * 1000).toBeCloseTo(75, 10);
    expect(before - after).toBeCloseTo(16 * SPEED, 10);
    expect((before - after) % 1).not.toBe(0);
  });

  it('brings a new bar in at the trailing edge, through the fade', () => {
    const bars = [{ level: 1, at: 1000 }];
    const born = frame(bars, { now: 1000 })[0]!;
    expect(born.x).toBeCloseTo(SETTLED + PITCH, 10);
    expect(born.alpha).toBeGreaterThan(0);
    expect(born.alpha).toBeLessThan(1);

    const emerged = frame(bars, { now: 1000 + SAMPLE_MS })[0]!;
    expect(emerged.x).toBeCloseTo(SETTLED, 10);
    expect(emerged.alpha).toBe(1);
  });

  it('keeps the bars one pitch apart, the newest first', () => {
    const xs = frame(row(4, 4000), { now: 4000 + SAMPLE_MS }).map((bar) => bar.x);
    expect(xs[0]! - xs[1]!).toBeCloseTo(PITCH, 10);
    expect(xs[1]! - xs[2]!).toBeCloseTo(PITCH, 10);
    expect(xs[2]! - xs[3]!).toBeCloseTo(PITCH, 10);
  });

  it('dissolves old bars over the leading 24 px and stops at the edge', () => {
    // Bars back to well past the leading edge.
    const drawn = frame(row(80, 10_000), { now: 10_000 + SAMPLE_MS });
    const halfway = drawn.find((bar) => Math.abs(bar.x - FADE_LEFT / 2) < PITCH / 2)!;
    expect(halfway.alpha).toBeCloseTo(halfway.x / FADE_LEFT, 10);
    expect(Math.min(...drawn.map((bar) => bar.x))).toBeGreaterThan(0);
    expect(drawn.length).toBeLessThan(80);
  });
});

describe("a bar's height", () => {
  const tallest = HEIGHT * MAX_SHARE;

  it('grows from a dot to its level in 120 ms, eased out', () => {
    const bars = [{ level: 1, at: 0 }];
    expect(frame(bars, { now: 0 })[0]!.height).toBeCloseTo(MIN_HEIGHT, 10);
    // Past halfway at half the time: it decelerates into place.
    expect(frame(bars, { now: GROW_MS / 2 })[0]!.height).toBeGreaterThan(
      (MIN_HEIGHT + tallest) / 2,
    );
    expect(frame(bars, { now: GROW_MS })[0]!.height).toBeCloseTo(tallest, 10);
    expect(frame(bars, { now: GROW_MS * 3 })[0]!.height).toBeCloseTo(tallest, 10);
  });

  it('is never taller than 60% of the row, and silence is a dot', () => {
    const loud = frame([{ level: 1, at: 0 }], { now: 1000 })[0]!;
    const quiet = frame([{ level: 0, at: 0 }], { now: 1000 })[0]!;
    expect(loud.height).toBeCloseTo(tallest, 10);
    expect(quiet.height).toBe(MIN_HEIGHT);
  });

  it('follows level^1.5, so ordinary speech spreads and only loud syllables reach the top', () => {
    const height = (level: number) => frame([{ level, at: 0 }], { now: 1000 })[0]!.height;
    expect(HEIGHT_CURVE).toBe(1.5);
    expect(height(0.5)).toBeCloseTo(MIN_HEIGHT + (tallest - MIN_HEIGHT) * 0.5 ** 1.5, 10);
    expect(height(0.25)).toBeCloseTo(MIN_HEIGHT + (tallest - MIN_HEIGHT) * 0.125, 10);
    // A three-quarter level stands well short of the cap.
    expect(height(0.75)).toBeLessThan(MIN_HEIGHT + (tallest - MIN_HEIGHT) * 0.7);
  });
});

describe('resting after Done', () => {
  it('slows the glide to a stop over 200 ms instead of freezing it', () => {
    expect(glideClock(1000, null)).toBe(1000);
    expect(glideClock(1000, 1000)).toBe(1000);
    // Halfway, the clock has run three quarters of the way to where it stops.
    expect(glideClock(1000 + REST_MS / 2, 1000)).toBeCloseTo(1000 + (REST_MS * 3) / 8, 10);
    expect(glideClock(1000 + REST_MS, 1000)).toBe(1000 + REST_MS / 2);
    expect(glideClock(9000, 1000)).toBe(1000 + REST_MS / 2);

    const bars = [{ level: 1, at: 0 }];
    const x = (now: number) => frame(bars, { now, restAt: 1000 })[0]!.x;
    expect(x(1000) - x(1000 + REST_MS)).toBeCloseTo((REST_MS / 2) * SPEED, 10);
    expect(x(1000 + REST_MS)).toBe(x(5000));
  });

  it('dims the rested bars to 0.35 under a highlight 36 px wide', () => {
    expect(sweepAlpha(120, 120)).toBeCloseTo(1, 10);
    expect(sweepAlpha(120 + SWEEP_WIDTH, 120)).toBeCloseTo(
      REST_ALPHA + (1 - REST_ALPHA) * Math.exp(-1),
      10,
    );
    expect(sweepAlpha(900, 120)).toBeCloseTo(REST_ALPHA, 10);
  });

  it('sweeps from −72 to the width + 72 at a constant speed, and starts over at once', () => {
    // A narrow row takes 1.4 s a pass; a wide one goes no faster than 480 px/s.
    expect(sweepPeriod(WIDTH)).toBe(SWEEP_MS);
    expect(sweepPeriod(1100)).toBeCloseTo(((1100 + 144) / SWEEP_SPEED) * 1000, 10);

    const period = sweepPeriod(WIDTH);
    expect(sweepX(0, WIDTH)).toBe(-2 * SWEEP_WIDTH);
    // Linear: a quarter of the time is a quarter of the way, with no easing.
    expect(sweepX(period / 4, WIDTH)).toBeCloseTo(-72 + (WIDTH + 144) / 4, 10);
    expect(sweepX(period / 2, WIDTH)).toBeCloseTo(WIDTH / 2, 10);
    expect(sweepX(period - 1, WIDTH)).toBeCloseTo(WIDTH + 72 - (WIDTH + 144) / period, 10);
    expect(sweepX(period, WIDTH)).toBe(-2 * SWEEP_WIDTH);

    const wide = 1100;
    const step = sweepX(1000, wide) - sweepX(500, wide);
    expect(step).toBeCloseTo(SWEEP_SPEED / 2, 10);
  });

  it('starts the sweep once the glide has stopped, from beyond the leading edge', () => {
    const bars = row(40, 1000);
    const at = (now: number) => frame(bars, { now, restAt: 1000 });
    const inner = (drawn: Drawn[]) =>
      drawn.filter((bar) => bar.x > FADE_LEFT && bar.x < SETTLED - 1);

    // Right after the tap nothing has dimmed yet.
    expect(Math.min(...inner(at(1000)).map((bar) => bar.alpha))).toBeGreaterThan(0.99);
    // Once rested, the bars away from the highlight sit at the floor.
    const rested = inner(at(1000 + REST_MS));
    expect(rested.length).toBeGreaterThan(20);
    for (const bar of rested) expect(bar.alpha).toBeCloseTo(REST_ALPHA, 1);
    // Mid-pass the highlight is lighting bars in the middle of the row.
    const middle = at(1000 + REST_MS + sweepPeriod(WIDTH) / 2).filter(
      (bar) => Math.abs(bar.x - WIDTH / 2) < PITCH,
    );
    expect(Math.max(...middle.map((bar) => bar.alpha))).toBeGreaterThan(0.95);
  });

  it('gives the trailing edge to the label as the timer fades out', () => {
    const bars = row(40, 1000);
    const cover = 60;
    const drawn = frame(bars, { now: 1000 + COVER_MS, restAt: 1000, cover });

    expect(Math.max(...drawn.map((bar) => bar.x + BAR_WIDTH / 2))).toBeLessThanOrEqual(
      WIDTH - cover,
    );
    expect(drawn.some((bar) => bar.x < WIDTH - cover - FADE_RIGHT)).toBe(true);
    // Before the tap the same bars reach the edge.
    expect(Math.max(...frame(bars, { now: 1000 }).map((bar) => bar.x))).toBeGreaterThan(
      WIDTH - cover,
    );
  });

  it('leaves no faint sliver of a bar where the label begins', () => {
    const bars = row(60, 1000);
    const cover = 47;
    // Wherever the glide stopped, against the label every bar is whole or gone.
    for (const restAt of [1000, 1013, 1027, 1041, 1055, 1069]) {
      const drawn = frame(bars, { now: restAt + REST_MS + 500, restAt, cover });
      const near = drawn.filter((bar) => bar.x > WIDTH - cover - 4 * PITCH);
      expect(near.length).toBeGreaterThan(0);
      for (const bar of near) {
        expect(bar.x + BAR_WIDTH / 2).toBeLessThanOrEqual(WIDTH - cover);
        expect(bar.alpha).toBeCloseTo(REST_ALPHA, 1);
      }
    }
  });
});

describe('Reduce Motion', () => {
  it('sets each bar on its slot, so the row moves one pitch per window', () => {
    const bars = row(3, 2000, 0.5);
    const early = frame(bars, { now: 2000, reduced: true });
    const late = frame(bars, { now: 2070, reduced: true });

    expect(early).toEqual(late);
    expect(early.map((bar) => bar.x)).toEqual([SETTLED, SETTLED - PITCH, SETTLED - 2 * PITCH]);
    expect(early[0]!.alpha).toBe(1);
  });

  it('draws each bar at its level at once', () => {
    const drawn = frame([{ level: 1, at: 2000 }], { now: 2000, reduced: true });
    expect(drawn[0]!.height).toBeCloseTo(HEIGHT * MAX_SHARE, 10);
  });

  it('rests without a sweep', () => {
    const bars = row(20, 2000);
    const first = frame(bars, { now: 2000, restAt: 2000, reduced: true });
    const later = frame(bars, { now: 2000 + REST_MS + SWEEP_MS / 2, restAt: 2000, reduced: true });

    expect(first).toEqual(later);
    const inner = first.filter((bar) => bar.x > FADE_LEFT);
    for (const bar of inner) expect(bar.alpha).toBe(REST_ALPHA);
  });
});
