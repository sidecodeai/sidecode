/**
 * The jump-to-latest control. `docs/DESIGN.md` § "Reading position": it is on
 * screen as soon as the reader scrolls away, whether or not anything new has
 * arrived, because paging up through history needs a way back down too.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Timeline } from '../src/features/chat/Timeline';
import { useSettings } from '../src/stores/settings';
import { applyEvent, emptyTimeline, mergeHistory, type TimelineState } from '../src/stores/timeline';
import type { SessionEvent } from '../src/protocol/types';

// jsdom has no layout, so the scroller's geometry is simulated on the prototype
// and driven from these two variables, as in `useScrollFollow.test.tsx`.
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
  useSettings.setState({ timelineDetail: 'detailed' });
});

afterEach(cleanup);

const notice = (seq: number, text: string): SessionEvent =>
  ({ seq, ts: 1_000 + seq, kind: 'notice', level: 'info', text }) as SessionEvent;

const toolCall = (seq: number): SessionEvent =>
  ({
    seq,
    ts: 2_000 + seq,
    kind: 'tool_call',
    block_id: `t${seq}`,
    tool: 'Read',
    tool_kind: 'read',
    title: `file-${seq}.ts`,
    status: 'succeeded',
    started_at: 2_000,
    ended_at: 2_100,
  }) as SessionEvent;

/** A34: a teammate's report, which Claude Code files as a user turn. */
const agentMessage = (seq: number): SessionEvent =>
  ({
    seq,
    ts: 3_000 + seq,
    kind: 'user_message',
    block_id: `ag${seq}`,
    source: 'agent',
    text: `recon-${seq}: reporting back`,
  }) as SessionEvent;

const stateOf = (...events: SessionEvent[]): TimelineState => events.reduce(applyEvent, emptyTimeline());

function setup(initial: TimelineState) {
  const onLoadOlder = vi.fn();
  const element = (timeline: TimelineState) => (
    <Timeline
      timeline={timeline}
      historyLoading={false}
      historyHasMore
      onOpenFull={vi.fn().mockResolvedValue(undefined)}
      onApprove={vi.fn().mockResolvedValue(undefined)}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onLoadOlder={onLoadOlder}
    />
  );
  const view = render(element(initial));
  const scroller = document.querySelector('.timeline') as HTMLElement;
  return {
    onLoadOlder,
    scroller,
    /** The control, or null while the timeline is following the tail. */
    button: () => screen.queryByRole('button', { name: /back to latest/i }),
    /** Re-render after growing the content, the way a new event would. */
    update: (next: TimelineState, height = scrollHeight) => {
      scrollHeight = height;
      act(() => view.rerender(element(next)));
    },
    scrollTo: (top: number) => {
      scroller.scrollTop = top;
      act(() => {
        scroller.dispatchEvent(new Event('scroll', { bubbles: true }));
      });
    },
  };
}

