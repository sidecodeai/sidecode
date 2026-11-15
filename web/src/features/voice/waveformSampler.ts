/**
 * The listening row's level model: one bar every 80 ms, as loud as the audio
 * its window held (`docs/DESIGN.md` § "The composer", A57).
 *
 * Pure apart from its own interval and an injected clock, so the mapping, the
 * smoothing and the cadence are tested with fake timers and no canvas.
 */

/** One bar per window: about 12.5 bars a second. */
export const SAMPLE_MS = 80;
/** A window's RMS at this many dBFS, or quieter, is drawn as a dot. */
export const FLOOR_DB = -50;
/** From the floor to full height: −10 dBFS of RMS and up is a full bar. */
export const RANGE_DB = 40;
/** How much of the last bar a falling level keeps, so a word's tail decays rather than drops. */
export const RELEASE = 0.55;

export interface Bar {
  /** 0 for a dot, 1 for a full bar, after the mapping and the release. */
  level: number;
  /** When the bar's window closed, on the sampler's 80 ms grid, in its clock's milliseconds. */
  at: number;
}

/** `dB = 20·log10(rms)` with full scale 1, then −50 dBFS → 0 and −10 dBFS → 1. */
export function levelOf(rms: number): number {
  if (!(rms > 0)) return 0;
  const db = 20 * Math.log10(rms);
  return Math.min(1, Math.max(0, (db - FLOOR_DB) / RANGE_DB));
}

/** A rise is shown at once; a fall keeps at least 55% of the bar before it. */
export function release(raw: number, previous: number): number {
  return raw >= previous ? raw : Math.max(raw, previous * RELEASE);
}

interface Options {
  /** The most bars kept; older ones have glided out of any row by then. */
  capacity: number;
  /** Milliseconds on the timeline the bars are drawn against. */
  now: () => number;
}

export class LevelSampler {
  /** The energy of the open window's audio, and how many samples it had. */
  private energy = 0;
  private samples = 0;
  private shown = 0;
  private origin = 0;
  private slot = 0;
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly kept: Bar[] = [];

  constructor(private readonly options: Options) {}

  /** Oldest first. */
  get bars(): readonly Bar[] {
    return this.kept;
  }

  /**
   * One block of audio: its energy, the sum of its squared samples, and how
   * many it had. The window's level is the RMS of everything it held — the
   * loudest of its blocks' peaks sat at the top of the range under a
   * browser's gain control (A57).
   */
  push(energy: number, samples: number): void {
    this.energy += energy;
    this.samples += samples;
  }

  /**
   * Close a window every 80 ms from now on. The row starts full of resting
   * dots, as if silence had been recorded, so it never shows an empty
   * stretch; the first bar arrives at the trailing edge and pushes them along.
   */
  start(): void {
    if (this.timer !== null) return;
    this.origin = this.options.now();
    this.slot = 0;
    this.shown = 0;
    this.energy = 0;
    this.samples = 0;
    this.kept.length = 0;
    for (let slot = 1 - this.options.capacity; slot <= 0; slot += 1) this.append(slot);
    this.timer = setInterval(() => this.commit(), SAMPLE_MS);
  }

  /** No new bars: the ones already drawn stay. */
  stop(): void {
    if (this.timer === null) return;
    clearInterval(this.timer);
    this.timer = null;
  }

  /**
   * A bar is stamped with the slot on the grid nearest the moment its timer
   * fired, so jitter never bunches two bars together, and the catch-up tick
   * after a late one finds its slot taken and adds nothing, so the bars never
   * run ahead of the clock. Slots a throttled timer skipped are not left empty:
   * the bar before them decays through them to dots, and the window's level
   * comes after.
   */
  private commit(): void {
    const due = Math.round((this.options.now() - this.origin) / SAMPLE_MS);
    if (due <= this.slot) return;
    for (let slot = Math.max(this.slot + 1, due - this.options.capacity); slot < due; slot += 1) {
      this.shown = release(0, this.shown);
      this.append(slot);
    }
    const rms = this.samples > 0 ? Math.sqrt(this.energy / this.samples) : 0;
    this.slot = due;
    this.shown = release(levelOf(rms), this.shown);
    this.energy = 0;
    this.samples = 0;
    this.append(due);
  }

  private append(slot: number): void {
    this.kept.push({ level: this.shown, at: this.origin + slot * SAMPLE_MS });
    const excess = this.kept.length - this.options.capacity;
    if (excess > 0) this.kept.splice(0, excess);
  }
}
