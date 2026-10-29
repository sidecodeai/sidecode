/**
 * Pure timeline reducer: session events in, ordered renderable items out.
 *
 * Contract rules implemented here (PROTOCOL-FROZEN.md §4, §7, A8):
 *  - a later event with the same `block_id` fully replaces the earlier one,
 *    except `delta` events on `assistant_text` / `thinking`, which append;
 *  - an event whose `seq` is not greater than the last applied one moves nothing:
 *    it is dropped unless it carries a block the timeline does not hold or a
 *    newer version of one it does — that is an event that arrived behind newer
 *    ones, and it is merged where it belongs;
 *  - a live event that skips a `seq` leaves a hole: `contiguousSeq` stays behind
 *    `lastSeq` until a repair has fetched what lies between (`mergeMissed`);
 *  - `status`, `meta`, `queue` and `todos` are session state, not timeline items;
 *  - events carrying `parent_block_id` belong under their parent tool row;
 *  - blocks are ordered by `first_seq ?? seq`, so a tool call that finishes long
 *    after it started keeps its place — live and after a reload.
 *
 * Amendment A12 adds one more: the app mints the `session.send` request id and
 * the device echoes it as the `user_message` block id, so the message is shown
 * the moment it is sent and the device's event replaces it under rule 1. Those
 * optimistic blocks are kept apart from `items` — they carry no `seq`, must not
 * move the replay cursor, and always sort after everything the device sent.
 */
import { eventKey } from '../lib/ids';
import type {
  Attachment,
  QuestionEvent,
  SessionEvent,
  TimelineDetail,
} from '../protocol/types';

export interface TimelineItem {
  key: string;
  /** Ordering key: `first_seq ?? seq` of the block's first appearance. */
  seq: number;
  ts: number;
  parentBlockId: string | null;
  event: SessionEvent;
  /** Set on a row the device has not confirmed yet. */
  pending?: OptimisticBlock;
}

/** A message this app has sent and the device has not echoed back yet (A12). */
export interface OptimisticBlock {
  /** The `session.send` request id, which becomes the device's `block_id`. */
  id: string;
  text: string;
  attachments: Attachment[];
  /** When the app sent it, for the unconfirmed timeout. */
  at: number;
  /**
   * The timeline's `lastSeq` when it was sent, set by `addOptimistic`. The echo is
   * numbered after everything the device had issued when it took the message,
   * which is at least everything this timeline had seen; an event numbered at or
   * below it is older than the send and cannot be its echo.
   */
  afterSeq?: number;
  /**
   * What `session.send` answered, once it has. A `steered` message waits for the
   * agent to reach its next step (A14), which takes as long as it takes, so the
   * wait says nothing about delivery.
   */
  accepted?: 'sent' | 'steered';
}

export interface TimelineState {
  order: string[];
  items: Record<string, TimelineItem>;
  /**
   * The highest `seq` applied: the cursor `session.subscribe` resumes from, and
   * the line at or below which a live event counts as a late one.
   */
  lastSeq: number;
  /**
   * The highest `seq` such that nothing at or below it is missing. It follows
   * `lastSeq` until an event skips a number, and then stays where it was while
   * `lastSeq` moves on: a repair asks the device for everything after it.
   */
  contiguousSeq: number;
  oldestSeq: number | null;
  /** Sent, not yet confirmed. Rendered after `order`, oldest first. */
  optimistic: OptimisticBlock[];
  /**
   * How many rows the cap below has dropped from the front. The chat store
   * reads it to know there is older history again, whatever the last
   * `session.history` page said.
   */
  dropped: number;
}

/**
 * The most rows a live timeline keeps. A tab left open on a busy agent gained
 * a row per block for as long as it ran and never lost one, so both the store
 * and the DOM grew without bound. Past this the oldest rows go; scrolling back
 * pages them in again, because the cap moves `oldestSeq` forward with them.
 */
export const MAX_TIMELINE_ITEMS = 3_000;

export function emptyTimeline(): TimelineState {
  return {
    order: [],
    items: {},
    lastSeq: 0,
    contiguousSeq: 0,
    oldestSeq: null,
    optimistic: [],
    dropped: 0,
  };
}

