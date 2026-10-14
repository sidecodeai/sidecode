/**
 * Pure timeline reducer: session events in, ordered renderable items out.
 *
 * Contract rules implemented here (PROTOCOL-FROZEN.md §4, §7, A8):
 *  - a later event with the same `block_id` fully replaces the earlier one,
 *    except `delta` events on `assistant_text` / `thinking`, which append;
 *  - events whose `seq` is not greater than the last applied one are dropped;
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
   * What `session.send` answered, once it has. A `steered` message waits for the
   * agent to reach its next step (A14), which takes as long as it takes, so the
   * wait says nothing about delivery.
   */
  accepted?: 'sent' | 'steered';
}

export interface TimelineState {
  order: string[];
  items: Record<string, TimelineItem>;
  lastSeq: number;
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
  return { order: [], items: {}, lastSeq: 0, oldestSeq: null, optimistic: [], dropped: 0 };
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
  return { ...state, optimistic: [...state.optimistic, block] };
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
 * Retire the pending rows an incoming event confirms. The id match is A12; the
 * text match is the fallback for a device that still mints its own block ids,
 * and only ever retires one row per event.
 */
function reconcile(optimistic: OptimisticBlock[], event: SessionEvent): OptimisticBlock[] {
  if (optimistic.length === 0) return optimistic;
  const key = keyFor(event);
  if (optimistic.some((b) => b.id === key)) return optimistic.filter((b) => b.id !== key);
  if (event.kind !== 'user_message' || event.source !== 'remote') return optimistic;
  const index = optimistic.findIndex((b) => b.text === event.text);
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

/** Apply a live event. Out-of-order or duplicate frames are dropped. */
export function applyEvent(state: TimelineState, event: SessionEvent): TimelineState {
  if (event.seq <= state.lastSeq) return state;
  const key = keyFor(event);
  const previous = state.items[key];
  const item = mergeItem(previous, event);
  const lastSeq = event.seq;
  const oldestSeq = state.oldestSeq === null ? event.seq : Math.min(state.oldestSeq, event.seq);
  const optimistic = reconcile(state.optimistic, event);
  // A state-only event still moves the window: it is the oldest event this
  // session delivered, so history must be asked for what came before it and
  // not for it again.
  if (!item) return { ...state, lastSeq, oldestSeq, optimistic };

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
  return capOldest({ ...state, order, items, lastSeq, oldestSeq, optimistic });
}

export function applyEvents(state: TimelineState, events: readonly SessionEvent[]): TimelineState {
  return events.reduce(applyEvent, state);
}

/**
 * Merge an older history page (ascending `seq`, ending before the oldest known
 * event). Existing items win: they are at least as fresh as the history copy.
 */
export function mergeHistory(state: TimelineState, events: readonly SessionEvent[]): TimelineState {
  if (events.length === 0) return state;
  const items = { ...state.items };
  const added: TimelineItem[] = [];
  let oldest = state.oldestSeq;
  let optimistic = state.optimistic;
  for (const event of events) {
    if (oldest === null || event.seq < oldest) oldest = event.seq;
    // A page can carry the device's copy of a send this app is still showing.
    optimistic = reconcile(optimistic, event);
    const key = keyFor(event);
    if (items[key]) continue;
    const item = mergeItem(undefined, event);
    if (!item) continue;
    items[key] = item;
    added.push(item);
  }
  // Both sides are already sorted by ordering seq, so merge instead of sorting.
  added.sort((a, b) => a.seq - b.seq);
  const order = mergeSorted(added, state.order, items);
  // Not capped: these are the rows the reader asked for by scrolling back, and
  // dropping them here would page the same events in for ever.
  return {
    ...state,
    order,
    items,
    lastSeq: Math.max(state.lastSeq, events[events.length - 1]?.seq ?? 0),
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
