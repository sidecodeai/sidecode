/**
 * The jump-to-latest control never covers a request that waits for
 * the reader. While any of a pending card's controls is inside the timeline's
 * visible area the control is not drawn, the count of what arrives goes on,
 * and the control comes back under its usual rules once they have all left.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Timeline } from '../src/features/chat/Timeline';
import { useSettings } from '../src/stores/settings';
import { applyEvent, emptyTimeline, type TimelineState } from '../src/stores/timeline';
import type { SessionEvent } from '../src/protocol/types';

// jsdom has no layout: the scroller's geometry is simulated as in
// `Timeline.test.tsx`, and what is on screen is said by the observer below.
let scrollHeight = 1000;

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
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { get: () => 400, configurable: true });
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

/** An IntersectionObserver the test drives: it says which targets are in view. */
class FakeObserver {
  static made: FakeObserver[] = [];
  readonly observed = new Set<Element>();
  readonly root: Element | null;

  constructor(
    readonly callback: IntersectionObserverCallback,
    options?: IntersectionObserverInit,
  ) {
    this.root = options?.root instanceof Element ? options.root : null;
    FakeObserver.made.push(this);
  }

  observe(target: Element) {
    this.observed.add(target);
  }

  unobserve(target: Element) {
    this.observed.delete(target);
  }

  disconnect() {
    this.observed.clear();
  }

  takeRecords() {
    return [];
  }

  /** Layout moved these targets into, or out of, the root's visible area. */
  sight(targets: Element[], isIntersecting: boolean) {
    const entries = targets.map((target) => ({ target, isIntersecting }));
    act(() =>
      this.callback(
        entries as unknown as IntersectionObserverEntry[],
        this as unknown as IntersectionObserver,
      ),
    );
  }
}