/** Whether a live event skipped a `seq` that nothing has fetched yet. */
export function hasGap(state: TimelineState): boolean {
  return state.contiguousSeq < state.lastSeq;
}

/**
 * A repair is on its way to fetch what is missing, so the hole it was started
 * for no longer counts. A skip noticed from here on opens a new one.
 */
export function beginRepair(state: TimelineState): TimelineState {
  return state.contiguousSeq === state.lastSeq ? state : { ...state, contiguousSeq: state.lastSeq };
}

/** The repair failed: everything after `after` is missing again. */
export function reopenGap(state: TimelineState, after: number): TimelineState {
  return after >= state.contiguousSeq ? state : { ...state, contiguousSeq: after };
}

/**
 * The device counts its last event as `seq`, and what it stores was just read
 * in full: whatever came after the last event read was never stored — a status,
 * a meta — so nothing is missing up to `seq`. Without this the first live event
 * of an idle conversation would look as if it had skipped those.
 */
export function coverThrough(state: TimelineState, seq: number): TimelineState {
  if (seq <= state.lastSeq) return state;
  return { ...state, lastSeq: seq, contiguousSeq: hasGap(state) ? state.contiguousSeq : seq };
}

/**
 * A fresh timeline for a resync that keeps the unconfirmed sends. Dropping them
 * would make a message the user just sent vanish on a reconnect.
 */
export function keepOptimistic(previous: TimelineState): TimelineState {
  return { ...emptyTimeline(), optimistic: previous.optimistic };
}

/** Event kinds that never produce a timeline item. */
const STATE_ONLY = new Set(['status', 'meta', 'queue', 'todos', 'turn_started']);

function isRenderable(event: SessionEvent): boolean {
  if (STATE_ONLY.has(event.kind)) return false;
  // A clean turn boundary needs no row; interrupted / failed turns do.
  if (event.kind === 'turn_completed') return event.stop_reason !== 'completed';
  // A35: the moment of resuming is the prompt in the person's bubble, so the
  // `fired` step is not a row of its own; every other step is.
  if (event.kind === 'resume') return event.status !== 'fired';
  return true;
}

function keyFor(event: SessionEvent): string {
  if ('block_id' in event && typeof event.block_id === 'string' && event.block_id) {
    return event.block_id;
  }
  return eventKey(event.kind, event.seq);
}

/**
 * Where the block belongs in the timeline. `first_seq` wins when the device
 * sends it; otherwise the earliest `seq` this app has seen for the block does.
 */
function orderSeq(event: SessionEvent, previous: TimelineItem | undefined): number {
  const declared = typeof event.first_seq === 'number' ? event.first_seq : event.seq;
  return previous ? Math.min(previous.seq, declared) : declared;
}

function streamingText(previous: SessionEvent | undefined, next: SessionEvent): string | undefined {
  if (next.kind !== 'assistant_text' && next.kind !== 'thinking') return undefined;
  if (next.delta === undefined) return next.text;
  const prevText =
    previous && (previous.kind === 'assistant_text' || previous.kind === 'thinking')
      ? (previous.text ?? '')
      : '';
  return prevText + next.delta;
}

/** Merge one event into an existing item (or create it). Returns null when not renderable. */
function mergeItem(previous: TimelineItem | undefined, event: SessionEvent): TimelineItem | null {
  if (!isRenderable(event)) return null;
  const key = keyFor(event);
  const text = streamingText(previous?.event, event);
  const merged: SessionEvent =
    text === undefined
      ? event
      : ({ ...event, text, delta: undefined } as SessionEvent);
  return {
    key,
    seq: orderSeq(event, previous),
    ts: event.ts,
    parentBlockId: event.parent_block_id ?? previous?.parentBlockId ?? null,
    event: merged,
  };
}

/**
 * Insert a key so `order` stays sorted by ordering seq. Scans from the end, so
 * the common case — a block that belongs last — costs one comparison.
 */
