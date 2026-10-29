import { describe, expect, it } from 'vitest';
import {
  MAX_TIMELINE_ITEMS,
  UNCONFIRMED_AFTER_MS,
  addOptimistic,
  applyEvent,
  applyEvents,
  beginRepair,
  coverThrough,
  dropQueued,
  emptyTimeline,
  hasGap,
  isUnconfirmed,
  keepOptimistic,
  mergeHistory,
  mergeMissed,
  mergeNewestPage,
  removeOptimistic,
  reopenGap,
  replaceBlock,
  selectView,
  type OptimisticBlock,
  type TimelineState,
} from '../src/stores/timeline';
import type { SessionEvent } from '../src/protocol/types';
import { fixturesAvailable, readFixture } from './fixtures';

const text = (seq: number, delta: string, done = false): SessionEvent =>
  ({ seq, ts: 1_000 + seq, kind: 'assistant_text', block_id: 'b1', delta, done }) as SessionEvent;

const tool = (seq: number, status: 'running' | 'succeeded', output: string): SessionEvent =>
  ({
    seq,
    ts: 2_000 + seq,
    kind: 'tool_call',
    block_id: 't1',
    tool: 'Bash',
    tool_kind: 'shell',
    title: 'pytest',
    status,
    started_at: 2_000,
    output,
  }) as SessionEvent;

describe('timeline reducer', () => {
  it('appends streaming deltas into one block', () => {
    let state = emptyTimeline();
    state = applyEvent(state, text(1, 'Hello '));
    state = applyEvent(state, text(2, 'world'));
    expect(state.order).toEqual(['b1']);
    const event = state.items.b1?.event;
    expect(event?.kind).toBe('assistant_text');
    expect(event && 'text' in event ? event.text : null).toBe('Hello world');
    expect(state.lastSeq).toBe(2);
  });

  it('replaces the block when a final event carries the full text', () => {
    let state = emptyTimeline();
    state = applyEvent(state, text(1, 'partial'));
    state = applyEvent(state, {
      seq: 2,
      ts: 1_002,
      kind: 'assistant_text',
      block_id: 'b1',
      text: 'the complete answer',
      done: true,
    } as SessionEvent);
    const event = state.items.b1?.event;
    expect(event && 'text' in event ? event.text : null).toBe('the complete answer');
    expect(event && 'done' in event ? event.done : null).toBe(true);
    expect(state.order).toHaveLength(1);
  });

  it('fully replaces a tool_call block and keeps its original position', () => {
    let state = emptyTimeline();
    state = applyEvent(state, text(1, 'before'));
    state = applyEvent(state, tool(2, 'running', 'partial output'));
    state = applyEvent(state, text(3, ' after'));
    state = applyEvent(state, tool(4, 'succeeded', 'final output'));
    expect(state.order).toEqual(['b1', 't1']);
    const event = state.items.t1?.event;
    expect(event?.kind === 'tool_call' && event.status).toBe('succeeded');
    expect(event?.kind === 'tool_call' && event.output).toBe('final output');
    expect(state.items.t1?.seq).toBe(2);
  });

  it('drops late and duplicate frames by seq', () => {
    let state = emptyTimeline();
    state = applyEvent(state, text(5, 'kept'));
    const before = state;
    state = applyEvent(state, text(5, 'duplicate'));
    state = applyEvent(state, text(3, 'late'));
    expect(state).toBe(before);
    const event = state.items.b1?.event;
    expect(event && 'text' in event ? event.text : null).toBe('kept');
  });

  it('keeps state-only kinds out of the timeline', () => {
    let state = emptyTimeline();
    state = applyEvents(state, [
      { seq: 1, ts: 1, kind: 'status', state: 'running' },
      { seq: 2, ts: 2, kind: 'meta', title: 'Renamed' },
      { seq: 3, ts: 3, kind: 'queue', pending: [] },
      { seq: 4, ts: 4, kind: 'todos', items: [] },
      { seq: 5, ts: 5, kind: 'turn_started', turn_id: 'x', trigger: 'remote' },
      { seq: 6, ts: 6, kind: 'turn_completed', turn_id: 'x', stop_reason: 'completed', duration_ms: 1 },
    ] as SessionEvent[]);
    expect(state.order).toEqual([]);
    expect(state.lastSeq).toBe(6);
  });

  it('renders interrupted turns but not clean ones', () => {
    let state = emptyTimeline();
    state = applyEvent(state, {
      seq: 1,
      ts: 1,
      kind: 'turn_completed',
      turn_id: 'x',
      stop_reason: 'interrupted',
      duration_ms: 5,
    } as SessionEvent);
    expect(state.order).toHaveLength(1);
  });

  it('prepends an older history page without disturbing live blocks', () => {
    let state = emptyTimeline();
    state = applyEvent(state, text(10, 'live'));
    state = mergeHistory(state, [
      { seq: 4, ts: 4, kind: 'user_message', block_id: 'h1', text: 'older', source: 'remote' },
      { seq: 5, ts: 5, kind: 'assistant_text', block_id: 'h2', text: 'reply', done: true },
    ] as SessionEvent[]);
    expect(state.order).toEqual(['h1', 'h2', 'b1']);
    expect(state.oldestSeq).toBe(4);
    expect(state.lastSeq).toBe(10);
  });

  it('never lets a history page overwrite a fresher live block', () => {
    let state = emptyTimeline();
    state = applyEvent(state, tool(9, 'succeeded', 'final output'));
    state = mergeHistory(state, [tool(2, 'running', 'stale output')]);
    const event = state.items.t1?.event;
    expect(event?.kind === 'tool_call' && event.output).toBe('final output');
    expect(state.order).toEqual(['t1']);
  });

  it('nests events that carry parent_block_id under their parent row', () => {
    let state = emptyTimeline();
    state = applyEvent(state, {
      seq: 1,
      ts: 1,
      kind: 'tool_call',
      block_id: 'parent',
      tool: 'Task',
      tool_kind: 'subagent',
      title: 'Audit clocks',
      status: 'running',
      started_at: 1,
    } as SessionEvent);
    state = applyEvent(state, {
      seq: 2,
      ts: 2,
      kind: 'assistant_text',
      block_id: 'child',
      parent_block_id: 'parent',
      text: 'sub-agent said this',
      done: true,
    } as SessionEvent);
    const view = selectView(state, 'detailed');
    expect(view.roots.map((i) => i.key)).toEqual(['parent']);
    expect(view.children.parent?.map((i) => i.key)).toEqual(['child']);
  });

  it('swaps in the untruncated block returned by session.block', () => {
    let state = emptyTimeline();
    state = applyEvent(state, tool(1, 'succeeded', 'short'));
    state = replaceBlock(state, tool(1, 'succeeded', 'the whole thing'));
    const event = state.items.t1?.event;
    expect(event?.kind === 'tool_call' && event.output).toBe('the whole thing');
  });
});

