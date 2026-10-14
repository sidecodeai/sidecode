/**
 * Amendment A12 in the rows themselves: what a message looks like between
 * pressing Send and the device echoing it back.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { UserMessageRow } from '../src/features/chat/blocks/UserMessageRow';
import {
  UNCONFIRMED_AFTER_MS,
  addOptimistic,
  applyEvent,
  emptyTimeline,
  selectView,
  type TimelineState,
} from '../src/stores/timeline';
import type { SessionEvent, UserMessageEvent } from '../src/protocol/types';

const SENT_AT = 1_700_000_000_000;

const withPending = (): TimelineState =>
  addOptimistic(emptyTimeline(), {
    id: 'req-1',
    text: 'run the tests',
    attachments: [{ name: 'shot.png', mime: 'image/png', size: 120 }],
    at: SENT_AT,
  });

/** Render the last row of a timeline the way the Timeline component does. */
function renderLastRow(state: TimelineState) {
  const item = selectView(state, 'detailed').roots.at(-1);
  if (!item) throw new Error('no row to render');
  render(<UserMessageRow event={item.event as UserMessageEvent} pending={item.pending} />);
  return {
    chip: () => document.querySelector('.delivery-chip')?.textContent ?? null,
    bubble: () => document.querySelector('.user-bubble'),
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('the pending send bubble (A12)', () => {
  it('shows the text, its attachments and a quiet "Sending…" chip', () => {
    vi.useFakeTimers();
    vi.setSystemTime(SENT_AT + 200);
    const row = renderLastRow(withPending());

    expect(screen.getByText('run the tests')).toBeInTheDocument();
    expect(screen.getByText('1 attachment')).toBeInTheDocument();
    expect(row.chip()).toBe('Sending…');
    expect(row.bubble()?.className).toContain('pending');
  });

  it('calls the send unconfirmed after a minute with no device event', () => {
    vi.useFakeTimers();
    vi.setSystemTime(SENT_AT + UNCONFIRMED_AFTER_MS + 1);
    expect(renderLastRow(withPending()).chip()).toBe('Delivery unconfirmed');
  });

  it('says a steered message is waiting for the agent, however long it takes (A14)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(SENT_AT + UNCONFIRMED_AFTER_MS * 5);
    const steered = addOptimistic(emptyTimeline(), {
      id: 'req-2',
      text: '有什么项目',
      attachments: [],
      at: SENT_AT,
      accepted: 'steered',
    });
    expect(renderLastRow(steered).chip()).toBe('the agent will read it at its next step');
  });

  it('drops the chip and the dimming when the device confirms the block', () => {
    const confirmed = applyEvent(withPending(), {
      seq: 12,
      ts: SENT_AT + 350,
      kind: 'user_message',
      block_id: 'req-1',
      text: 'run the tests',
      source: 'remote',
    } as SessionEvent);

    expect(confirmed.optimistic).toEqual([]);
    const row = renderLastRow(confirmed);
    expect(screen.getByText('run the tests')).toBeInTheDocument();
    expect(row.chip()).toBeNull();
    expect(row.bubble()?.className).not.toContain('pending');
  });
});