function insertOrdered(
  order: readonly string[],
  items: Record<string, TimelineItem>,
  key: string,
  seq: number,
): string[] {
  let index = order.length;
  while (index > 0) {
    const previous = items[order[index - 1] as string];
    if (previous !== undefined && previous.seq > seq) index -= 1;
    else break;
  }
  if (index === order.length) return [...order, key];
  const next = [...order];
  next.splice(index, 0, key);
  return next;
}

/* --------------------------------------------------------- optimistic (A12) */

/** How long an unconfirmed send waits before the row says so. */
export const UNCONFIRMED_AFTER_MS = 60_000;

/**
 * Whether a pending row has waited too long. Pure; the row needs only a clock.
 * §8 rule 7 reserves this for a send whose delivery is genuinely uncertain, so
 * a gateway answer settles it for good however long the agent then takes.
 */
export function isUnconfirmed(block: OptimisticBlock, now: number): boolean {
  if (block.accepted !== undefined) return false;
  return now - block.at >= UNCONFIRMED_AFTER_MS;
}

/** Show a message the moment it is sent. Idempotent, so Retry reuses the row. */
export function addOptimistic(state: TimelineState, block: OptimisticBlock): TimelineState {
  if (state.items[block.id] || state.optimistic.some((b) => b.id === block.id)) return state;
  return { ...state, optimistic: [...state.optimistic, { ...block, afterSeq: state.lastSeq }] };
}

/**
 * Record what `session.send` answered for a pending row. The row itself stays:
 * under A14 a `steered` message becomes a device block only when the agent
 * takes it, and a `sent` one when the device echoes it.
 */
export function markAccepted(
  state: TimelineState,
  id: string,
  accepted: 'sent' | 'steered',
): TimelineState {
  if (!state.optimistic.some((b) => b.id === id && b.accepted !== accepted)) return state;
  return {
    ...state,
    optimistic: state.optimistic.map((b) => (b.id === id ? { ...b, accepted } : b)),
  };
}

/** Drop a pending row: the send was refused, or its queue entry was removed. */
export function removeOptimistic(state: TimelineState, id: string): TimelineState {
  if (!state.optimistic.some((b) => b.id === id)) return state;
  return { ...state, optimistic: state.optimistic.filter((b) => b.id !== id) };
}

/**
 * Retire the pending rows a `queue` snapshot has taken over. A queued message
 * is represented by the composer's Up next list until the device dequeues it and
 * emits the `user_message` under the same id (A12), so showing both would show
 * it twice.
 */
export function dropQueued(state: TimelineState, ids: readonly string[]): TimelineState {
  if (state.optimistic.length === 0 || ids.length === 0) return state;
  const optimistic = state.optimistic.filter((block) => !ids.includes(block.id));
  return optimistic.length === state.optimistic.length ? state : { ...state, optimistic };
}

/**
 * Retire the pending rows an incoming event confirms. The id match is A12 and
 * holds for every event. The text match is the fallback for a device that still
 * mints its own block ids: it only ever retires one row per event, only for a
 * live event, and only for one numbered after the send. A page of history, or an
 * event from before the send, can hold an older message with the same words,
 * and that is not the echo of this one.
 */
function reconcile(
  optimistic: OptimisticBlock[],
  event: SessionEvent,
  live: boolean,
): OptimisticBlock[] {
  if (optimistic.length === 0) return optimistic;
  const key = keyFor(event);
  if (optimistic.some((b) => b.id === key)) return optimistic.filter((b) => b.id !== key);
  if (!live || event.kind !== 'user_message' || event.source !== 'remote') return optimistic;
  const index = optimistic.findIndex((b) => b.text === event.text && event.seq > (b.afterSeq ?? 0));
  if (index === -1) return optimistic;
  return optimistic.filter((_, i) => i !== index);
}

/**
 * Drop the oldest rows once the timeline is past its cap, and carry
 * `oldestSeq` forward with them so the next page of history asks for what was
 * dropped rather than for what is already on screen.
 */
