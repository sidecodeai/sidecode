/**
 * Where each bar of the listening row is drawn at one instant: its centre, its
 * height and its opacity. Pure, so the glide, the growth, the edge fades and
 * the rest after Done are tested without a canvas (`docs/DESIGN.md` § "The
 * composer", A57). Every length is in CSS pixels, every time in milliseconds.
 */
import { SAMPLE_MS, type Bar } from './waveformSampler';

export const BAR_WIDTH = 3;
/** A bar and the gap after it. */
export const PITCH = 6;
/** A bar's shortest form, a dot: what silence draws. */
export const MIN_HEIGHT = 3;
/** The tallest bar's share of the row's inner height, centred. */
export const MAX_SHARE = 0.6;
/**
 * Heights follow `level^1.5`: ordinary speech spreads across the range, and
 * only loud syllables reach the top.
 */
export const HEIGHT_CURVE = 1.5;
/** Opacity ramps from 0 over this much of the leading edge, so old bars dissolve. */
export const FADE_LEFT = 24;
/** And over this much of the trailing edge, so new bars emerge. */
export const FADE_RIGHT = 6;
/** The glide: one pitch per window, 75 px a second. */
export const SPEED = PITCH / SAMPLE_MS;
/** A new bar grows from a dot to its level in this long. */
export const GROW_MS = 120;
/** Done: the glide slows to a stop in this long, and the bars dim to the sweep's floor. */
export const REST_MS = 200;
/**
 * The bars under the label are gone this soon after Done, ahead of the label,
 * which still takes 150 ms to fade in.
 */
export const COVER_MS = 80;
/** The shortest pass of the highlight over the rested bars. */
export const SWEEP_MS = 1400;
/** And its fastest travel, in px a second: a wide row takes longer, never faster. */
export const SWEEP_SPEED = 480;
/** The highlight's width: `exp(−((x − sweepX) / 36)²)`. */
export const SWEEP_WIDTH = 36;
/** A rested bar's opacity away from the highlight. */
export const REST_ALPHA = 0.35;

export interface FrameLayout {
  width: number;
  height: number;
}

export interface FrameMotion {
  now: number;
  /** When the row came to rest — Done, or the run's end — or null while it listens. */
  restAt: number | null;
  /** How much of the trailing edge the "Transcribing…" label covers once resting. */
  cover: number;
  /** Reduce Motion: no glide, no growth, no sweep. */
  reduced: boolean;
}

export type PaintBar = (x: number, height: number, alpha: number) => void;

const clamp01 = (value: number): number => Math.min(1, Math.max(0, value));
const easeOut = (t: number): number => 1 - (1 - t) ** 3;

/**
 * The glide's own clock: real time while listening; after Done its speed falls
 * linearly to zero over 200 ms, so the row decelerates to a stop instead of
 * freezing mid-step.
 */
export function glideClock(now: number, restAt: number | null): number {
  if (restAt === null || now <= restAt) return now;
  const t = Math.min(1, (now - restAt) / REST_MS);
  return restAt + REST_MS * (t - (t * t) / 2);
}

/**
 * How long one pass of the highlight takes over bars that end at `barsEnd`:
 * `max(1.4 s, (barsEnd + 72) / 480)`.
 */
export function sweepPeriod(barsEnd: number): number {
  return Math.max(SWEEP_MS, ((barsEnd + 2 * SWEEP_WIDTH) / SWEEP_SPEED) * 1000);
}

/**
 * Where the highlight is `elapsed` ms into its sweeps: it travels over the bars
 * drawn, from 36 px before the first to 36 px past the last, at a constant
 * speed, and starts over at once, so every pass lights bars nearly
 * throughout.
 */
export function sweepX(elapsed: number, barsEnd: number): number {
  const period = sweepPeriod(barsEnd);
  return -SWEEP_WIDTH + (barsEnd + 2 * SWEEP_WIDTH) * ((elapsed % period) / period);
}

/** A rested bar's opacity: `0.35 + 0.65·exp(−((x − sweepX) / 36)²)`. */
export function sweepAlpha(x: number, highlight: number): number {
  return REST_ALPHA + (1 - REST_ALPHA) * Math.exp(-(((x - highlight) / SWEEP_WIDTH) ** 2));
}

interface Rest {
  /**
   * Where the label and its clearance begin, as the cover closes over the
   * bars' tail: a bar reaching past it is not drawn at all, so no faint sliver
   * of one is left at the label's edge.
   */
  edge: number;
  /**
   * The glide clock as the cover closes. Whether a bar is drawn is read from
   * where it stands then: the glide is still slowing, and a bar the cover took
   * must not slide back out from under it.
   */
  closed: number;
  alpha: (x: number) => number;
}

function restOf(motion: FrameMotion, width: number): Rest | null {
  if (motion.restAt === null) return null;
  const since = motion.now - motion.restAt;
  const closing = motion.reduced ? 1 : easeOut(clamp01(since / COVER_MS));
  const edge = width - motion.cover * closing;
  const closed = glideClock(motion.restAt + COVER_MS, motion.restAt);
  if (motion.reduced) return { edge, closed, alpha: () => REST_ALPHA };
  // The bars dim to the sweep's floor while the glide slows, then the highlight
  // runs over the bars the label leaves.
  const dim = easeOut(clamp01(since / REST_MS));
  const highlight = sweepX(Math.max(0, since - REST_MS), width - motion.cover);
  return { edge, closed, alpha: (x) => 1 + (sweepAlpha(x, highlight) - 1) * dim };
}

/**
 * Hand every visible bar to `paint`, newest first. A bar is born at the
 * trailing edge, inside the fade, and has fully emerged one window later; under
 * Reduce Motion each bar sits on its slot instead, and the row moves one pitch
 * per window.
 */
export function paintBars(
  bars: readonly Bar[],
  layout: FrameLayout,
  motion: FrameMotion,
  paint: PaintBar,
): void {
  const newest = bars.at(-1);
  if (!newest || layout.width <= 0) return;
  const settled = layout.width - FADE_RIGHT - BAR_WIDTH / 2;
  const tallest = Math.max(MIN_HEIGHT, layout.height * MAX_SHARE);
  const glide = glideClock(motion.now, motion.restAt);
  const rest = restOf(motion, layout.width);
  /** A bar's centre when the glide's clock reads `clock`. */
  const at = (bar: Bar, clock: number): number =>
    motion.reduced
      ? settled - ((newest.at - bar.at) / SAMPLE_MS) * PITCH
      : settled + PITCH - (clock - bar.at) * SPEED;
  /** At the trailing end a bar emerges while listening, and at rest is whole or not drawn. */
  const trailing = (bar: Bar, x: number): number => {
    if (!rest) return clamp01((layout.width - x) / FADE_RIGHT);
    return at(bar, rest.closed) + BAR_WIDTH / 2 <= rest.edge ? rest.alpha(x) : 0;
  };
  for (let index = bars.length - 1; index >= 0; index -= 1) {
    const bar = bars[index];
    if (!bar) continue;
    const x = at(bar, glide);
    // Every older bar is further left still.
    if (x < -BAR_WIDTH) return;
    const alpha = clamp01(x / FADE_LEFT) * trailing(bar, x);
    if (alpha <= 0) continue;
    const grown = motion.reduced ? 1 : easeOut(clamp01((motion.now - bar.at) / GROW_MS));
    paint(x, MIN_HEIGHT + (tallest - MIN_HEIGHT) * bar.level ** HEIGHT_CURVE * grown, alpha);
  }
}
