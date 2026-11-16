import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { SETTLE_MS, useScrollFollow, type Opening } from '../src/features/chat/useScrollFollow';

// jsdom has no layout, so the scroller's geometry is simulated on the prototype
// and driven from these two variables.
let scrollHeight = 1000;
const CLIENT_HEIGHT = 400;

const original = {
  scrollHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight'),
  clientHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight'),
  scrollTo: HTMLElement.prototype.scrollTo,
};

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    get: () => scrollHeight,
    configurable: true,
  });
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    get: () => CLIENT_HEIGHT,
    configurable: true,
  });
  HTMLElement.prototype.scrollTo = function scrollToStub(this: HTMLElement, options?: unknown) {
    if (options && typeof options === 'object' && 'top' in options) {
      this.scrollTop = (options as ScrollToOptions).top ?? 0;
    }
  } as HTMLElement['scrollTo'];
});

afterAll(() => {
  if (original.scrollHeight) {
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', original.scrollHeight);
  }
  if (original.clientHeight) {
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', original.clientHeight);
  }
  HTMLElement.prototype.scrollTo = original.scrollTo;
});

beforeEach(() => {
  scrollHeight = 1000;
});

interface Props {
  revision: number;
  firstKey: string | null;
  lastKey: string | null;
  /** The detail level: the same blocks drawn differently. */
  redraw?: string;
  onReachTop?: () => void;
}

function Harness(props: Props) {
  const { ref, following, missed, onScroll } = useScrollFollow({
    revision: props.revision,
    redraw: props.redraw ?? 'detailed',
    firstKey: props.firstKey,
    lastKey: props.lastKey,
    onReachTop: props.onReachTop ?? (() => {}),
  });
  return (
    <div>
      <div data-testid="scroller" ref={ref} onScroll={onScroll} />
      <span data-testid="following">{String(following)}</span>
      <span data-testid="missed">{String(missed)}</span>
    </div>
  );
}

function setup(initial: Props) {
  const view = render(<Harness {...initial} />);
  const el = view.getByTestId('scroller');
  return {
    el,
    following: () => view.getByTestId('following').textContent,
    missed: () => Number(view.getByTestId('missed').textContent),
    /** Re-render after growing the content, the way a new event would. */
    update: (next: Props, height = scrollHeight) => {
      scrollHeight = height;
      act(() => view.rerender(<Harness {...next} />));
    },
    /** The reader scrolls: a wheel, then the scroll it makes. */
    scrollTo: (top: number) => {
      act(() => {
        el.dispatchEvent(new Event('wheel'));
      });
      el.scrollTop = top;
      act(() => {
        el.dispatchEvent(new Event('scroll', { bubbles: true }));
      });
    },
    /** Something else moves the scroller: no hand on it. */
    moveTo: (top: number) => {
      el.scrollTop = top;
      act(() => {
        el.dispatchEvent(new Event('scroll', { bubbles: true }));
      });
    },
  };
}