function capOldest(state: TimelineState): TimelineState {
  const excess = state.order.length - MAX_TIMELINE_ITEMS;
  if (excess <= 0) return state;
  const items = { ...state.items };
  for (const key of state.order.slice(0, excess)) delete items[key];
  const order = state.order.slice(excess);
  const front = items[order[0] as string];
  return {
    ...state,
    order,
    items,
    oldestSeq: front ? front.seq : state.oldestSeq,
    dropped: state.dropped + excess,
  };
}

/**
 * Apply a live event. One that skips a `seq` is applied all the same and leaves
 * a hole behind it (`hasGap`); the first event of a timeline has nothing to
 * skip. One at or below `lastSeq` is a duplicate or a late arrival, and
 * `mergeLate` says which.
 */
export function applyEvent(state: TimelineState, event: SessionEvent): TimelineState {
  return apply(state, event, true);
}

/**
 * Apply events the gateway vouches for, in order: a subscribe reply. A jump in
 * `seq` inside one is not a hole, because after a device reconnect the gateway
 * answers from the device's history, which keeps only the latest event of each
 * block.
 */
export function applyEvents(state: TimelineState, events: readonly SessionEvent[]): TimelineState {
  return events.reduce((next, event) => apply(next, event, false), state);
}

function apply(state: TimelineState, event: SessionEvent, detectGaps: boolean): TimelineState {
  if (event.seq <= state.lastSeq) return mergeLate(state, event);
  const key = keyFor(event);
  const previous = state.items[key];
  const item = mergeItem(previous, event);
  const lastSeq = event.seq;
  const skipped = detectGaps && state.lastSeq > 0 && event.seq > state.lastSeq + 1;
  // A hole already open stays open; a new one opens behind the event that skipped.
  const contiguousSeq = hasGap(state) || skipped ? state.contiguousSeq : event.seq;
  const oldestSeq = state.oldestSeq === null ? event.seq : Math.min(state.oldestSeq, event.seq);
  const optimistic = reconcile(state.optimistic, event, true);
  // A state-only event still moves the window: it is the oldest event this
  // session delivered, so history must be asked for what came before it and
  // not for it again.
  if (!item) return { ...state, lastSeq, contiguousSeq, oldestSeq, optimistic };

  const items = { ...state.items, [key]: item };
  let order = state.order;
  if (previous === undefined) {
    order = insertOrdered(order, items, key, item.seq);
  } else if (previous.seq !== item.seq) {
    // A replacement revealed an earlier `first_seq`: move the row back.
    order = insertOrdered(
      order.filter((k) => k !== key),
      items,
      key,
      item.seq,
    );
  }
  return capOldest({ ...state, order, items, lastSeq, contiguousSeq, oldestSeq, optimistic });
}

/**
 * An event at or below the cursor: a duplicate, or one that arrived behind
 * newer ones — the gateway fanning out what a device reconnect backfilled once
 * live events were already flowing. A block the timeline does not hold, or holds
 * in an older version, takes it; anything else is dropped. A streaming
 * fragment is dropped either way, because its block's final event carries the
 * whole text.
 */
function mergeLate(state: TimelineState, event: SessionEvent): TimelineState {
  const fragment =
    (event.kind === 'assistant_text' || event.kind === 'thinking') && event.delta !== undefined;
  return fragment ? state : mergeMissed(state, [event]);
}

/**
 * Merge an older history page (ascending `seq`, ending before the oldest known
 * event). Existing items win: they are at least as fresh as the history copy.
 * Not capped: these are the rows the reader asked for by scrolling back, and
 * dropping them here would page the same events in for ever.
 */
export function mergeHistory(state: TimelineState, events: readonly SessionEvent[]): TimelineState {
  return mergeEvents(state, events, false);
}

/**
 * Merge the newest page of history, which is the window the reader pages back
 * from: its own lowest `seq` is the edge. Rows that arrived live below it before
 * it came — events a backfill for another client made the gateway push — sit
 * outside the window, and taking the lowest of them as the edge would have the
 * next page start below them and skip everything between. They stay where they
 * are, and the page that reaches them takes them for duplicates.
 */