beforeEach(() => {
  scrollHeight = 1000;
  FakeObserver.made = [];
  vi.stubGlobal('IntersectionObserver', FakeObserver);
  useSettings.setState({ timelineDetail: 'detailed' });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const notice = (seq: number, text: string): SessionEvent =>
  ({ seq, ts: 1_000 + seq, kind: 'notice', level: 'info', text }) as SessionEvent;

const question = (seq: number, status: 'pending' | 'resolved'): SessionEvent =>
  ({
    seq,
    ts: 2_000 + seq,
    kind: 'question',
    block_id: 'q-backport',
    request_id: 'req-backport',
    questions: [
      {
        id: 'backport',
        prompt: 'Should I also backport the fix to the release branch?',
        options: [
          { id: 'yes', label: 'Yes, open a cherry-pick' },
          { id: 'no', label: 'No, main only' },
        ],
        multi: false,
        allow_text: true,
      },
    ],
    status,
    ...(status === 'resolved' ? { answers: { backport: ['no'] }, by: 'remote' } : {}),
  }) as SessionEvent;

const approval = (seq: number, status: 'pending' | 'resolved', plan = false): SessionEvent =>
  ({
    seq,
    ts: 3_000 + seq,
    kind: 'approval',
    block_id: 'ap-commit',
    request_id: 'req-commit',
    tool: plan ? 'ExitPlanMode' : 'Bash',
    tool_kind: 'shell',
    title: plan ? 'Plan ready: isolate the clock' : 'git commit -am "fix: isolate the auth clock"',
    ...(plan ? { approval_kind: 'plan', input: { plan: '1. Freeze the clock' } } : {}),
    options: plan
      ? [
          { id: 'plan_accept', label: 'Yes', style: 'primary' },
          { id: 'plan_keep', label: 'No, keep planning', style: 'danger' },
        ]
      : [
          { id: 'allow', label: 'Allow once', style: 'primary' },
          { id: 'deny', label: 'Deny', style: 'danger' },
        ],
    status,
    ...(status === 'resolved' ? { decision: { option_id: 'allow', by: 'remote' } } : {}),
  }) as SessionEvent;

const stateOf = (...events: SessionEvent[]): TimelineState =>
  events.reduce(applyEvent, emptyTimeline());

function setup(initial: TimelineState) {
  const element = (timeline: TimelineState) => (
    <Timeline
      sessionKey="dev/ses"
      current
      pagesAsked={1}
      timeline={timeline}
      historyLoading={false}
      historyHasMore
      historyLoaded
      repairing={false}
      error={null}
      onRetry={vi.fn()}
      onOpenFull={vi.fn().mockResolvedValue(undefined)}
      onApprove={vi.fn().mockResolvedValue(undefined)}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onLoadOlder={vi.fn()}
    />
  );
  const view = render(element(initial));
  const scroller = document.querySelector('.timeline') as HTMLElement;
  const observer = FakeObserver.made.at(-1);
  if (!observer) throw new Error('the timeline should watch its pending controls');
  return {
    scroller,
    observer,
    button: () => screen.queryByRole('button', { name: /back to latest/i }),
    update: (next: TimelineState, height = scrollHeight) => {
      scrollHeight = height;
      act(() => view.rerender(element(next)));
    },
    /** The reader scrolls away from the tail: a wheel, then the scroll it makes. */
    scrollAway: () => {
      act(() => {
        scroller.dispatchEvent(new Event('wheel'));
      });
      scroller.scrollTop = 300;
      act(() => {
        scroller.dispatchEvent(new Event('scroll', { bubbles: true }));
      });
    },
  };
}

const one = (selector: string): Element => {
  const found = document.querySelector(selector);
  if (!found) throw new Error(`nothing matches ${selector}`);
  return found;
};

describe('back to latest beside a pending request', () => {
  it("watches a pending question's options, free-text field and Submit inside the scroller", () => {
    const h = setup(stateOf(notice(1, 'one'), question(2, 'pending')));

    expect(h.observer.root).toBe(h.scroller);
    expect([...h.observer.observed]).toEqual(
      expect.arrayContaining([
        one('.question-options'),
        one('.question input.field'),
        one('.question .approval-actions'),
      ]),
    );
    expect(h.observer.observed.size).toBe(3);
  });

  it('gives way while one of them is on screen and keeps counting what arrives', () => {
    const start = stateOf(notice(1, 'one'), question(2, 'pending'));
    const h = setup(start);
    h.scrollAway();
    // Away from the tail with nothing of the card in view: the usual rule.
    expect(h.button()).not.toBeNull();

    h.observer.sight([one('.question-options'), one('.question input.field')], true);
    expect(h.button()).toBeNull();

    // The free-text field alone is still a part of the controls on screen.
    h.observer.sight([one('.question-options')], false);
    expect(h.button()).toBeNull();

    h.update(applyEvent(start, notice(3, 'two')), 1200);
    h.update(applyEvent(applyEvent(start, notice(3, 'two')), notice(4, 'three')), 1400);
    expect(h.button()).toBeNull();

    // All of them have left the visible area: back, with what arrived meanwhile.
    h.observer.sight([one('.question input.field')], false);
    expect(h.button()).toHaveTextContent('2 new');
    expect(screen.getByRole('button', { name: 'Back to latest, 2 new' })).toBeInTheDocument();
  });

  it('takes no notice of a request that was already answered', () => {
    const h = setup(stateOf(notice(1, 'one'), question(2, 'resolved')));

    expect(h.observer.observed.size).toBe(0);
    h.scrollAway();
    expect(h.button()).not.toBeNull();
  });

  it('comes back as soon as the request on screen is answered', async () => {
    const user = userEvent.setup();
    const h = setup(stateOf(notice(1, 'one'), approval(2, 'pending')));
    const actions = one('.approval .approval-actions');
    expect([...h.observer.observed]).toEqual([actions]);

    h.scrollAway();
    h.observer.sight([actions], true);
    expect(h.button()).toBeNull();

    h.update(stateOf(notice(1, 'one'), approval(3, 'resolved')));
    expect(h.observer.observed.size).toBe(0);
    expect(h.button()).not.toBeNull();

    // A sighting the observer queued before the buttons went changes nothing.
    h.observer.sight([actions], true);
    expect(h.button()).not.toBeNull();

    // And the control still works as it always has.
    const button = h.button();
    if (!button) throw new Error('the control should be on screen');
    await user.click(button);
    expect(h.button()).toBeNull();
  });

  it("watches a plan's choices, then the field for what to change", async () => {
    const user = userEvent.setup();
    const h = setup(stateOf(notice(1, 'one'), approval(2, 'pending', true)));
    expect([...h.observer.observed]).toEqual([one('.plan .approval-actions')]);

    await user.click(screen.getByRole('button', { name: /keep planning/i }));
    expect([...h.observer.observed]).toEqual([one('.plan-feedback')]);
  });
});
