import { describe, expect, it } from 'vitest';
import { appendDelta, appendSent, type Entry } from '../../src/features/live/transcript';

const turn = (speaker: 'user' | 'assistant', text: string): Entry => ({ kind: 'turn', speaker, text });

describe('the transcript builder', () => {
  it('joins the fragments of one speaker into one turn', () => {
    let entries: Entry[] = [];
    entries = appendDelta(entries, 'user', 'What is ');
    entries = appendDelta(entries, 'user', 'running?');
    expect(entries).toEqual([turn('user', 'What is running?')]);
  });

  it('starts a new turn when the speaker changes, and keeps every turn of a long call', () => {
    let entries: Entry[] = [];
    entries = appendDelta(entries, 'user', 'What is running?');
    entries = appendDelta(entries, 'assistant', 'Two sessions.');
    entries = appendDelta(entries, 'user', 'Thanks');
    entries = appendDelta(entries, 'assistant', ' You are welcome.');
    expect(entries).toEqual([
      turn('user', 'What is running?'),
      turn('assistant', 'Two sessions.'),
      turn('user', 'Thanks'),
      turn('assistant', 'You are welcome.'),
    ]);
  });

  it('keeps the space between the words of one turn, and drops the one that opens it', () => {
    let entries = appendDelta([], 'assistant', ' Hello');
    entries = appendDelta(entries, 'assistant', ' there');
    expect(entries).toEqual([turn('assistant', 'Hello there')]);
  });

  it('opens no turn for a fragment with nothing in it', () => {
    expect(appendDelta([], 'user', '')).toEqual([]);
    expect(appendDelta([], 'user', '  ')).toEqual([]);
    const entries = appendDelta([], 'user', 'Hi');
    expect(appendDelta(entries, 'user', '')).toBe(entries);
  });

  it('does not change the list it was given', () => {
    const before = appendDelta([], 'user', 'Hi');
    appendDelta(before, 'user', ' there');
    appendDelta(before, 'assistant', 'Hello');
    expect(before).toEqual([turn('user', 'Hi')]);
  });

  it('adds a sent message naming its session, and the next words start a new turn', () => {
    let entries: Entry[] = [];
    entries = appendDelta(entries, 'assistant', 'Sending it now.');
    entries = appendSent(entries, 'Billing', 'Skip the migration tests');
    entries = appendDelta(entries, 'assistant', 'Done.');
    expect(entries).toEqual([
      turn('assistant', 'Sending it now.'),
      { kind: 'sent', session: 'Billing', text: 'Skip the migration tests' },
      turn('assistant', 'Done.'),
    ]);
  });
});
