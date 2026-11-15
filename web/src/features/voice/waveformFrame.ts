/**
 * Where each bar of the listening row is drawn at one instant: its centre, its
 * height and its opacity. Pure, so the glide, the growth, the edge fades and
 * the rest after Done are tested without a canvas (`docs/DESIGN.md` § "The
 * composer", A57). Every length is in CSS pixels, every time
 * in milliseconds.
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
/** The label takes the timer's place in this long, covering the waveform's end. */
export const COVER_MS = 150;
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

/** How long one pass of the highlight takes over a row this wide: `max(1.4 s, (w + 144) / 480)`. */
export function sweepPeriod(width: number): number {
  return Math.max(SWEEP_MS, ((width + 4 * SWEEP_WIDTH) / SWEEP_SPEED) * 1000);
}

/**
 * Where the highlight is `elapsed` ms into its sweeps: it travels from beyond
 * the leading edge to beyond the trailing one at a constant speed, and starts
 * over at once, so it is in view for most of every pass.
 */
export function sweepX(elapsed: number, width: number): number {
  const period = sweepPeriod(width);
  return -2 * SWEEP_WIDTH + (width + 4 * SWEEP_WIDTH) * ((elapsed % period) / period);
}

/** A rested bar's opacity: `0.35 + 0.65·exp(−((x − sweepX) / 36)²)`. */
export function sweepAlpha(x: number, highlight: number): number {
  return REST_ALPHA + (1 - REST_ALPHA) * Math.exp(-(((x - highlight) / SWEEP_WIDTH) ** 2));
}

interface Rest {
  /**
   * Where the label and its clearance begin: a bar reaching past it is not
   * drawn at all, so no faint sliver of one is left at the label's edge.
   */
  edge: number;
  alpha: (x: number) => number;
}

function restOf(motion: FrameMotion, width: number): Rest | null {
  if (motion.restAt === null) return null;
  const since = motion.now - motion.restAt;
  const covered = motion.reduced ? 1 : easeOut(clamp01(since / COVER_MS));
  const edge = width - motion.cover * covered;
  if (motion.reduced) return { edge, alpha: () => REST_ALPHA };
  // The bars dim to the sweep's floor while the glide slows, then the highlight runs.
  const dim = easeOut(clamp01(since / REST_MS));
  const highlight = sweepX(Math.max(0, since - REST_MS), width);
  return { edge, alpha: (x) => 1 + (sweepAlpha(x, highlight) - 1) * dim };
}

/** How much of a bar shows at the trailing end: it emerges while listening, and is cut at rest. */
function trailing(x: number, width: number, rest: Rest | null): number {
  if (rest) return x + BAR_WIDTH / 2 <= rest.edge ? rest.alpha(x) : 0;
  return clamp01((width - x) / FADE_RIGHT);
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
  for (let index = bars.length - 1; index >= 0; index -= 1) {
    const bar = bars[index];
    if (!bar) continue;
    const x = motion.reduced
      ? settled - ((newest.at - bar.at) / SAMPLE_MS) * PITCH
      : settled + PITCH - (glide - bar.at) * SPEED;
    // Every older bar is further left still.
    if (x < -BAR_WIDTH) return;
    const alpha = clamp01(x / FADE_LEFT) * trailing(x, layout.width, rest);
    if (alpha <= 0) continue;
    const grown = motion.reduced ? 1 : easeOut(clamp01((motion.now - bar.at) / GROW_MS));
    paint(x, MIN_HEIGHT + (tallest - MIN_HEIGHT) * bar.level ** HEIGHT_CURVE * grown, alpha);
  }
}