describe('block ordering (amendment A8)', () => {
  const block = (
    id: string,
    seq: number,
    options: { firstSeq?: number; kind?: 'tool_call' | 'assistant_text' } = {},
  ): SessionEvent => {
    const base = {
      seq,
      ts: 3_000 + seq,
      block_id: id,
      ...(options.firstSeq === undefined ? {} : { first_seq: options.firstSeq }),
    };
    if (options.kind === 'assistant_text' || options.kind === undefined) {
      return { ...base, kind: 'assistant_text', text: id, done: true } as SessionEvent;
    }
    return {
      ...base,
      kind: 'tool_call',
      tool: 'Bash',
      tool_kind: 'shell',
      title: 'pytest --count 100',
      status: 'succeeded',
      started_at: 3_000,
    } as SessionEvent;
  };

  it('keeps a late-finishing tool call where it started', () => {
    let state = emptyTimeline();
    state = applyEvent(state, block('tool', 5, { kind: 'tool_call', firstSeq: 5 }));
    state = applyEvent(state, block('after', 6));
    // The tool finishes long after the message that followed it started.
    state = applyEvent(state, block('tool', 40, { kind: 'tool_call', firstSeq: 5 }));
    expect(state.order).toEqual(['tool', 'after']);
    expect(state.items.tool?.seq).toBe(5);
    expect(state.lastSeq).toBe(40);
  });

  it('places a history block by first_seq, not by the seq of its latest event', () => {
    let state = emptyTimeline();
    // A reload: history returns only the newest event per block, ascending.
    state = mergeHistory(state, [
      block('early', 3),
      block('mid', 7),
      block('tool', 40, { kind: 'tool_call', firstSeq: 5 }),
    ]);
    expect(state.order).toEqual(['early', 'tool', 'mid']);
    expect(state.items.tool?.seq).toBe(5);
  });

  it('falls back to seq when the device omits first_seq', () => {
    let state = emptyTimeline();
    state = mergeHistory(state, [block('a', 3), block('b', 7), block('c', 40)]);
    expect(state.order).toEqual(['a', 'b', 'c']);
    expect(state.items.c?.seq).toBe(40);
  });

  it('moves a row back when a replacement reveals an earlier first_seq', () => {
    let state = emptyTimeline();
    // Joined mid-turn: the first event seen for this block has no first_seq.
    state = applyEvent(state, block('joined', 30, { kind: 'tool_call' }));
    state = applyEvent(state, block('later', 31));
    expect(state.order).toEqual(['joined', 'later']);

    state = applyEvent(state, block('joined', 32, { kind: 'tool_call', firstSeq: 2 }));
    expect(state.order).toEqual(['joined', 'later']);
    expect(state.items.joined?.seq).toBe(2);

    // A block that started before it now sorts ahead of it.
    state = mergeHistory(state, [block('oldest', 1)]);
    expect(state.order).toEqual(['oldest', 'joined', 'later']);
  });

  it('never lets first_seq push a block past one that started earlier', () => {
    let state = emptyTimeline();
    state = applyEvent(state, block('a', 10, { firstSeq: 10 }));
    state = applyEvent(state, block('b', 11, { firstSeq: 4 }));
    state = applyEvent(state, block('c', 12, { firstSeq: 40 }));
    expect(state.order).toEqual(['b', 'a', 'c']);
  });

  it('interleaves a prepended page with existing rows by ordering seq', () => {
    let state = emptyTimeline();
    state = applyEvent(state, block('live-1', 50, { firstSeq: 50 }));
    state = applyEvent(state, block('live-2', 51, { firstSeq: 51 }));
    state = mergeHistory(state, [
      block('h1', 10),
      block('slow', 49, { kind: 'tool_call', firstSeq: 20 }),
      block('h2', 30),
    ]);
    expect(state.order).toEqual(['h1', 'slow', 'h2', 'live-1', 'live-2']);
    expect(state.oldestSeq).toBe(10);
  });

  it('ignores first_seq on a block the timeline does not render', () => {
    let state = emptyTimeline();
    state = applyEvent(state, {
      seq: 9,
      ts: 9,
      kind: 'todos',
      first_seq: 1,
      items: [],
    } as SessionEvent);
    expect(state.order).toEqual([]);
    expect(state.lastSeq).toBe(9);
  });
});