describe('back to latest', () => {
  it('is absent while the timeline follows the tail', () => {
    const h = setup(stateOf(notice(1, 'one')));
    expect(h.button()).toBeNull();
  });

  it('appears as soon as the reader scrolls away, with nothing new to show', () => {
    const h = setup(stateOf(notice(1, 'one')));
    h.scrollTo(300);

    const button = h.button();
    expect(button).not.toBeNull();
    // Round, arrow only: the count widens it into a capsule, and nothing else.
    expect(button?.textContent).toBe('');
    expect(button?.className).toBe('back-to-latest');
    expect(button).toHaveAttribute('title', 'Back to latest');
  });

  it('counts the blocks that land while the reader is away', () => {
    const h = setup(stateOf(notice(1, 'one')));
    h.scrollTo(300);

    h.update(stateOf(notice(1, 'one'), notice(2, 'two')), 1200);
    expect(h.button()).toHaveTextContent('1 new');
    expect(h.button()?.className).toContain('counted');

    h.update(stateOf(notice(1, 'one'), notice(2, 'two'), notice(3, 'three')), 1400);
    expect(h.button()).toHaveTextContent('2 new');
    // The words move into the accessible name; the face carries the count.
    expect(screen.getByRole('button', { name: 'Back to latest, 2 new' })).toBeInTheDocument();
  });

  it('returns to the bottom and clears the count when clicked', async () => {
    const user = userEvent.setup();
    const h = setup(stateOf(notice(1, 'one')));
    h.scrollTo(300);
    h.update(stateOf(notice(1, 'one'), notice(2, 'two')), 1200);
    expect(h.button()).toHaveTextContent('1 new');

    const button = h.button();
    if (!button) throw new Error('the control should be on screen');
    await user.click(button);

    expect(h.scroller.scrollTop).toBe(1200);
    expect(h.button()).toBeNull();
  });

  it('stays on screen when an older history page is prepended', () => {
    const h = setup(stateOf(notice(2, 'two'), notice(3, 'three')));
    h.scrollTo(300);
    expect(h.button()).not.toBeNull();

    const prepended = mergeHistory(stateOf(notice(2, 'two'), notice(3, 'three')), [
      notice(1, 'one'),
    ]);
    h.update(prepended, 1600);

    expect(h.button()).not.toBeNull();
    // A page that lands above the reader is not something they missed.
    expect(h.button()?.textContent).toBe('');
  });
});

/**
 * The count follows the detail level: a burst of tool calls the reader has
 * chosen not to see is nothing to come back to.
 */
describe('back to latest at Simple', () => {
  /** Each block in its own commit, the way the socket delivers them. */
  const arrive = (
    h: ReturnType<typeof setup>,
    start: TimelineState,
    events: SessionEvent[],
  ): TimelineState => {
    let state = start;
    let height = scrollHeight;
    for (const event of events) {
      state = applyEvent(state, event);
      height += 200;
      h.update(state, height);
    }
    return state;
  };

  const burst = (from: number, to: number): SessionEvent[] =>
    Array.from({ length: to - from + 1 }, (_, i) => toolCall(from + i));

  it('counts nothing for tool calls, and draws none of them', () => {
    useSettings.setState({ timelineDetail: 'simple' });
    const start = stateOf(notice(1, 'one'));
    const h = setup(start);
    h.scrollTo(300);
    expect(h.button()).not.toBeNull();

    const state = arrive(h, start, burst(2, 6));
    expect(document.querySelectorAll('.tool')).toHaveLength(0);
    expect(h.button()?.textContent).toBe('');

    // A message written to the reader still counts.
    arrive(h, state, [notice(7, 'two')]);
    expect(h.button()).toHaveTextContent('1 new');
  });

  it('counts one per block at Detailed', () => {
    const start = stateOf(notice(1, 'one'));
    const h = setup(start);
    h.scrollTo(300);

    arrive(h, start, burst(2, 4));
    expect(document.querySelectorAll('.tool')).toHaveLength(3);
    expect(h.button()).toHaveTextContent('3 new');
  });

  it('counts nothing for another agent’s words either (A34)', () => {
    useSettings.setState({ timelineDetail: 'simple' });
    const start = stateOf(notice(1, 'one'));
    const h = setup(start);
    h.scrollTo(300);

    const state = arrive(h, start, [agentMessage(2), agentMessage(3)]);
    expect(document.querySelectorAll('.agent-message')).toHaveLength(0);
    expect(h.button()?.textContent).toBe('');

    // What is written to the reader still counts.
    arrive(h, state, [notice(4, 'two')]);
    expect(h.button()).toHaveTextContent('1 new');
  });

  it('counts one per block for them at Detailed', () => {
    const start = stateOf(notice(1, 'one'));
    const h = setup(start);
    h.scrollTo(300);

    arrive(h, start, [agentMessage(2), agentMessage(3)]);
    expect(document.querySelectorAll('.agent-message')).toHaveLength(2);
    expect(h.button()).toHaveTextContent('2 new');
  });

  it('starts the count again when the level changes', () => {
    useSettings.setState({ timelineDetail: 'simple' });
    const h = setup(stateOf(notice(1, 'one')));
    h.scrollTo(300);
    h.update(stateOf(notice(1, 'one'), notice(2, 'two')), 1200);
    expect(h.button()).toHaveTextContent('1 new');

    act(() => useSettings.setState({ timelineDetail: 'detailed' }));
    expect(h.button()?.textContent).toBe('');
  });
});

