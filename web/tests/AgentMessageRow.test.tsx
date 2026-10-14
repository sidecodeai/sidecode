/**
 * A34 — words another agent put into the conversation: a teammate session's
 * report, a background task's notification. `docs/DESIGN.md` § "The timeline":
 * they are the agent's side of the conversation, not the person's, so they sit
 * on the left as a muted block captioned "from another agent" and never in the
 * gray bubble on the right; and they are the agent's workings, so Simple does
 * not draw them.
 */
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { AgentMessageRow } from '../src/features/chat/blocks/AgentMessageRow';
import { Timeline } from '../src/features/chat/Timeline';
import { useSettings } from '../src/stores/settings';
import { applyEvent, emptyTimeline, selectView, type TimelineState } from '../src/stores/timeline';
import { en } from '../src/strings';
import { zhHans } from '../src/strings.zh-Hans';
import type { SessionEvent, UserMessageEvent } from '../src/protocol/types';
import { fixturesAvailable, readFixture } from './fixtures';

// jsdom has no scrolling, and the timeline scrolls itself to the latest row.
const nativeScrollTo = HTMLElement.prototype.scrollTo;

beforeAll(() => {
  HTMLElement.prototype.scrollTo = function scrollToStub(this: HTMLElement) {
    // Nothing to scroll: this suite is about which row is drawn.
  } as HTMLElement['scrollTo'];
});

afterAll(() => {
  HTMLElement.prototype.scrollTo = nativeScrollTo;
});

beforeEach(() => useSettings.setState({ timelineDetail: 'detailed', language: 'en' }));

afterEach(() => {
  cleanup();
  useSettings.setState({ language: 'en' });
});

const REPORT = 'recon-ios: Recon complete. Three call sites still read the old cursor.';

const agentMessage = (seq = 3, text = REPORT): UserMessageEvent => ({
  seq,
  ts: 1_788_944_409_000 + seq,
  kind: 'user_message',
  block_id: `blk-${seq}`,
  text,
  source: 'agent',
});

const typed = (seq: number, text: string): SessionEvent =>
  ({
    seq,
    ts: 1_788_944_409_000 + seq,
    kind: 'user_message',
    block_id: `u${seq}`,
    source: 'remote',
    text,
  }) as SessionEvent;

const prose = (seq: number, text: string): SessionEvent =>
  ({ seq, ts: 1_788_944_409_000 + seq, kind: 'assistant_text', block_id: `a${seq}`, text, done: true }) as SessionEvent;

const stateOf = (...events: SessionEvent[]): TimelineState => events.reduce(applyEvent, emptyTimeline());

const renderTimeline = (timeline: TimelineState) =>
  render(
    <Timeline
      timeline={timeline}
      historyLoading={false}
      historyHasMore={false}
      onOpenFull={vi.fn().mockResolvedValue(undefined)}
      onApprove={vi.fn().mockResolvedValue(undefined)}
      onAnswer={vi.fn().mockResolvedValue(undefined)}
      onLoadOlder={vi.fn()}
    />,
  );

describe('the row another agent’s words are drawn in', () => {
  it('captions the block and prints the report as it arrived', () => {
    render(<AgentMessageRow event={agentMessage()} />);

    const block = document.querySelector('.agent-message');
    expect(block).not.toBeNull();
    expect(screen.getByText(en.chat.fromAgent)).toBeInTheDocument();
    expect(screen.getByText(REPORT)).toBeInTheDocument();
    // The caption sits above the text, inside the same block.
    expect(block?.firstElementChild?.className).toBe('agent-message-origin');
  });

  it('says the caption in the interface language', () => {
    useSettings.setState({ language: 'zh-Hans' });
    render(<AgentMessageRow event={agentMessage()} />);

    expect(screen.getByText(zhHans.chat.fromAgent)).toBeInTheDocument();
    // The report itself is the device's words and is never translated.
    expect(screen.getByText(REPORT)).toBeInTheDocument();
  });

  it('is what the timeline draws for such a message, never the person’s bubble', () => {
    renderTimeline(stateOf(typed(1, 'fix it'), agentMessage(2)));

    expect(document.querySelectorAll('.agent-message')).toHaveLength(1);
    // One bubble, and it holds the message the person actually typed.
    const bubbles = [...document.querySelectorAll('.user-bubble')];
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0]?.textContent).toContain('fix it');
    expect(bubbles[0]?.textContent).not.toContain(REPORT);
    expect(document.querySelector('.user-bubble.from-agent')).toBeNull();
  });

  it('sits on the agent’s side, at the full width of the content', () => {
    renderTimeline(stateOf(agentMessage(1), prose(2, 'taking the three call sites on')));

    const block = document.querySelector('.agent-message');
    // `.user-row` is the flex row that pushes a bubble to the right; the block
    // is a root of the timeline instead, as the agent's prose is.
    expect(block?.closest('.user-row')).toBeNull();
    expect(block?.parentElement?.className).toBe('timeline-inner');
  });

  it.runIf(fixturesAvailable())('draws the frozen fixture the device sends', () => {
    const event = readFixture<UserMessageEvent>('events/user_message.agent.json');
    expect(event.source).toBe('agent');
    render(<AgentMessageRow event={event} />);

    expect(screen.getByText(en.chat.fromAgent)).toBeInTheDocument();
    expect(screen.getByText(event.text)).toBeInTheDocument();
    expect(document.querySelector('.user-bubble')).toBeNull();
  });
});

describe('the detail level it follows', () => {
  const conversation = (): TimelineState =>
    stateOf(typed(1, 'fix it'), agentMessage(2), prose(3, 'on it'));

  it('draws it at Detailed', () => {
    expect(selectView(conversation(), 'detailed').roots.map((item) => item.key)).toEqual([
      'u1',
      'blk-2',
      'a3',
    ]);
  });

  it('drops it at Simple, with the agent’s other workings', () => {
    expect(selectView(conversation(), 'simple').roots.map((item) => item.key)).toEqual(['u1', 'a3']);
  });

  it('still draws the person’s own message at Simple', () => {
    useSettings.setState({ timelineDetail: 'simple' });
    renderTimeline(conversation());

    expect(document.querySelector('.agent-message')).toBeNull();
    expect(screen.getByText('fix it')).toBeInTheDocument();
    expect(screen.queryByText(REPORT)).toBeNull();
  });
});