interface TimelineFixture {
  name: string;
  frames: { type: string; session_id: string; event: SessionEvent }[];
  session_final: { last_seq: number; state: string };
}

describe.runIf(fixturesAvailable())('protocol timeline fixtures', () => {
  for (const file of ['timelines/claude.json', 'timelines/codex.json']) {
    it(`replays ${file} into a coherent timeline`, () => {
      const fixture = readFixture<TimelineFixture>(file);
      let state = emptyTimeline();
      for (const frame of fixture.frames) {
        expect(frame.type).toBe('session.event');
        state = applyEvent(state, frame.event);
      }

      expect(state.lastSeq).toBe(fixture.session_final.last_seq);
      expect(state.order.length).toBeGreaterThan(0);

      // Every rendered block resolves to an item, and nothing streams a delta.
      for (const key of state.order) {
        const item = state.items[key];
        expect(item).toBeDefined();
        const event = item?.event;
        if (event && (event.kind === 'assistant_text' || event.kind === 'thinking')) {
          expect(event.delta).toBeUndefined();
          expect(typeof event.text).toBe('string');
        }
      }

      // Streaming text was concatenated, not duplicated per delta.
      const deltas = fixture.frames.filter(
        (f) => 'delta' in f.event && f.event.delta !== undefined,
      );
      const textBlocks = new Set(
        deltas.map((f) => ('block_id' in f.event ? f.event.block_id : '')),
      );
      for (const blockId of textBlocks) {
        if (!blockId) continue;
        expect(state.items[blockId], `block ${blockId} missing`).toBeDefined();
      }

      // Sub-agent children hang off their parent tool row.
      const view = selectView(state, 'detailed');
      const parents = view.roots.filter(
        (i) => i.event.kind === 'tool_call' && i.event.tool_kind === 'subagent',
      );
      for (const parent of parents) {
        const children = view.children[parent.key] ?? [];
        for (const child of children) expect(child.parentBlockId).toBe(parent.key);
      }
    });
  }

  it('merges the history page fixture ahead of the live tail', () => {
    const page = readFixture<{ result: { events: SessionEvent[]; has_more: boolean } }>(
      'history/page.json',
    );
    const events = page.result.events;
    expect(events.length).toBeGreaterThan(0);

    const newest = events[events.length - 1];
    let state = emptyTimeline();
    state = applyEvent(state, {
      seq: (newest?.seq ?? 0) + 100,
      ts: Date.now(),
      kind: 'assistant_text',
      block_id: 'live-tail',
      text: 'live',
      done: true,
    } as SessionEvent);
    state = mergeHistory(state, events);

    expect(state.order.at(-1)).toBe('live-tail');
    expect(state.oldestSeq).toBe(events[0]?.seq);
    // History never contains streaming deltas or state-only kinds.
    for (const event of events) {
      expect(['status', 'meta', 'queue']).not.toContain(event.kind);
      expect('delta' in event ? event.delta : undefined).toBeUndefined();
    }
  });
});