/**
 * A35 — what a session the usage limit stopped draws in the transcript, and
 * what it deliberately does not (`docs/DESIGN.md` § "Paused by the usage
 * limit"). The Simple level keeps all of it: these are not the agent's
 * workings, they are what happened to the session.
 */
describe('a session the usage limit stopped', () => {
  const limitEnd = (seq: number, resetsAt: number | null): SessionEvent =>
    ({
      seq,
      ts: 4_000 + seq,
      kind: 'turn_completed',
      turn_id: 'limit-turn',
      stop_reason: 'error',
      duration_ms: 1_200,
      limit: { window_minutes: 300, resets_at: resetsAt },
    }) as SessionEvent;

  const resume = (seq: number, partial: Record<string, unknown>): SessionEvent =>
    ({ seq, ts: 5_000 + seq, kind: 'resume', ...partial }) as SessionEvent;

  const at = (h: number, m: number): number => new Date(2026, 8, 17, h, m).getTime();

  it('closes the turn at the usage limit and says when it resets', () => {
    setup(stateOf(limitEnd(1, at(15, 50))));
    expect(screen.getByText(/Ended at the usage limit/)).toHaveTextContent('resets');
  });

  it('says only that it ended when the vendor named no time', () => {
    setup(stateOf(limitEnd(1, null)));
    expect(screen.getByText('Ended at the usage limit')).toBeInTheDocument();
  });

  it("draws the device's steps in the notice voice", () => {
    setup(
      stateOf(
        resume(1, { status: 'scheduled', at: at(15, 50), estimated: false }),
        resume(2, { status: 'rescheduled', at: at(16, 20) }),
        resume(3, { status: 'cancelled', reason: 'you sent a message' }),
        resume(4, { status: 'dropped', reason: 'the terminal was closed' }),
      ),
    );

    expect(screen.getByText(/Resume scheduled for/)).toBeInTheDocument();
    expect(screen.getByText(/Resume moved to/)).toBeInTheDocument();
    expect(screen.getByText('Resume cancelled · you sent a message')).toBeInTheDocument();
    expect(screen.getByText('Not resumed · the terminal was closed')).toBeInTheDocument();
  });

  it('draws nothing at all for the moment of resuming', () => {
    const state = stateOf(resume(1, { status: 'fired' }));
    setup(state);

    // Not a row, and not a block the reducer is holding on to either.
    expect(state.order).toHaveLength(0);
    expect(document.querySelectorAll('.notice')).toHaveLength(0);
  });

  it('keeps every one of them at the Simple level', () => {
    useSettings.setState({ timelineDetail: 'simple' });
    setup(
      stateOf(
        limitEnd(1, at(15, 50)),
        resume(2, { status: 'scheduled', at: at(15, 50) }),
        {
          seq: 3,
          ts: 6_000,
          kind: 'user_message',
          block_id: 'u-resume',
          source: 'resume',
          text: 'The usage limit has reset. Continue where you left off.',
        } as SessionEvent,
      ),
    );

    expect(screen.getByText(/Ended at the usage limit/)).toBeInTheDocument();
    expect(screen.getByText(/Resume scheduled for/)).toBeInTheDocument();
    expect(screen.getByText('Sent for you after the limit reset')).toBeInTheDocument();
  });
});