export function mergeNewestPage(state: TimelineState, events: readonly SessionEvent[]): TimelineState {
  const merged = mergeHistory(state, events);
  if (events.length === 0) return merged;
  const lowest = events.reduce((least, event) => Math.min(least, event.seq), Infinity);
  return merged.oldestSeq === lowest ? merged : { ...merged, oldestSeq: lowest };
}

/**
 * Merge events the device holds for a stretch this timeline missed: a page of a
 * repair, or an event that arrived behind newer ones. Unlike a page of older
 * history, the newer copy of a block wins — the tool call the page finished
 * replaces the one still running — and the cap applies, since these rows are
 * new at the tail.
 */
export function mergeMissed(state: TimelineState, events: readonly SessionEvent[]): TimelineState {
  return capOldest(mergeEvents(state, events, true));
}

/**
 * The one merge both of those are. `replaces` says whether a block the timeline
 * already holds gives way to a copy with a higher `seq`. A row is placed by its
 * ordering seq whichever end it lands on, so a block that began before the page
 * and finished inside it keeps the place it started in.
 */
function mergeEvents(
  state: TimelineState,
  events: readonly SessionEvent[],
  replaces: boolean,
): TimelineState {
  if (events.length === 0) return state;
  const items = { ...state.items };
  const placed = new Map<string, TimelineItem>();
  const moved = new Set<string>();
  let changed = false;
  // `session.history` pages back from `oldestSeq`, so only a page of older history
  // moves it down. An event that merely arrives behind newer ones does not join
  // the window: paging back from its seq would skip everything between it and
  // the rows the timeline holds, and that stretch would never be asked for.
  const lowest = events.reduce((least, event) => Math.min(least, event.seq), Infinity);
  const oldest =
    state.oldestSeq === null
      ? lowest
      : replaces
        ? state.oldestSeq
        : Math.min(state.oldestSeq, lowest);
  let newest = state.lastSeq;
  let optimistic = state.optimistic;
  for (const event of events) {
    if (event.seq > newest) newest = event.seq;
    // A page can carry the device's copy of a send this app is still showing.
    optimistic = reconcile(optimistic, event, false);
    const key = keyFor(event);
    const previous = items[key];
    if (previous && !(replaces && event.seq > previous.event.seq)) continue;
    const item = mergeItem(previous, event);
    if (!item) continue;
    items[key] = item;
    changed = true;
    if (previous === undefined || previous.seq !== item.seq) placed.set(key, item);
    if (previous !== undefined && previous.seq !== item.seq) moved.add(key);
  }
  // A duplicate changes nothing, and says so by being the same object.
  if (!changed && newest === state.lastSeq && oldest === state.oldestSeq && optimistic === state.optimistic) {
    return state;
  }
  // Both sides are already sorted by ordering seq, so merge instead of sorting.
  const added = [...placed.values()].sort((a, b) => a.seq - b.seq);
  const base = moved.size === 0 ? state.order : state.order.filter((key) => !moved.has(key));
  const order = added.length === 0 ? state.order : mergeSorted(added, base, items);
  return {
    ...state,
    order,
    items,
    lastSeq: newest,
    // A hole that was open stays open; otherwise everything up to `newest` is here.
    contiguousSeq: hasGap(state) ? state.contiguousSeq : newest,
    oldestSeq: oldest,
    optimistic,
  };
}

function mergeSorted(
  added: readonly TimelineItem[],
  existing: readonly string[],
  items: Record<string, TimelineItem>,
): string[] {
  const out: string[] = [];
  let i = 0;
  let j = 0;
  while (i < added.length && j < existing.length) {
    const left = added[i] as TimelineItem;
    const right = items[existing[j] as string];
    if (right === undefined || left.seq <= right.seq) {
      out.push(left.key);
      i += 1;
    } else {
      out.push(existing[j] as string);
      j += 1;
    }
  }
  for (; i < added.length; i += 1) out.push((added[i] as TimelineItem).key);
  for (; j < existing.length; j += 1) out.push(existing[j] as string);
  return out;
}

/** Replace one block with its full, untruncated version (`session.block`). */
export function replaceBlock(state: TimelineState, event: SessionEvent): TimelineState {
  const key = keyFor(event);
  const previous = state.items[key];
  if (!previous) return state;
  return {
    ...state,
    items: { ...state.items, [key]: { ...previous, ts: event.ts, event } },
  };
}