describe('optimistic sends (A12)', () => {
  const SENT_AT = 1_700_000_000_000;
  const pending = (id: string, text: string): OptimisticBlock => ({
    id,
    text,
    attachments: [],
    at: SENT_AT,
  });

  const userMessage = (
    seq: number,
    blockId: string,
    text: string,
    source: 'remote' | 'terminal' = 'remote',
  ): SessionEvent =>
    ({ seq, ts: 3_000 + seq, kind: 'user_message', block_id: blockId, text, source }) as SessionEvent;

  it('renders the message before the device has echoed it', () => {
    let state = emptyTimeline();
    state = applyEvent(state, text(1, 'earlier', true));
    state = addOptimistic(state, pending('req-1', 'run the tests'));

    const roots = selectView(state, 'detailed').roots;
    expect(roots.map((item) => item.key)).toEqual(['b1', 'req-1']);
    const row = roots[1];
    expect(row?.pending?.id).toBe('req-1');
    expect(row?.event.kind).toBe('user_message');
    expect(row?.event && 'text' in row.event ? row.event.text : null).toBe('run the tests');
    // It is not a device event: the replay cursor must not move.
    expect(state.lastSeq).toBe(1);
  });

  it('ignores a second insert of the same request id, so Retry reuses the row', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'once'));
    state = addOptimistic(state, { ...pending('req-1', 'once'), at: SENT_AT + 5_000 });
    expect(state.optimistic).toHaveLength(1);
    expect(state.optimistic[0]?.at).toBe(SENT_AT);
  });

  it("replaces the pending row with the device's event under the same id", () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'run the tests'));
    state = applyEvent(state, userMessage(4, 'req-1', 'run the tests'));

    expect(state.optimistic).toEqual([]);
    const roots = selectView(state, 'detailed').roots;
    expect(roots).toHaveLength(1);
    expect(roots[0]?.key).toBe('req-1');
    expect(roots[0]?.pending).toBeUndefined();
  });

  it('reconciles by text when an older device mints its own block id', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'run the tests'));
    state = applyEvent(state, userMessage(4, 'dev-99', 'run the tests'));

    expect(state.optimistic).toEqual([]);
    const roots = selectView(state, 'detailed').roots;
    expect(roots.map((item) => item.key)).toEqual(['dev-99']);
  });

  // A page of history after a reconnect holds the conversation's older messages, and the
  // person's next message is often one they said before ("continue"). Matching the words
  // took the older message for the echo and took the pending row, with its Retry, away.
  it('never takes a page of history for the echo by its words, only by its id', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'continue'));
    state = mergeMissed(state, [userMessage(4, 'earlier', 'continue')]);
    expect(state.optimistic.map((b) => b.id)).toEqual(['req-1']);
    state = mergeHistory(state, [userMessage(2, 'earlier-still', 'continue')]);
    expect(state.optimistic.map((b) => b.id)).toEqual(['req-1']);

    state = mergeMissed(state, [userMessage(9, 'req-1', 'continue')]);
    expect(state.optimistic).toEqual([]);
  });

  it('does not take an older message with the same words, pushed live after a restart, for the echo', () => {
    let state = applyEvent(emptyTimeline(), text(1000, 'earlier', true));
    state = addOptimistic(state, pending('req-1', 'continue'));
    // The rows start again from the newest page; the pending row stays.
    state = keepOptimistic(state);

    state = applyEvent(state, userMessage(900, 'older', 'continue'));
    expect(state.optimistic.map((b) => b.id)).toEqual(['req-1']);
    state = applyEvent(state, userMessage(1001, 'dev-1', 'continue'));
    expect(state.optimistic).toEqual([]);
  });

  it('reconciles one pending row per event and leaves the rest', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'same words'));
    state = addOptimistic(state, pending('req-2', 'same words'));
    state = applyEvent(state, userMessage(4, 'dev-99', 'same words'));

    expect(state.optimistic.map((b) => b.id)).toEqual(['req-2']);
  });

  it('never reconciles against a message typed in the terminal', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'run the tests'));
    state = applyEvent(state, userMessage(4, 'dev-99', 'run the tests', 'terminal'));

    expect(state.optimistic.map((b) => b.id)).toEqual(['req-1']);
    expect(selectView(state, 'detailed').roots.map((item) => item.key)).toEqual(['dev-99', 'req-1']);
  });

  it('drops a pending row the gateway refused', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'nope'));
    state = removeOptimistic(state, 'req-1');
    expect(state.optimistic).toEqual([]);
    expect(selectView(state, 'detailed').roots).toEqual([]);
    expect(removeOptimistic(state, 'req-1')).toBe(state);
  });

  it('calls a send unconfirmed only once it is a minute old', () => {
    const block = pending('req-1', 'hello');
    expect(isUnconfirmed(block, SENT_AT + 500)).toBe(false);
    expect(isUnconfirmed(block, SENT_AT + UNCONFIRMED_AFTER_MS - 1)).toBe(false);
    expect(isUnconfirmed(block, SENT_AT + UNCONFIRMED_AFTER_MS)).toBe(true);
  });

  it('gives the row up to the queue when a snapshot names it', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'first'));
    state = addOptimistic(state, pending('req-2', 'second'));
    state = dropQueued(state, ['req-2']);

    // The composer's Up next list stands for it until the device sends it.
    expect(state.optimistic.map((b) => b.id)).toEqual(['req-1']);
    // Nothing to change means the same object, so no needless re-render.
    expect(dropQueued(state, ['req-2'])).toBe(state);
    expect(dropQueued(state, [])).toBe(state);
  });

  it('keeps pending rows across a resync and drops the device events', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'still sending'));
    state = applyEvent(state, text(9, 'old answer', true));
    const fresh = keepOptimistic(state);

    expect(fresh.order).toEqual([]);
    expect(fresh.lastSeq).toBe(0);
    expect(fresh.optimistic.map((b) => b.id)).toEqual(['req-1']);
  });

  it('does not duplicate the row when history carries the device copy', () => {
    let state = addOptimistic(emptyTimeline(), pending('req-1', 'run the tests'));
    state = mergeHistory(state, [userMessage(4, 'req-1', 'run the tests')]);

    expect(state.optimistic).toEqual([]);
    expect(selectView(state, 'detailed').roots.map((item) => item.key)).toEqual(['req-1']);
  });

  it('hides a pending row whose block the device already sent', () => {
    let state = applyEvent(emptyTimeline(), userMessage(4, 'req-1', 'run the tests'));
    // addOptimistic refuses, and selectView would skip it either way.
    state = addOptimistic(state, pending('req-1', 'run the tests'));
    expect(selectView(state, 'detailed').roots).toHaveLength(1);
  });
});

