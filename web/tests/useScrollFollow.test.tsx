import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render } from '@testing-library/react';
import { useScrollFollow } from '../src/features/chat/useScrollFollow';

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
    scrollTo: (top: number) => {
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
});
