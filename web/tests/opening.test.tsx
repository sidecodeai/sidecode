/**
 * `docs/DESIGN.md` § "Reading position": a conversation opens on the
 * person's own message, placed once its rows are current, looks through at most
 * two older pages for it, and keeps where it was being read as it is left. The
 * page keys the transcript by conversation, so a move to another one starts the
 * scroll and the follow state again.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router';
import { Timeline } from '../src/features/chat/Timeline';
import { ChatPage } from '../src/features/chat/ChatPage';
import { readPosition, savePosition } from '../src/features/chat/readingPosition';
import { useAuth } from '../src/stores/auth';
import { useChat, type ChatSession } from '../src/stores/chat';
import { useConnection } from '../src/stores/connection';
import { useDevices } from '../src/stores/devices';
import { useOutbox } from '../src/stores/outbox';
import { keyOf, useSessions } from '../src/stores/sessions';
import { useSettings } from '../src/stores/settings';
import { applyEvent, emptyTimeline, type TimelineState } from '../src/stores/timeline';
import { claudeAgent, devices as deviceFixtures } from '../mock/fixtures';
import type { Session, SessionEvent } from '../src/protocol/types';

vi.mock('../src/lib/gateway', () => ({
  rpc: vi.fn(() => new Promise(() => undefined)),
  getSocket: () => null,
  setSocket: () => undefined,
}));

// jsdom lays nothing out: every row is 100 px tall, stacked from the top in the
// order drawn, and the transcript shows 400 px of it.
const CLIENT_HEIGHT = 400;
const ROW_HEIGHT = 100;
const original = {
  scrollHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight'),
  clientHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight'),
  scrollTo: HTMLElement.prototype.scrollTo,
  rect: HTMLElement.prototype.getBoundingClientRect,
};

const scroller = () => document.querySelector<HTMLElement>('.timeline');
const rowsDrawn = () => [...document.querySelectorAll<HTMLElement>('.timeline-row')];

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', {
    get(this: HTMLElement) {
      return this.classList.contains('timeline') ? rowsDrawn().length * ROW_HEIGHT : 0;
    },
    configurable: true,
  });
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    get: () => CLIENT_HEIGHT,
    configurable: true,
  });
  HTMLElement.prototype.scrollTo = function scrollToStub(this: HTMLElement, options?: unknown) {
    if (options && typeof options === 'object' && 'top' in options) {
      const end = Math.max(0, this.scrollHeight - CLIENT_HEIGHT);
      this.scrollTop = Math.min(end, (options as ScrollToOptions).top ?? 0);
    }
  } as HTMLElement['scrollTo'];
  HTMLElement.prototype.getBoundingClientRect = function rect(this: HTMLElement) {
    const list = scroller();
    const index = rowsDrawn().findIndex((row) => row.firstElementChild === this);
    const top = this === list ? 0 : index * ROW_HEIGHT - (list?.scrollTop ?? 0);
    const height = this === list ? CLIENT_HEIGHT : ROW_HEIGHT;
    return { top, bottom: top + height, left: 0, right: 0, width: 0, height, x: 0, y: top } as DOMRect;
  };
});

afterAll(() => {
  if (original.scrollHeight) {
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', original.scrollHeight);
  }
  if (original.clientHeight) {
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', original.clientHeight);
  }
  HTMLElement.prototype.scrollTo = original.scrollTo;
  HTMLElement.prototype.getBoundingClientRect = original.rect;
});

let stored: Map<string, string>;

beforeEach(() => {
  stored = new Map();
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => void stored.set(key, value),
    removeItem: (key: string) => void stored.delete(key),
  });
  useSettings.setState({ timelineDetail: 'detailed' });
  useAuth.setState({ username: 'me' });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  useAuth.setState({ username: null });
});

const said = (seq: number): SessionEvent =>
  ({ seq, ts: seq, kind: 'assistant_text', block_id: `a${seq}`, text: `answer ${seq}`, done: true }) as SessionEvent;

const asked = (seq: number): SessionEvent =>
  ({ seq, ts: seq, kind: 'user_message', block_id: `u${seq}`, text: `question ${seq}`, source: 'remote' }) as SessionEvent;

const stateOf = (...events: SessionEvent[]): TimelineState => events.reduce(applyEvent, emptyTimeline());

/** Twelve rows: the person asked at 3 and at 6, and the agent answered at length. */
const conversation = () =>
  stateOf(said(1), said(2), asked(3), said(4), said(5), asked(6), ...[7, 8, 9, 10, 11, 12].map(said));

