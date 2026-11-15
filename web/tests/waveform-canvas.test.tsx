/**
 * The waveform's drawing loop (A57, `docs/DESIGN.md` § "The composer"): it
 * draws once the canvas is laid out, at the device's pixel ratio and in the
 * canvas's own CSS colour, from the first frame a row full of resting dots;
 * it stops while the page is hidden and when the row goes away; Done stops
 * the bars from arriving; and the canvas is decorative, hidden from assistive
 * technology.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { LevelFeed } from '../src/features/voice/levelFeed';
import { Waveform } from '../src/features/voice/Waveform';

/** Stands in for layout: the test says when the canvas has a size. */
class Layout {
  static current: Layout | null = null;
  disconnected = false;

  constructor(private readonly callback: ResizeObserverCallback) {
    Layout.current = this;
  }

  observe(): void {}

  unobserve(): void {}

  disconnect(): void {
    this.disconnected = true;
  }

  size(width: number, height: number): void {
    const entry = { contentRect: { width, height } } as ResizeObserverEntry;
    this.callback([entry], this as unknown as ResizeObserver);
  }
}

/** A 2D context that counts what it was asked to draw, and where. */
function recordingContext() {
  const drawn = {
    clears: 0,
    bars: 0,
    /** The centre of every bar drawn since the last clear. */
    xs: [] as number[],
    transforms: [] as number[][],
    colours: [] as string[],
  };
  let first = true;
  const context = {
    globalAlpha: 1,
    set fillStyle(value: string) {
      drawn.colours.push(value);
    },
    setTransform: (...matrix: number[]) => void drawn.transforms.push(matrix),
    clearRect: () => {
      drawn.clears += 1;
      drawn.xs = [];
    },
    beginPath: () => {
      first = true;
    },
    // A bar is two arcs about one centre; the first says where it is.
    arc: (x: number) => {
      if (first) drawn.xs.push(x);
      first = false;
    },
    closePath: () => undefined,
    fill: () => void (drawn.bars += 1),
  };
  return { context, drawn };
}

let canvas2d: ReturnType<typeof recordingContext>;

function setVisibility(state: DocumentVisibilityState): void {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state });
  document.dispatchEvent(new Event('visibilitychange'));
}

function mount(resting = false) {
  const levels = new LevelFeed();
  const view = render(<Waveform levels={levels} resting={resting} cover={() => 0} />);
  const canvas = view.container.querySelector('canvas');
  if (!canvas) throw new Error('no canvas');
  canvas.style.color = 'rgb(17, 17, 17)';
  return { ...view, levels, canvas };
}

/** Let `ms` of animation frames and sampler windows run. */
const run = async (ms: number): Promise<void> => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
};

beforeEach(() => {
  vi.useFakeTimers();
  Layout.current = null;
  canvas2d = recordingContext();
  vi.stubGlobal('ResizeObserver', Layout);
  vi.stubGlobal('devicePixelRatio', 2);
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    () => canvas2d.context as never,
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Reflect.deleteProperty(document, 'visibilityState');
});

describe('the waveform canvas', () => {
  it('is decorative', () => {
    const { canvas } = mount();
    expect(canvas).toHaveAttribute('aria-hidden', 'true');
  });

  it('draws nothing until it is laid out, then draws at the pixel ratio', async () => {
    const { canvas } = mount();
    await run(200);
    expect(canvas2d.drawn.clears).toBe(0);

    act(() => Layout.current?.size(240, 34));
    await run(100);

    expect(canvas.width).toBe(480);
    expect(canvas.height).toBe(68);
    expect(canvas2d.drawn.transforms.at(-1)).toEqual([2, 0, 0, 2, 0, 0]);
    expect(canvas2d.drawn.colours.at(-1)).toBe('rgb(17, 17, 17)');
    expect(canvas2d.drawn.clears).toBeGreaterThan(3);
  });

  it('fills the row with resting dots from the first frame, with no gap', async () => {
    mount();
    act(() => Layout.current?.size(240, 34));
    await run(16);

    // Newest first, one pitch apart, from the trailing edge to the leading one.
    const xs = canvas2d.drawn.xs;
    expect(xs.length).toBeGreaterThanOrEqual(38);
    expect(xs[0]).toBeGreaterThan(232);
    expect(xs.at(-1)).toBeLessThan(6);
    for (let i = 1; i < xs.length; i += 1) expect(xs[i - 1]! - xs[i]!).toBeCloseTo(6, 6);
  });

  it('keeps the row full as bars arrive, loud or silent', async () => {
    const { levels } = mount();
    act(() => Layout.current?.size(240, 34));
    await run(16);
    const full = canvas2d.drawn.xs.length;

    levels.emit(0.3 * 0.3 * 128, 128);
    await run(80 * 5);

    expect(Math.abs(canvas2d.drawn.xs.length - full)).toBeLessThanOrEqual(1);
  });

  it('stops drawing while the page is hidden, and picks up when it is shown', async () => {
    mount();
    act(() => Layout.current?.size(240, 34));
    await run(100);

    act(() => setVisibility('hidden'));
    const hidden = canvas2d.drawn.clears;
    await run(500);
    expect(canvas2d.drawn.clears).toBe(hidden);

    act(() => setVisibility('visible'));
    await run(100);
    expect(canvas2d.drawn.clears).toBeGreaterThan(hidden);
  });

  it('stops for good when the row goes away', async () => {
    const view = mount();
    act(() => Layout.current?.size(240, 34));
    await run(100);

    view.unmount();
    const last = canvas2d.drawn.clears;
    await run(500);

    expect(canvas2d.drawn.clears).toBe(last);
    expect(Layout.current?.disconnected).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('takes no new bars once Done is tapped, and keeps drawing the ones it has', async () => {
    const { levels, rerender } = mount();
    act(() => Layout.current?.size(240, 34));
    await run(80 * 4 + 10);

    rerender(<Waveform levels={levels} resting cover={() => 0} />);
    await run(300);
    const rested = [...canvas2d.drawn.xs];

    levels.emit(0.5 * 0.5 * 128, 128);
    await run(80 * 5);

    expect(rested.length).toBeGreaterThan(30);
    expect(canvas2d.drawn.xs).toEqual(rested);
  });
});