/**
 * The two detail levels, `docs/DESIGN.md` § "The timeline". Simple draws only
 * what is written to the person; Detailed is the transcript entire.
 */
describe('detail levels', () => {
  const event = (partial: Record<string, unknown>): SessionEvent => partial as unknown as SessionEvent;

  const conversation = (): TimelineState =>
    applyEvents(emptyTimeline(), [
      event({ seq: 1, ts: 1, kind: 'user_message', block_id: 'u1', source: 'remote', text: 'fix it' }),
      event({ seq: 2, ts: 2, kind: 'thinking', block_id: 'th1', text: 'weighing it up', done: true }),
      event({
        seq: 3,
        ts: 3,
        kind: 'tool_call',
        block_id: 'task1',
        tool: 'Task',
        tool_kind: 'subagent',
        title: 'Audit clocks',
        status: 'running',
        started_at: 3,
      }),
      event({
        seq: 4,
        ts: 4,
        kind: 'assistant_text',
        block_id: 'sub1',
        parent_block_id: 'task1',
        text: 'the sub-agent reported back',
        done: true,
      }),
      event({
        seq: 5,
        ts: 5,
        kind: 'approval',
        block_id: 'ap1',
        parent_block_id: 'task1',
        request_id: 'req-a',
        tool: 'Bash',
        tool_kind: 'shell',
        title: 'git commit',
        status: 'pending',
        options: [{ id: 'allow', label: 'Allow once', style: 'primary' }],
      }),
      event({ seq: 6, ts: 6, kind: 'assistant_text', block_id: 'a1', text: 'done', done: true }),
      event({ seq: 7, ts: 7, kind: 'notice', level: 'info', text: 'the device reconnected' }),
    ]);

  it('draws the whole transcript at Detailed', () => {
    const view = selectView(conversation(), 'detailed');
    expect(view.roots.map((item) => item.key)).toEqual(['u1', 'th1', 'task1', 'a1', 'notice#7']);
    expect(view.children.task1?.map((item) => item.key)).toEqual(['sub1', 'ap1']);
  });

  it('drops the workings at Simple, in place, not collapsed', () => {
    const view = selectView(conversation(), 'simple');
    expect(view.roots.map((item) => item.key)).toEqual(['u1', 'ap1', 'a1', 'notice#7']);
    expect(view.children).toEqual({});
  });

  it('promotes a card waiting on an answer out of a hidden tool call', () => {
    const roots = selectView(conversation(), 'simple').roots;
    // The approval keeps its place in the transcript; the sub-agent's own text
    // goes with the tool row that held it.
    expect(roots.map((item) => item.key)).toContain('ap1');
    expect(roots.map((item) => item.key)).not.toContain('sub1');
  });

  it('keeps a slash command’s own output at both levels (A27)', () => {
    const state = applyEvents(conversation(), [
      event({
        seq: 8,
        ts: 8,
        kind: 'tool_call',
        block_id: 'cmd1',
        tool: '/usage',
        tool_kind: 'other',
        title: '/usage',
        status: 'succeeded',
        started_at: 8,
        output: 'Plan: Pro',
      }),
    ]);
    // It answers what the person asked for, so Simple draws it like any answer.
    for (const detail of ['simple', 'detailed'] as const) {
      expect(selectView(state, detail).roots.map((item) => item.key)).toContain('cmd1');
    }
  });

  it('keeps an unconfirmed send at both levels', () => {
    const state = addOptimistic(conversation(), {
      id: 'req-1',
      text: 'and again',
      attachments: [],
      at: 9_000,
    });
    for (const detail of ['simple', 'detailed'] as const) {
      expect(selectView(state, detail).roots.at(-1)?.key).toBe('req-1');
    }
  });

  it('draws an interrupted turn at both levels and a clean one at neither', () => {
    const ended = (stop: string, seq: number): SessionEvent =>
      event({ seq, ts: seq, kind: 'turn_completed', turn_id: `t${seq}`, stop_reason: stop });
    const state = applyEvents(conversation(), [ended('completed', 8), ended('interrupted', 9)]);
    for (const detail of ['simple', 'detailed'] as const) {
      const kinds = selectView(state, detail).roots.map((item) => item.event.kind);
      expect(kinds.filter((kind) => kind === 'turn_completed')).toHaveLength(1);
    }
  });
});

/**
 * A tab left open on a busy agent used to gain a row per block for as long as
 * the agent ran and never lose one: the store and the DOM grew without bound.
 */