function show(
  timeline: TimelineState,
  options: { current?: boolean; historyHasMore?: boolean; pagesAsked?: number } = {},
) {
  const onLoadOlder = vi.fn();
  const element = (next: TimelineState, current = options.current ?? true) => (
    <Timeline
      sessionKey="dev-mac/ses-1"
      current={current}
      pagesAsked={options.pagesAsked ?? 1}
      timeline={next}
      historyLoading={false}
      historyHasMore={options.historyHasMore ?? false}
      historyLoaded
      repairing={false}
      error={null}
      onRetry={vi.fn()}
      onOpenFull={vi.fn().mockResolvedValue(undefined)}
      onApprove={vi.fn().mockResolvedValue(undefined)}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onLoadOlder={onLoadOlder}
    />
  );
  const view = render(element(timeline));
  return {
    onLoadOlder,
    view,
    becomeCurrent: () => act(() => view.rerender(element(timeline, true))),
  };
}

const backToLatest = () => screen.queryByRole('button', { name: /back to latest/i });

describe('where a conversation opens', () => {
  it('opens on the person’s newest message, its top just under the top of the transcript', () => {
    show(conversation());
    // The sixth row is at 500 px; the margin above it is 12 px.
    expect(scroller()?.scrollTop).toBe(488);
    expect(backToLatest()).not.toBeNull();
  });

  it('opens on their message nearest above where they were reading', () => {
    // They read the answer to their first question, up to row 5, and left.
    savePosition('me', 'dev-mac/ses-1', { seq: 5, held: 12 });
    show(conversation());
    // Their message at 3 is the third row, at 200 px.
    expect(scroller()?.scrollTop).toBe(188);
  });

  it('opens on their newest message when they wrote since they left', () => {
    // They left with events up to 5; the question at 6 came from elsewhere since.
    savePosition('me', 'dev-mac/ses-1', { seq: 4, held: 5 });
    show(conversation());
    expect(scroller()?.scrollTop).toBe(488);
  });

  it('places nothing before the rows are this open’s', () => {
    const h = show(conversation(), { current: false });
    expect(scroller()?.scrollTop).toBe(800);

    h.becomeCurrent();

    expect(scroller()?.scrollTop).toBe(488);
  });

  it('looks in an older page when none of the person’s messages is held', () => {
    const { onLoadOlder } = show(stateOf(said(40), said(41)), { historyHasMore: true });
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('opens on the tail once the newest page and two older ones hold none', () => {
    const { onLoadOlder } = show(stateOf(said(40), said(41), said(42), said(43), said(44)), {
      historyHasMore: true,
      pagesAsked: 3,
    });
    expect(onLoadOlder).not.toHaveBeenCalled();
    expect(scroller()?.scrollTop).toBe(100);
    expect(backToLatest()).toBeNull();
  });

  it('stops looking once the reader has scrolled for themselves', () => {
    vi.useFakeTimers();
    try {
      const { onLoadOlder, view } = show(stateOf(said(40), said(41)), {
        historyHasMore: true,
        current: false,
      });
      act(() => {
        scroller()?.dispatchEvent(new Event('wheel'));
      });
      view.rerender(
        <Timeline
          sessionKey="dev-mac/ses-1"
          current
          pagesAsked={1}
          timeline={stateOf(said(40), said(41))}
          historyLoading={false}
          historyHasMore
          historyLoaded
          repairing={false}
          error={null}
          onRetry={vi.fn()}
          onOpenFull={vi.fn().mockResolvedValue(undefined)}
          onApprove={vi.fn().mockResolvedValue(undefined)}
          onAnswer={vi.fn().mockResolvedValue(undefined)}
          onLoadOlder={onLoadOlder}
        />,
      );
      expect(onLoadOlder).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('the reading position', () => {
  it('keeps the lowest row on screen, and the newest event held, when the conversation is left', () => {
    const { view } = show(conversation());
    // Placed at 488: rows 6 to 9 have their tops inside the 400 px shown.
    view.unmount();
    expect(readPosition('me', 'dev-mac/ses-1')).toEqual({ seq: 9, held: 12 });
  });

  it('is kept for nobody when nobody is signed in', () => {
    useAuth.setState({ username: null });
    const { view } = show(conversation());
    view.unmount();
    expect(stored.size).toBe(0);
  });
});

/**
 * `docs/DESIGN.md` § "Reading position": the timeline is keyed by conversation
 * like the header and the composer, so moving to another one in the sidebar
 * does not carry the scroll or the follow state across.
 */
describe('moving to another conversation', () => {
  const device = { ...deviceFixtures[0]!, device_id: 'dev-mac', agents: [claudeAgent] };
  const base: Session = {
    session_id: 'ses-a',
    device_id: 'dev-mac',
    agent: 'claude',
    title: 'First conversation',
    cwd: '/Users/me/dev/a',
    git: null,
    state: 'idle',
    state_detail: null,
    origin: 'remote',
    control: 'remote',
    model: 'claude-sonnet-4-5',
    permission_mode: 'default',
    effort: 'high',
    created_at: 1,
    updated_at: 2,
    last_seq: 12,
    archived: false,
    turn: null,
    todos: null,
    usage: null,
    queued: 0,
  };
  const other: Session = { ...base, session_id: 'ses-b', title: 'Second conversation', updated_at: 1 };

  const chatOf = (session: Session, timeline: TimelineState): ChatSession => ({
    key: keyOf(session),
    deviceId: session.device_id,
    sessionId: session.session_id,
    timeline,
    todos: [],
    todosSeq: 0,
    queue: [],
    queueSeq: 0,
    usage: null,
    subscription: 'live',
    historyLoading: false,
    historyHasMore: false,
    historyLoaded: true,
    pagesAsked: 1,
    repairing: false,
    generation: 0,
    error: null,
    closedSeq: null,
  });

  it('opens the next one at its own place, not at the one left', async () => {
    const answers = stateOf(...[1, 2, 3, 4, 5, 6, 7, 8].map(said));
    useDevices.setState({ devices: [device], loaded: true, updateErrors: {} });
    useSessions.setState({
      sessions: { [keyOf(base)]: base, [keyOf(other)]: other },
      loaded: true,
      agentFilter: null,
    });
    useChat.setState({
      sessions: { [keyOf(base)]: chatOf(base, answers), [keyOf(other)]: chatOf(other, answers) },
    });
    useOutbox.setState({ pending: {} });
    useConnection.setState({ status: 'closed' });

    render(
      <MemoryRouter initialEntries={['/sessions/dev-mac/ses-a']}>
        <Routes>
          <Route path="/sessions/:deviceId/:sessionId" element={<ChatPage />} />
        </Routes>
      </MemoryRouter>,
    );
    // The reader scrolls up in the first conversation.
    const list = scroller();
    if (!list) throw new Error('no transcript');
    act(() => {
      list.dispatchEvent(new Event('wheel'));
    });
    list.scrollTop = 0;
    act(() => {
      list.dispatchEvent(new Event('scroll', { bubbles: true }));
    });
    expect(backToLatest()).not.toBeNull();

    await userEvent.click(screen.getByRole('button', { name: /Second conversation/ }));

    // A transcript of its own, at its tail and following it.
    expect(scroller()).not.toBe(list);
    expect(scroller()?.scrollTop).toBe(400);
    expect(backToLatest()).toBeNull();
  });
});