/**
 * The question the session is waiting on, if any (amendment A20). The composer
 * reads it to become an Answer button, so the newest one wins: a second
 * question can only arrive once the first was resolved or expired.
 */
export function selectPendingQuestion(state: TimelineState): QuestionEvent | null {
  for (let i = state.order.length - 1; i >= 0; i -= 1) {
    const event = state.items[state.order[i] as string]?.event;
    if (event?.kind === 'question' && event.status === 'pending') return event;
  }
  return null;
}

export interface TimelineView {
  roots: TimelineItem[];
  children: Record<string, TimelineItem[]>;
}

/**
 * How much of the timeline is drawn (`docs/DESIGN.md` § "The timeline"). Simple
 * shows only what is written to the person; Detailed adds everything the agent
 * did on the way. A41 made the level the account's, so it is a wire value and
 * lives with the protocol's own types.
 */

/** Kinds Simple never draws: not collapsed, not summarised, not counted. */
const WORKINGS = new Set(['thinking', 'tool_call']);

/** Kinds no level hides, wherever they sit: they are waiting for an answer. */
const ALWAYS_DRAWN = new Set(['approval', 'question']);

/**
 * A27: the device reports what a terminal would have printed for a slash
 * command as a `tool_call` whose `tool` and `title` are the command itself
 * (PROTOCOL.md §6.3). That block is not one of the agent's workings — it is
 * the answer to what the person asked for — so Simple draws it too, or
 * `/usage` would run and show nothing at all.
 */
const isCommandOutput = (event: SessionEvent): boolean =>
  event.kind === 'tool_call' && event.tool.startsWith('/');

/**
 * A34: a `user_message` another agent filed — a teammate's report, a background
 * task's notification. Nobody typed it, so it belongs with the agent's workings
 * rather than with the person's messages, and Simple hides it with them. The
 * rule therefore reads `source` as well as `kind`.
 */
const isAgentMessage = (event: SessionEvent): boolean =>
  event.kind === 'user_message' && event.source === 'agent';

const isWorkings = (event: SessionEvent): boolean =>
  !isCommandOutput(event) && (WORKINGS.has(event.kind) || isAgentMessage(event));

const drawnAt = (item: TimelineItem, detail: TimelineDetail): boolean =>
  detail === 'detailed' || !isWorkings(item.event);

/**
 * Split items into top-level rows and sub-agent children keyed by parent block,
 * then append the sends the device has not confirmed yet. A pending row carries
 * the `user_message` it will become, so the timeline renders it like any other.
 *
 * Simple drops the agent's workings along with everything nested under them,
 * except a card that needs an answer: it comes up to the top level rather than
 * going with the tool row that held it.
 */
export function selectView(state: TimelineState, detail: TimelineDetail): TimelineView {
  const roots: TimelineItem[] = [];
  const children: Record<string, TimelineItem[]> = {};
  for (const key of state.order) {
    const item = state.items[key];
    if (!item || !drawnAt(item, detail)) continue;
    const parent = item.parentBlockId ? state.items[item.parentBlockId] : undefined;
    if (parent && drawnAt(parent, detail)) {
      (children[parent.key] ??= []).push(item);
    } else if (!parent || ALWAYS_DRAWN.has(item.event.kind)) {
      roots.push(item);
    }
  }
  for (const block of state.optimistic) {
    if (state.items[block.id]) continue;
    roots.push(pendingItem(block));
  }
  return { roots, children };
}

function pendingItem(block: OptimisticBlock): TimelineItem {
  return {
    key: block.id,
    // Pending rows always sort last; they hold no device `seq`.
    seq: Number.MAX_SAFE_INTEGER,
    ts: block.at,
    parentBlockId: null,
    pending: block,
    event: {
      seq: 0,
      ts: block.at,
      kind: 'user_message',
      block_id: block.id,
      text: block.text,
      source: 'remote',
      ...(block.attachments.length > 0 ? { attachments: block.attachments } : {}),
    },
  };
}
