/**
 * A29 — the pure half of dictation polish: the conversation the model is given,
 * and the replacement that touches the dictated words and nothing else.
 * `docs/DESIGN.md` § "Polishing what you dictated".
 */
import { describe, expect, it } from 'vitest';
import {
  CONTEXT_LIMIT,
  CONTEXT_TEXT_LIMIT,
  applyPolished,
  canPolish,
  dictatedDraft,
  polishContext,
  polishRequest,
  polishedDraft,
  undoPolished,
} from '../src/features/voice/polish';
import { applyEvent, addOptimistic, emptyTimeline } from '../src/stores/timeline';
import type { TimelineState } from '../src/stores/timeline';
import type { SessionEvent } from '../src/protocol/types';

let seq = 0;

const said = (text: string): SessionEvent => ({
  kind: 'user_message',
  seq: (seq += 1),
  ts: seq,
  block_id: `user-${seq}`,
  text,
  source: 'remote',
});

const answered = (text: string, done = true): SessionEvent => ({
  kind: 'assistant_text',
  seq: (seq += 1),
  ts: seq,
  block_id: `asst-${seq}`,
  text,
  done,
});

const thought = (text: string): SessionEvent => ({
  kind: 'thinking',
  seq: (seq += 1),
  ts: seq,
  block_id: `think-${seq}`,
  text,
  done: true,
});

const ran = (): SessionEvent => ({
  kind: 'tool_call',
  seq: (seq += 1),
  ts: seq,
  block_id: `tool-${seq}`,
  tool: 'Bash',
  tool_kind: 'shell',
  title: 'npm test',
  status: 'succeeded',
  started_at: seq,
});

const timelineOf = (...events: SessionEvent[]): TimelineState =>
  events.reduce(applyEvent, emptyTimeline());

describe('the conversation a polish request carries', () => {
  it('takes the user and assistant messages, oldest first, and nothing else', () => {
    const timeline = timelineOf(
      said('Make the session status dot pulse while a turn is running.'),
      thought('The dot is drawn by StatusDot.'),
      ran(),
      answered('Done: the dot now breathes while the state is running.'),
    );

    expect(polishContext(timeline)).toEqual([
      { role: 'user', text: 'Make the session status dot pulse while a turn is running.' },
      { role: 'assistant', text: 'Done: the dot now breathes while the state is running.' },
    ]);
  });

  it('keeps the last twenty messages, which are the ones nearest the dictation', () => {
    const events: SessionEvent[] = [];
    for (let i = 0; i < 15; i += 1) {
      events.push(said(`ask ${i}`), answered(`answer ${i}`));
    }
    const context = polishContext(timelineOf(...events));

    expect(context).toHaveLength(CONTEXT_LIMIT);
    expect(context[0]).toEqual({ role: 'user', text: 'ask 5' });
    expect(context[CONTEXT_LIMIT - 1]).toEqual({ role: 'assistant', text: 'answer 14' });
  });

  it('trims a long message to what the contract allows, and drops empty ones', () => {
    const timeline = timelineOf(said('x'.repeat(CONTEXT_TEXT_LIMIT + 500)), answered('   '));
    const context = polishContext(timeline);

    expect(context).toHaveLength(1);
    expect(context[0]?.text).toHaveLength(CONTEXT_TEXT_LIMIT);
  });

  it('counts a message that is still on its way to the device (A12)', () => {
    const timeline = addOptimistic(timelineOf(answered('Which test?')), {
      id: 'req-1',
      text: 'The auth refresh one.',
      attachments: [],
      at: 1,
    });

    expect(polishContext(timeline)).toEqual([
      { role: 'assistant', text: 'Which test?' },
      { role: 'user', text: 'The auth refresh one.' },
    ]);
  });

  it('builds the body of POST /api/polish, with the language the gateway detected (A44)', () => {
    const body = polishRequest(
      { base: 'note: ', dictated: 'um the green blinking thing' },
      { model: 'gpt-4.1-mini', strength: 'strong' },
      [{ role: 'user', text: 'Make the dot pulse.' }],
    );

    expect(body).toEqual({
      text: 'um the green blinking thing',
      model: 'gpt-4.1-mini',
      strength: 'strong',
      language: 'auto',
      context: [{ role: 'user', text: 'Make the dot pulse.' }],
    });
  });

  it('refuses a dictation that is empty or longer than the contract allows', () => {
    expect(canPolish('   ')).toBe(false);
    expect(canPolish('a'.repeat(8192))).toBe(true);
    expect(canPolish('a'.repeat(8193))).toBe(false);
  });
});

describe('what the answer does to the draft', () => {
  const span = { base: 'after lunch', dictated: 'um run the the auth suite' };

  it('replaces the dictated span and leaves what was typed before it', () => {
    expect(dictatedDraft(span)).toBe('after lunch um run the the auth suite');

    const next = applyPolished(dictatedDraft(span), span, 'Run the auth suite.');

    expect(next).toBe('after lunch Run the auth suite.');
    expect(next).toBe(polishedDraft(span, 'Run the auth suite.'));
  });

  it('trims the whitespace and reads an empty answer as no answer', () => {
    expect(applyPolished(dictatedDraft(span), span, '  Run the auth suite.\n')).toBe(
      'after lunch Run the auth suite.',
    );
    expect(applyPolished(dictatedDraft(span), span, '   ')).toBeNull();
  });

  it('leaves a field the person has moved on alone', () => {
    expect(applyPolished('something else entirely', span, 'Run the auth suite.')).toBeNull();
  });

  it('says nothing happened when the model returned the words it was given', () => {
    expect(applyPolished(dictatedDraft(span), span, span.dictated)).toBeNull();
  });

  it('puts the dictated words back on undo, and only from the polished draft', () => {
    const polished = polishedDraft(span, 'Run the auth suite.');

    expect(undoPolished(polished, span, 'Run the auth suite.')).toBe(dictatedDraft(span));
    expect(undoPolished('edited since', span, 'Run the auth suite.')).toBeNull();
  });
});