describe('the row cap', () => {
  const message = (seq: number): SessionEvent =>
    ({
      seq,
      ts: 1_000 + seq,
      kind: 'notice',
      block_id: `n-${seq}`,
      level: 'info',
      text: `notice ${seq}`,
    }) as SessionEvent;

  it('drops the oldest rows and moves the history cursor with them', () => {
    let state = emptyTimeline();
    for (let seq = 1; seq <= MAX_TIMELINE_ITEMS + 50; seq += 1) state = applyEvent(state, message(seq));

    expect(state.order).toHaveLength(MAX_TIMELINE_ITEMS);
    expect(state.dropped).toBe(50);
    // The rows that went are gone from `items` too, not merely unlisted.
    expect(state.items['n-1']).toBeUndefined();
    expect(state.order[0]).toBe('n-51');
    // Scrolling back asks for what was dropped, not for what is on screen.
    expect(state.oldestSeq).toBe(51);
    expect(state.lastSeq).toBe(MAX_TIMELINE_ITEMS + 50);
  });

  it('keeps a page the reader asked for by scrolling back', () => {
    let state = emptyTimeline();
    for (let seq = 1; seq <= MAX_TIMELINE_ITEMS + 10; seq += 1) state = applyEvent(state, message(seq));
    const older = Array.from({ length: 5 }, (_, i) => message(i + 1));

    state = mergeHistory(state, older);

    expect(state.order.length).toBe(MAX_TIMELINE_ITEMS + 5);
    expect(state.oldestSeq).toBe(1);
  });
});

/**
 * The oldest event a session delivered live is often a `status`, which draws no
 * row. Discarding its `seq` made the first history page ask for everything
 * before the *next* event, so that one event came back a second time.
 */
describe('the history cursor', () => {
  it('counts a state-only event as the oldest the session delivered', () => {
    const status = { seq: 1, ts: 1, kind: 'status', state: 'running' } as unknown as SessionEvent;
    const state = applyEvent(emptyTimeline(), status);

    expect(state.order).toEqual([]);
    expect(state.oldestSeq).toBe(1);
  });

  it.runIf(fixturesAvailable())('agrees with the history path on the shipped timelines', () => {
    for (const file of ['timelines/claude.json', 'timelines/codex.json']) {
      const fixture = readFixture<{ frames: { event: SessionEvent }[] }>(file);
      const events = fixture.frames.map((frame) => frame.event);
      const live = applyEvents(emptyTimeline(), events);
      const history = mergeHistory(emptyTimeline(), events);

      expect(live.oldestSeq).toBe(history.oldestSeq);
    }
  });
});

/**
 * A live event that skips a `seq` leaves a hole. The web used to move its
 * cursor over it and say nothing, so the hole stayed for good; now the
 * timeline remembers the last event below it, which is where a repair asks the
 * device to start.
 */
describe('holes', () => {
  const block = (seq: number): SessionEvent =>
    ({ seq, ts: seq, kind: 'assistant_text', block_id: `b${seq}`, text: `t${seq}`, done: true }) as SessionEvent;

  it('follows lastSeq while events arrive in order, the first one included', () => {
    let state = emptyTimeline();
    // A first event has nothing before it to skip, whatever its number.
    state = applyEvent(state, block(40));
    expect([state.lastSeq, state.contiguousSeq, hasGap(state)]).toEqual([40, 40, false]);
    state = applyEvent(state, block(41));
    expect([state.lastSeq, state.contiguousSeq, hasGap(state)]).toEqual([41, 41, false]);
  });

  it('stays behind lastSeq once an event skips a seq, and the hole stays open', () => {
    let state = applyEvents(emptyTimeline(), [block(1), block(2)]);
    state = applyEvent(state, block(5));
    expect([state.lastSeq, state.contiguousSeq, hasGap(state)]).toEqual([5, 2, true]);

    // Events that follow in order do not close it: only a repair can.
    state = applyEvent(state, block(6));
    expect([state.lastSeq, state.contiguousSeq, hasGap(state)]).toEqual([6, 2, true]);
  });

  it('takes a jump inside a batch the gateway vouches for to be no hole', () => {
    const state = applyEvents(emptyTimeline(), [block(1), block(2), block(9), block(14)]);
    expect([state.lastSeq, state.contiguousSeq, hasGap(state)]).toEqual([14, 14, false]);
  });

  it('counts a hole as handled while its repair runs, and a new skip opens a new one', () => {
    let state = applyEvents(emptyTimeline(), [block(1), block(2)]);
    state = applyEvent(state, block(5));
    state = beginRepair(state);
    expect([state.contiguousSeq, hasGap(state)]).toEqual([5, false]);

    state = applyEvent(state, block(6));
    expect([state.contiguousSeq, hasGap(state)]).toEqual([6, false]);
    state = applyEvent(state, block(9));
    expect([state.contiguousSeq, hasGap(state)]).toEqual([6, true]);
  });

  it('opens the hole again, back at its start, when the repair fails', () => {
    let state = applyEvents(emptyTimeline(), [block(1), block(2)]);
    state = beginRepair(applyEvent(state, block(5)));

    state = reopenGap(state, 2);
    expect([state.contiguousSeq, hasGap(state)]).toEqual([2, true]);
    // Never forward: a later, smaller hole does not close a bigger one.
    expect(reopenGap(state, 4)).toBe(state);
  });
});