describe('useScrollFollow', () => {
  it('starts pinned to the bottom', () => {
    const h = setup({ revision: 1, firstKey: 'a', lastKey: 'a' });
    expect(h.following()).toBe('true');
    expect(h.missed()).toBe(0);
    expect(h.el.scrollTop).toBe(1000);
  });

  it('detaches when the user scrolls up and re-attaches at the bottom', () => {
    const h = setup({ revision: 1, firstKey: 'a', lastKey: 'a' });
    h.scrollTo(200);
    expect(h.following()).toBe('false');
    h.scrollTo(600);
    expect(h.following()).toBe('true');
  });

  it('does not drag the viewport down when a block is appended below', () => {
    const h = setup({ revision: 1, firstKey: 'a', lastKey: 'c' });
    h.scrollTo(200);

    // Same first key, taller content: the growth is at the tail.
    h.update({ revision: 2, firstKey: 'a', lastKey: 'd' }, 1400);

    expect(h.el.scrollTop).toBe(200);
  });

  it('holds the anchor when an older history page is prepended', () => {
    const h = setup({ revision: 5, firstKey: 'c', lastKey: 'e' });
    h.scrollTo(100);

    // A prepend: the first key changes and 600px of content lands above.
    h.update({ revision: 5, firstKey: 'a', lastKey: 'e' }, 1600);

    expect(h.el.scrollTop).toBe(700);
  });

  it('counts new blocks, not streaming deltas', () => {
    const h = setup({ revision: 1, firstKey: 'a', lastKey: 'a' });
    h.scrollTo(200);
    expect(h.missed()).toBe(0);

    for (let i = 2; i <= 11; i += 1) {
      h.update({ revision: i, firstKey: 'a', lastKey: 'a' });
    }
    expect(h.missed()).toBe(0);

    h.update({ revision: 12, firstKey: 'a', lastKey: 'b' });
    expect(h.missed()).toBe(1);
    h.update({ revision: 13, firstKey: 'a', lastKey: 'c' });
    expect(h.missed()).toBe(2);
  });

  it('does not count a prepended history page as a missed update', () => {
    const h = setup({ revision: 5, firstKey: 'c', lastKey: 'e' });
    h.scrollTo(100);
    h.update({ revision: 5, firstKey: 'a', lastKey: 'e' }, 1600);
    expect(h.missed()).toBe(0);
  });

  it('follows the tail while attached and resets the counter on return', () => {
    const h = setup({ revision: 1, firstKey: 'a', lastKey: 'a' });
    h.update({ revision: 2, firstKey: 'a', lastKey: 'b' }, 1200);
    expect(h.el.scrollTop).toBe(1200);
    expect(h.missed()).toBe(0);

    h.scrollTo(100);
    h.update({ revision: 3, firstKey: 'a', lastKey: 'c' }, 1400);
    expect(h.missed()).toBe(1);
    expect(h.el.scrollTop).toBe(100);

    h.scrollTo(1000);
    expect(h.missed()).toBe(0);
    expect(h.following()).toBe('true');
  });

  it('counts nothing when the detail level redraws the same blocks', () => {
    const h = setup({ revision: 1, firstKey: 'a', lastKey: 'c', redraw: 'simple' });
    h.scrollTo(200);
    h.update({ revision: 2, firstKey: 'a', lastKey: 'd', redraw: 'simple' });
    expect(h.missed()).toBe(1);

    // Switching the level draws other rows: a different first and last key, and
    // a taller page, but nothing the reader has not already been told about.
    h.update({ revision: 2, firstKey: 'thinking-1', lastKey: 'tool-9', redraw: 'detailed' }, 2200);
    expect(h.missed()).toBe(0);

    // The next genuine block counts again.
    h.update({ revision: 3, firstKey: 'thinking-1', lastKey: 'e', redraw: 'detailed' }, 2400);
    expect(h.missed()).toBe(1);
  });

  it('does not move the anchor when the redraw changes the first row', () => {
    const h = setup({ revision: 5, firstKey: 'c', lastKey: 'e', redraw: 'simple' });
    h.scrollTo(100);

    h.update({ revision: 5, firstKey: 'a', lastKey: 'e', redraw: 'detailed' }, 1600);

    expect(h.el.scrollTop).toBe(100);
  });

  it('asks for older history once the reader reaches the top', () => {
    const onReachTop = vi.fn();
    const h = setup({ revision: 1, firstKey: 'a', lastKey: 'a', onReachTop });
    h.scrollTo(40);
    expect(onReachTop).toHaveBeenCalled();
  });

  // An opening placed near the top is not the reader asking for more.
  it('asks for none before the reader has scrolled themselves', () => {
    const onReachTop = vi.fn();
    const h = setup({ revision: 1, firstKey: 'a', lastKey: 'a', onReachTop });
    h.moveTo(40);
    expect(onReachTop).not.toHaveBeenCalled();

    h.scrollTo(30);
    expect(onReachTop).toHaveBeenCalledTimes(1);
  });
});

/**
 * `docs/DESIGN.md` § "Reading position": a conversation opens with its opening
 * row's top at the top of the transcript, less a small margin, and the row is
 * held there while the rows above it settle — until the reader takes over or
 * two seconds have passed.
 */
