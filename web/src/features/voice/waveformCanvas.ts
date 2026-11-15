/**
 * Draws the listening row's waveform on a canvas, one animation frame at a time.
 *
 * Nothing here touches React: the bars come from the sampler, their geometry
 * from `paintBars`, and the loop runs only while the canvas has a size and the
 * page is visible. The backing store follows the device pixel ratio, so the
 * sub-pixel glide stays sharp, and the colour is the canvas's own CSS colour,
 * read back now and then, so a theme reaches the bars.
 */
import { BAR_WIDTH, paintBars } from './waveformFrame';
import type { LevelSampler } from './waveformSampler';

/** How often the bar colour is read back from CSS. */
const COLOR_MS = 500;

export class WaveformCanvas {
  private frame = 0;
  private width = 0;
  private height = 0;
  private context: CanvasRenderingContext2D | null = null;
  private color = '';
  private colorAt = Number.NEGATIVE_INFINITY;
  private restAt: number | null = null;
  private cover = 0;
  private reduced = false;
  /** Under Reduce Motion, what the last frame drew; an unchanged row is not redrawn. */
  private drawn = '';
  private observer: ResizeObserver | null = null;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly sampler: LevelSampler,
  ) {}

  /** Draw from the first moment the canvas is laid out. A page without layout never draws. */
  start(): void {
    if (typeof ResizeObserver === 'undefined') return;
    this.observer = new ResizeObserver((entries) => this.resize(entries));
    this.observer.observe(this.canvas);
    document.addEventListener('visibilitychange', this.onVisibility);
  }

  /**
   * Done, or the run is over: no new bars, the glide slows to a stop, and the
   * trailing `cover` pixels give way to the label that replaces the timer.
   */
  rest(at: number, cover: number): void {
    if (this.restAt !== null) return;
    this.sampler.stop();
    this.restAt = at;
    this.cover = cover;
  }

  setReducedMotion(reduced: boolean): void {
    this.reduced = reduced;
    this.drawn = '';
  }

  dispose(): void {
    this.cancel();
    this.observer?.disconnect();
    this.observer = null;
    document.removeEventListener('visibilitychange', this.onVisibility);
  }

  private resize(entries: ResizeObserverEntry[]): void {
    const box = entries.at(-1)?.contentRect;
    if (!box) return;
    this.width = box.width;
    this.height = box.height;
    this.schedule();
  }

  /** A hidden tab draws nothing; the loop picks up again when it is shown. */
  private readonly onVisibility = (): void => {
    if (document.visibilityState === 'hidden') this.cancel();
    else this.schedule();
  };

  private schedule(): void {
    if (this.frame !== 0 || this.width <= 0 || this.height <= 0) return;
    if (document.visibilityState === 'hidden') return;
    this.frame = requestAnimationFrame(this.tick);
  }

  private cancel(): void {
    if (this.frame !== 0) cancelAnimationFrame(this.frame);
    this.frame = 0;
  }

  private readonly tick = (now: number): void => {
    this.frame = 0;
    this.draw(now);
    this.schedule();
  };

  private draw(now: number): void {
    this.context ??= this.canvas.getContext('2d');
    const context = this.context;
    if (!context) return;
    const ratio = window.devicePixelRatio || 1;
    const pixelWidth = Math.round(this.width * ratio);
    const pixelHeight = Math.round(this.height * ratio);
    if (this.canvas.width !== pixelWidth || this.canvas.height !== pixelHeight) {
      this.canvas.width = pixelWidth;
      this.canvas.height = pixelHeight;
      this.drawn = '';
    }
    if (now - this.colorAt >= COLOR_MS) {
      this.color = getComputedStyle(this.canvas).color;
      this.colorAt = now;
    }
    if (this.reduced && !this.changed(pixelWidth, pixelHeight)) return;

    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, this.width, this.height);
    context.fillStyle = this.color;
    const middle = this.height / 2;
    paintBars(
      this.sampler.bars,
      { width: this.width, height: this.height },
      { now, restAt: this.restAt, cover: this.cover, reduced: this.reduced },
      (x, height, alpha) => {
        context.globalAlpha = alpha;
        capsule(context, x, middle, height);
      },
    );
    context.globalAlpha = 1;
  }

  /** Under Reduce Motion the row only changes when a bar arrives or the row rests. */
  private changed(pixelWidth: number, pixelHeight: number): boolean {
    const key = [
      this.sampler.bars.length,
      this.sampler.bars.at(-1)?.at,
      this.restAt,
      this.cover,
      pixelWidth,
      pixelHeight,
      this.color,
    ].join('|');
    if (key === this.drawn) return false;
    this.drawn = key;
    return true;
  }
}

/** A bar with fully rounded ends centred on (x, y); at its shortest, a dot. */
function capsule(context: CanvasRenderingContext2D, x: number, y: number, height: number): void {
  const radius = BAR_WIDTH / 2;
  const reach = Math.max(0, height / 2 - radius);
  context.beginPath();
  context.arc(x, y - reach, radius, Math.PI, 0);
  context.arc(x, y + reach, radius, 0, Math.PI);
  context.closePath();
  context.fill();
}
