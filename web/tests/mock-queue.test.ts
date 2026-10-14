/**
 * A43 — the queue rule the mock device applies (PROTOCOL §6.3), so what
 * `npm run dev:mock` does with an edited message can be read without a socket:
 * a queue is in `ts` order with no two entries sharing one, a message sent back
 * with `queue_ts` goes in front of the first entry with a greater `ts`, and any
 * other message joins the end.
 */
import { describe, expect, it } from 'vitest';
import { filesField, heldTs, hold, readQueueTs } from '../mock/queue';
import { queueFor, sessions } from '../mock/fixtures';

interface Entry {
  id: string;
  text: string;
  ts: number;
}

const entry = (id: string, ts: number): Entry => ({ id, text: id, ts });
const ids = (queue: readonly Entry[]) => queue.map((item) => item.id);

/** Queue `id` the way the mock's `session.send` does. */
const send = (queue: Entry[], id: string, now: number, queueTs?: number): Entry[] =>
  hold(queue, entry(id, heldTs(queue, queueTs, now)));

describe('queue_ts', () => {
  it('is absent, a non-negative integer, or refused', () => {
    expect(readQueueTs(undefined)).toBeUndefined();
    expect(readQueueTs(null)).toBeUndefined();
    expect(readQueueTs(0)).toBe(0);
    expect(readQueueTs(1_788_945_028_800)).toBe(1_788_945_028_800);
    for (const bad of [-1, '12', 1.5, true, Number.NaN, {}]) expect(readQueueTs(bad)).toBeNull();
  });
});

describe('holding a message', () => {
  it('puts a message without queue_ts at the end, now or one past the last entry', () => {
    expect(heldTs([], undefined, 500)).toBe(500);
    expect(heldTs([entry('a', 100)], undefined, 500)).toBe(500);
    expect(heldTs([entry('a', 900)], undefined, 500)).toBe(901);
  });

  it('gives messages queued in one millisecond distinct, rising ts', () => {
    let queue = send([], 'one', 500);
    queue = send(queue, 'two', 500);
    queue = send(queue, 'three', 500);
    expect(queue.map((item) => item.ts)).toEqual([500, 501, 502]);
  });

  it('puts a message sent back with queue_ts in front of the first greater ts', () => {
    const queue = [entry('a', 100), entry('c', 300)];
    expect(ids(hold(queue, entry('front', 50)))).toEqual(['front', 'a', 'c']);
    expect(ids(hold(queue, entry('middle', 200)))).toEqual(['a', 'middle', 'c']);
    expect(ids(hold(queue, entry('end', 400)))).toEqual(['a', 'c', 'end']);
    // After an entry with the same ts, never in front of it.
    expect(ids(hold(queue, entry('same', 100)))).toEqual(['a', 'same', 'c']);
    expect(ids(hold([], entry('only', 100)))).toEqual(['only']);
  });

  it('brings an edit back to its place however the queue moved meanwhile', () => {
    let queue = send([], 'A', 500);
    queue = send(queue, 'B', 500);
    queue = send(queue, 'C', 500);
    const b = queue[1]!;
    // B is taken out to be edited, and D is queued while it is.
    queue = queue.filter((item) => item.id !== 'B');
    queue = send(queue, 'D', 700);
    queue = send(queue, "B'", 900, b.ts);
    expect(ids(queue)).toEqual(['A', "B'", 'C', 'D']);
  });
});

describe('what the snapshot says', () => {
  it('counts the files of a message that carries some, and says nothing otherwise', () => {
    expect(filesField([{ name: 'a.png' }, { name: 'b.png' }])).toEqual({ attachments: 2 });
    expect(filesField([])).toEqual({});
    expect(filesField(undefined)).toEqual({});
  });

  it('starts the session waiting on an approval with three messages, one with files', () => {
    const waiting = sessions.find((s) => s.session_id === 'ses-vite');
    expect(waiting?.state).toBe('needs_approval');
    const queue = queueFor('ses-vite');
    expect(queue).toHaveLength(3);
    expect(queue.map((item) => item.ts)).toEqual([...queue.map((item) => item.ts)].sort((a, b) => a - b));
    expect(queue.map((item) => item.attachments)).toEqual([undefined, 2, undefined]);
    expect(queueFor('ses-flaky')).toEqual([]);
  });
});