describe('the opening', () => {
  /** Where each row's box starts in the content; a row is 100 px tall. */
  let offsets: Record<string, number> = {};
  const original = HTMLElement.prototype.getBoundingClientRect;

  beforeAll(() => {
    HTMLElement.prototype.getBoundingClientRect = function rect(this: HTMLElement) {
      const scroller = document.querySelector<HTMLElement>('[data-testid="scroller"]');
      const at = this === scroller ? 0 : (offsets[this.dataset.box ?? ''] ?? 0) - (scroller?.scrollTop ?? 0);
      const height = this === scroller ? CLIENT_HEIGHT : 100;
      return { top: at, bottom: at + height, left: 0, right: 0, width: 0, height, x: 0, y: at } as DOMRect;
    };
  });

  afterAll(() => {
    HTMLElement.prototype.getBoundingClientRect = original;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  const ROWS = ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j'];

  /** Ten rows stacked from the top, and the content as tall as they are. */
  const stack = (rows: string[], from = 0) => {
    offsets = Object.fromEntries(rows.map((key, index) => [key, from + index * 100]));
    scrollHeight = from + rows.length * 100;
  };

  function OpeningHarness({ opening, rows, revision }: { opening: Opening; rows: string[]; revision: number }) {
    const { ref, following, missed, onScroll } = useScrollFollow({
      revision,
      redraw: 'detailed',
      firstKey: rows[0] ?? null,
      lastKey: rows.at(-1) ?? null,
      onReachTop: () => {},
      opening,
    });
    return (
      <div>
        <div data-testid="scroller" ref={ref} onScroll={onScroll}>
          <div>
            {rows.map((key) => (
              <div key={key} data-row-key={key}>
                <p data-box={key}>{key}</p>
              </div>
            ))}
          </div>
        </div>
        <span data-testid="following">{String(following)}</span>
        <span data-testid="missed">{String(missed)}</span>
      </div>
    );
  }

  function open(opening: Opening, rows = ROWS) {
    stack(rows);
    const view = render(<OpeningHarness opening={opening} rows={rows} revision={1} />);
    const el = view.getByTestId('scroller');
    return {
      el,
      following: () => view.getByTestId('following').textContent,
      missed: () => Number(view.getByTestId('missed').textContent),
      rerender: (next: Opening, nextRows = rows, revision = 2) =>
        act(() => view.rerender(<OpeningHarness opening={next} rows={nextRows} revision={revision} />)),
    };
  }

  const row = (key: string): Opening => ({ kind: 'row', key });

  it('puts the row at the top, less a small margin, and does not follow the tail', () => {
    const h = open(row('e'));
    expect(h.el.scrollTop).toBe(388);
    expect(h.following()).toBe('false');
  });

  it('opens at the bottom, following, when the rest fits under the row', () => {
    const h = open(row('i'));
    // The end is 600; the stubbed `scrollTo` does not clamp the follow's own pin.
    expect(h.el.scrollTop).toBeGreaterThanOrEqual(600);
    expect(h.following()).toBe('true');
  });

  it('waits at the tail until it is decided, then goes to the row', () => {
    const h = open({ kind: 'wait' });
    expect(h.el.scrollTop).toBe(1000);

    h.rerender(row('c'));

    expect(h.el.scrollTop).toBe(188);
  });

  it('counts nothing that was there when it opened', () => {
    const h = open(row('c'));
    expect(h.missed()).toBe(0);

    const grown = [...ROWS, 'k'];
    stack(grown);
    h.rerender(row('c'), grown, 3);

    expect(h.missed()).toBe(1);
  });

  it('holds the row while the rows above it settle', () => {
    const h = open(row('e'));

    // The rows above grew by 150 px between them, as Markdown arriving does.
    offsets = { ...offsets, e: 550 };
    scrollHeight = 1150;
    h.rerender(row('e'), ROWS, 3);

    expect(h.el.scrollTop).toBe(538);
  });

  it('lets the reader take it from there', () => {
    const h = open(row('e'));
    act(() => {
      h.el.dispatchEvent(new Event('wheel'));
    });
    h.el.scrollTop = 100;

    offsets = { ...offsets, e: 550 };
    scrollHeight = 1150;
    h.rerender(row('e'), ROWS, 3);

    expect(h.el.scrollTop).toBe(100);
  });

  it('is not moved again after a touch', () => {
    const h = open(row('e'));
    act(() => {
      h.el.dispatchEvent(new Event('touchstart'));
    });

    offsets = { ...offsets, e: 550 };
    h.rerender(row('e'), ROWS, 3);

    expect(h.el.scrollTop).toBe(388);
  });

  it('is let go two seconds after it was placed', () => {
    vi.useFakeTimers();
    const h = open(row('e'));
    act(() => {
      vi.advanceTimersByTime(SETTLE_MS);
    });

    offsets = { ...offsets, e: 550 };
    h.rerender(row('e'), ROWS, 3);

    expect(h.el.scrollTop).toBe(388);
  });
});