/**
 * The gateway fans out what a device reconnect backfilled once live events are
 * already flowing, so an event can arrive behind newer ones. It used to be
 * dropped as late, which left the hole for good.
 */
describe('events behind the cursor', () => {
  const full = (seq: number, blockId: string, body: string): SessionEvent =>
    ({ seq, ts: seq, kind: 'assistant_text', block_id: blockId, text: body, done: true }) as SessionEvent;

  it('merge where they belong, and move neither cursor', () => {
    let state = applyEvents(emptyTimeline(), [full(1, 'a', 'one'), full(9, 'c', 'three')]);
    state = applyEvent(state, full(4, 'b', 'two'));

    expect(state.order).toEqual(['a', 'b', 'c']);
    expect([state.lastSeq, state.contiguousSeq]).toEqual([9, 9]);
  });

  it('replace a block they carry a newer version of', () => {
    let state = applyEvents(emptyTimeline(), [tool(2, 'running', 'partial'), full(9, 'z', 'later')]);
    state = applyEvent(state, tool(6, 'succeeded', 'final'));

    const event = state.items.t1?.event;
    expect(event?.kind === 'tool_call' && event.status).toBe('succeeded');
    expect(state.order).toEqual(['t1', 'z']);
  });

  it('are dropped as the same state when the timeline already has them', () => {
    const state = applyEvents(emptyTimeline(), [full(3, 'a', 'one'), full(9, 'b', 'two')]);
    expect(applyEvent(state, full(3, 'a', 'one again'))).toBe(state);
    // An older version of a block it holds newer is no news either.
    expect(applyEvent(state, full(5, 'b', 'older'))).toBe(state);
  });

  it('are dropped when they are a fragment: the final event carries the whole text', () => {
    const state = applyEvents(emptyTimeline(), [text(5, 'kept'), full(9, 'z', 'later')]);
    expect(applyEvent(state, text(3, 'late fragment'))).toBe(state);
  });
});

/**
 * What the device holds for a stretch the timeline missed. Unlike a page of
 * older history, which only fills in below what is shown, the newer copy of a
 * block wins here.
 */
describe('merging what a repair fetched', () => {
  const full = (seq: number, blockId: string, body: string): SessionEvent =>
    ({ seq, ts: seq, kind: 'assistant_text', block_id: blockId, text: body, done: true }) as SessionEvent;

  it('lets the newer copy of a block replace the older one, in the older one’s place', () => {
    let state = applyEvents(emptyTimeline(), [full(1, 'a', 'one'), tool(2, 'running', 'partial')]);
    state = applyEvent(state, full(6, 'z', 'later'));
    state = mergeMissed(state, [tool(4, 'succeeded', 'final')]);

    const event = state.items.t1?.event;
    expect(event?.kind === 'tool_call' && event.output).toBe('final');
    expect(state.order).toEqual(['a', 't1', 'z']);
  });

  it('adds the rows of the stretch and moves both lines when no hole is open', () => {
    let state = applyEvents(emptyTimeline(), [full(1, 'a', 'one'), full(2, 'b', 'two')]);
    state = mergeMissed(state, [full(3, 'c', 'three'), full(5, 'd', 'four')]);

    expect(state.order).toEqual(['a', 'b', 'c', 'd']);
    expect([state.lastSeq, state.contiguousSeq]).toEqual([5, 5]);
  });

  it('leaves a hole that is still open open', () => {
    let state = applyEvents(emptyTimeline(), [full(1, 'a', 'one'), full(2, 'b', 'two')]);
    state = applyEvent(state, full(9, 'z', 'jumped'));
    state = mergeMissed(state, [full(3, 'c', 'three')]);

    expect(state.order).toEqual(['a', 'b', 'c', 'z']);
    expect([state.lastSeq, state.contiguousSeq, hasGap(state)]).toEqual([9, 2, true]);
  });

  it('never overwrites what is shown with an older copy', () => {
    let state = applyEvents(emptyTimeline(), [tool(9, 'succeeded', 'final')]);
    state = mergeMissed(state, [tool(2, 'running', 'stale')]);

    const event = state.items.t1?.event;
    expect(event?.kind === 'tool_call' && event.output).toBe('final');
  });

  it('retires the pending row the device’s copy of a send confirms', () => {
    let state = addOptimistic(emptyTimeline(), {
      id: 'req-1',
      text: 'run the tests',
      attachments: [],
      at: 1,
    });
    state = mergeMissed(state, [
      { seq: 4, ts: 4, kind: 'user_message', block_id: 'req-1', text: 'run the tests', source: 'remote' } as SessionEvent,
    ]);

    expect(state.optimistic).toEqual([]);
    expect(state.order).toEqual(['req-1']);
  });

  it('is the same state when there is nothing new', () => {
    const state = applyEvents(emptyTimeline(), [full(1, 'a', 'one'), full(2, 'b', 'two')]);
    expect(mergeMissed(state, [])).toBe(state);
    expect(mergeMissed(state, [full(2, 'b', 'two')])).toBe(state);
  });

  it('keeps to the row cap', () => {
    const rows = Array.from({ length: MAX_TIMELINE_ITEMS }, (_, i) => full(i + 1, `r${i + 1}`, 'x'));
    let state = applyEvents(emptyTimeline(), rows);
    state = mergeMissed(state, [full(MAX_TIMELINE_ITEMS + 1, 'new', 'x')]);

    expect(state.order).toHaveLength(MAX_TIMELINE_ITEMS);
    expect(state.dropped).toBe(1);
    expect(state.order.at(-1)).toBe('new');
  });
});

/**
 * The device keeps only the latest event of each block, and a `status` or a
 * `meta` it does not keep at all. After a complete read the last events of an
 * idle conversation are therefore ones nobody can fetch; they are not missing.
 */
describe('what the device never stored', () => {
  const block = (seq: number): SessionEvent =>
    ({ seq, ts: seq, kind: 'assistant_text', block_id: `b${seq}`, text: 't', done: true }) as SessionEvent;

  it('is counted as read once the device says its last event came after the last one read', () => {
    let state = mergeHistory(emptyTimeline(), [block(109), block(110)]);
    state = coverThrough(state, 112);
    expect([state.lastSeq, state.contiguousSeq, hasGap(state)]).toEqual([112, 112, false]);

    // So the first live event after it is the next one, not a skip.
    state = applyEvent(state, block(113));
    expect(hasGap(state)).toBe(false);
  });

  it('leaves a hole that is still open open', () => {
    let state = applyEvents(emptyTimeline(), [block(1), block(2)]);
    state = applyEvent(state, block(9));
    state = coverThrough(state, 12);
    expect([state.lastSeq, state.contiguousSeq, hasGap(state)]).toEqual([12, 2, true]);
  });

  it('changes nothing when the device’s count is not ahead', () => {
    const state = applyEvents(emptyTimeline(), [block(1), block(2)]);
    expect(coverThrough(state, 2)).toBe(state);
    expect(coverThrough(state, 0)).toBe(state);
  });
});

/**
 * `session.history` pages back from `oldestSeq`. An event that arrives behind
 * newer ones — a backfill another client's cursor made the gateway fetch — is
 * merged as a row, but it must not move that edge: paging back from its seq
 * would skip everything between it and the rows the timeline holds.
 */
describe('the edge of the history window', () => {
  const block = (seq: number): SessionEvent =>
    ({ seq, ts: seq, kind: 'assistant_text', block_id: `b${seq}`, text: 't', done: true }) as SessionEvent;
  const range = (from: number, to: number): SessionEvent[] =>
    Array.from({ length: to - from + 1 }, (_, i) => block(from + i));

  it('is not lowered by an event that arrives behind newer ones', () => {
    let state = mergeHistory(emptyTimeline(), range(5000, 5099));
    state = applyEvent(state, block(4001));

    expect(state.order[0]).toBe('b4001');
    expect(state.oldestSeq).toBe(5000);
  });

  it('is not lowered by a page of a repair either', () => {
    let state = mergeHistory(emptyTimeline(), range(5000, 5099));
    state = mergeMissed(state, [block(4001), block(4002)]);
    expect(state.oldestSeq).toBe(5000);
  });

  it('is the lowest seq of the first batch merged into a timeline with none, in whatever order', () => {
    const state = mergeMissed(emptyTimeline(), [block(7), block(5), block(6)]);
    expect(state.oldestSeq).toBe(5);
  });

  it('is still lowered by a page of older history', () => {
    let state = mergeHistory(emptyTimeline(), range(5000, 5099));
    state = mergeHistory(state, range(4800, 4999));
    expect(state.oldestSeq).toBe(4800);
  });

  // On a cold open the same stray events can arrive before the newest page does, while
  // the timeline is still empty, and then they are the first events it has seen.
  it('is the newest page’s own lowest seq, whatever arrived live below it before the page', () => {
    let state = applyEvent(emptyTimeline(), block(4001));
    state = applyEvent(state, block(4002));
    expect(state.oldestSeq).toBe(4001);

    state = mergeNewestPage(state, range(5000, 5099));

    expect(state.oldestSeq).toBe(5000);
    // The stray rows stay where they are; the page that reaches them takes them for duplicates.
    expect(state.order.slice(0, 3)).toEqual(['b4001', 'b4002', 'b5000']);
  });

  it('is what live events made it when the newest page is empty', () => {
    const state = mergeNewestPage(applyEvent(emptyTimeline(), block(4001)), []);
    expect(state.oldestSeq).toBe(4001);
  });

  it('is the newest page’s lowest seq when nothing arrived before it', () => {
    expect(mergeNewestPage(emptyTimeline(), range(5000, 5099)).oldestSeq).toBe(5000);
  });
});
