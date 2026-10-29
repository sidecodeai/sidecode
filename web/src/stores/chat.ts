/**
 * Per-session chat state: timeline, todos, queue, usage and the request actions
 * a session view needs. Live frames arrive through `ingestEvent`.
 *
 * State-only events never become timeline rows. Replayed events restore chat
 * details, while subscribe summaries and live frames own the current session
 * state (PROTOCOL-FROZEN.md §8 and amendment A6).
 */
import { create } from 'zustand';
import { base64Size } from '../lib/base64';
import { errorText } from '../lib/errors';
import { requestId } from '../lib/ids';
import { rpc, getSocket } from '../lib/gateway';
import { RequestError } from '../lib/ws';
import { strings } from '../strings';
import type { SendMode, SendResult } from '../protocol/frames';
import type {
  Attachment,
  OutgoingAttachment,
  QuestionAnswers,
  QueuedMessage,
  SessionEvent,
  TodoItem,
  Usage,
} from '../protocol/types';
import { foldChat, foldLate, foldPage, foldSession } from './chatFold';
import { REPAIR_PAGES, absorbMissed, adopt, owedAfterSubscribe, restart } from './chatSync';
import {
  addOptimistic,
  applyEvent,
  applyEvents,
  beginRepair,
  coverThrough,
  dropQueued,
  emptyTimeline,
  hasGap,
  markAccepted,
  mergeHistory,
  mergeNewestPage,
  removeOptimistic,
  reopenGap,
  replaceBlock,
  type TimelineState,
} from './timeline';
import { useCommands } from './commands';
import { useSessions, sessionKey } from './sessions';
import { useOutbox, type PendingSend } from './outbox';

const HISTORY_PAGE = 200;

/**
 * How many closed conversations keep their transcript. A tab left open all day
 * used to hold every session it had ever visited, timeline and all; the ones
 * the reader is moving between are worth keeping, the rest are a page of
 * history away. The current conversation is never among them: `close` runs on
 * a reconnect too, and evicting it there would blank the screen.
 */
const KEEP_CLOSED = 3;

export interface ChatSession {
  key: string;
  deviceId: string;
  sessionId: string;
  timeline: TimelineState;
  todos: TodoItem[];
  /** The `seq` of the todos snapshot held: a snapshot not newer than it is stale. */
  todosSeq: number;
  queue: QueuedMessage[];
  /**
   * The `seq` of the queue snapshot held. History never carries a queue event
   * (PROTOCOL 6.4), so the timeline's cursor can pass one that is still on its
   * way; it is judged by this instead.
   */
  queueSeq: number;
  usage: Usage | null;
  ready: boolean;
  historyLoading: boolean;
  historyHasMore: boolean;
  /**
   * Whether the newest page of history has been read. Until then an empty
   * timeline says nothing about the conversation, and a failure to read it is
   * the one the reader is told about.
   */
  historyLoaded: boolean;
  /**
   * Whether a repair is asking the device for what the transcript missed. A
   * conversation with no rows is not known to be empty while it runs.
   */
  repairing: boolean;
  /**
   * How many times the rows were started again from the newest page. A page read
   * that began in an earlier generation belongs to rows that are gone, and its
   * answer is dropped.
   */
  generation: number;
  /**
   * Why the latest state of the conversation could not be read — the subscribe,
   * its newest page or the repair of a gap failed — in the app's words, or null.
   * An older page that fails is not reported: scrolling up asks for it again.
   */
  error: string | null;
  /**
   * When the view was left, as a monotonic stamp rather than a clock: several
   * conversations can be left inside one millisecond, and the order they were
   * left in is what decides which ones stay.
   */
  closedSeq: number | null;
}

function blank(deviceId: string, sessionId: string): ChatSession {
  return {
    key: sessionKey(deviceId, sessionId),
    deviceId,
    sessionId,
    timeline: emptyTimeline(),
    todos: [],
    todosSeq: 0,
    queue: [],
    queueSeq: 0,
    usage: null,
    ready: false,
    historyLoading: false,
    historyHasMore: true,
    historyLoaded: false,
    repairing: false,
    generation: 0,
    error: null,
    closedSeq: null,
  };
}

/**
 * Asks for a hole nobody is fetching: one noticed while a failure held the
 * repair back, or while another repair was under way. A failure waits for its
 * retry rather than being asked again by every event.
 */
function repairIfOpen(get: () => ChatState, key: string): void {
  const chat = get().sessions[key];
  if (chat && hasGap(chat.timeline) && chat.error === null) void get().repair(key);
}

/** Bumped by every `close`, so the order conversations were left in is exact. */
let closeTick = 0;
let epoch = 0;

/** The conversations with a repair in flight: a burst of skipped events asks once. */
const repairsInFlight = new Set<string>();

/**
 * Keep the open conversations and the `KEEP_CLOSED` most recently left ones.
 * The slash-command list of an evicted conversation goes with it: it is the
 * same never-cleared shape, and it is re-fetched the moment one is opened.
 */
function evictClosed(sessions: Record<string, ChatSession>): Record<string, ChatSession> {
  const closed = Object.values(sessions)
    .filter((chat) => chat.closedSeq !== null)
    .sort((a, b) => (b.closedSeq ?? 0) - (a.closedSeq ?? 0));
  if (closed.length <= KEEP_CLOSED) return sessions;
  const next = { ...sessions };
  for (const chat of closed.slice(KEEP_CLOSED)) {
    delete next[chat.key];
    useCommands.getState().clear(chat.sessionId);
  }
  return next;
}

interface ChatState {
  sessions: Record<string, ChatSession>;
  open: (deviceId: string, sessionId: string) => void;
  close: (deviceId: string, sessionId: string) => void;
  ingestEvent: (sessionId: string, event: SessionEvent, deviceId?: string) => void;
  loadOlder: (key: string) => Promise<void>;
  /**
   * Fill what the transcript is missing: ask the device for everything after the
   * last contiguous event (`session.history` with `after_seq`, A9) and merge it
   * by block, whatever the cause — a live event that skipped a `seq`, or a
   * cursor the gateway could not serve from its buffer (`resync`), in which case
   * the rows on screen stay while the device is asked.
   */
  repair: (key: string) => Promise<void>;
  /**
   * Try again whatever failed: subscribe afresh, which reads the newest page if
   * none was read and repairs what is missing. A load or a repair already on
   * its way is left to finish.
   */
  retry: (key: string) => void;
  expandBlock: (key: string, blockId: string) => Promise<void>;
  send: (
    key: string,
    input: {
      text: string;
      attachments?: OutgoingAttachment[];
      mode: SendMode;
      /** A43: the `ts` of the queue entry an edited message goes back in place of. */
      queueTs?: number;
    },
  ) => Promise<void>;
  retrySend: (id: string) => Promise<void>;
  /** A27: run one slash command; the row appears under the request's own id. */
  runCommand: (key: string, name: string, argument?: string) => Promise<void>;
  stop: (key: string) => Promise<void>;
  approve: (key: string, requestId: string, optionId: string, message?: string) => Promise<void>;
  answer: (key: string, requestId: string, answers: QuestionAnswers) => Promise<void>;
  removeQueued: (key: string, queuedId: string) => Promise<void>;
  /** Sign-out: nothing of the previous account stays in the tab (`signOut`). */
  reset: () => void;
}

function patch(
  set: (fn: (s: ChatState) => Partial<ChatState>) => void,
  key: string,
  update: (chat: ChatSession) => ChatSession,
): void {
  set((s) => {
    const chat = s.sessions[key];
    if (!chat) return {};
    return { sessions: { ...s.sessions, [key]: update(chat) } };
  });
}

/** Apply live events to the session summary with a single store write. */
function upsertSessionFrom(key: string, events: readonly SessionEvent[]): void {
  const sessions = useSessions.getState();
  const session = sessions.sessions[key];
  if (!session) return;
  const next = foldSession(session, events);
  if (next !== session) sessions.upsert(next);
}

export const useChat = create<ChatState>((set, get) => ({
  sessions: {},

  open: (deviceId, sessionId) => {
    const sent = epoch;
    const key = sessionKey(deviceId, sessionId);
    if (!get().sessions[key]) {
      set((s) => ({ sessions: { ...s.sessions, [key]: blank(deviceId, sessionId) } }));
    } else {
      patch(set, key, (chat) => (chat.closedSeq === null ? chat : { ...chat, closedSeq: null }));
    }
    const socket = getSocket();
    if (!socket) return;
    const existing = get().sessions[key];
    socket.subscribe(sessionId, existing?.timeline.lastSeq || undefined, {
      onResult: (result) => {
        if (sent !== epoch) return;
        // Replayed state events can predate this summary after a device reconnect.
        // last_seq is an event cursor, not a version of the summary's state.
        useSessions.getState().upsert(result.session);
        patch(set, key, (chat) => {
          // A resync no longer clears the transcript: the rows stay where the
          // reader left them, and `repair` joins what the device holds after the
          // cursor. Only a conversation with nothing to keep asks for the newest
          // page afresh, which wants `historyHasMore` back.
          const base =
            result.resync && chat.timeline.order.length === 0
              ? { ...chat, historyHasMore: true, historyLoaded: false }
              : chat;
          // Buffered events carry todos and queue snapshots the timeline drops.
          const folded = result.events.reduce(foldChat, base);
          // Amendment A6: an explicit queue snapshot wins over replayed events.
          const queue = result.queue ? result.queue.pending : folded.queue;
          const timeline = dropQueued(
            applyEvents(folded.timeline, result.events),
            queue.map((item) => item.id),
          );
          const next = {
            ...adopt(folded, timeline),
            queue,
            usage: result.session.usage ?? folded.usage,
            ready: true,
          };
          // The subscribe worked. A failure of what it still owes from the
          // device stays in view until that is paid.
          return owedAfterSubscribe(next, result.resync) === null ? { ...next, error: null } : next;
        });
        const chat = get().sessions[key];
        if (chat === undefined) return;
        const owed = owedAfterSubscribe(chat, result.resync);
        if (owed === 'newest') void get().loadOlder(key);
        else if (owed === 'repair') void get().repair(key);
      },
      onError: (err) => {
        if (sent !== epoch) return;
        patch(set, key, (chat) => ({
          ...chat,
          ready: true,
          error: errorText(err, strings.errors.historyFailed),
        }));
      },
    });
  },

  close: (deviceId, sessionId) => {
    getSocket()?.unsubscribe(sessionId);
    const key = sessionKey(deviceId, sessionId);
    set((s) => {
      const chat = s.sessions[key];
      if (!chat) return {};
      closeTick += 1;
      return {
        sessions: evictClosed({ ...s.sessions, [key]: { ...chat, closedSeq: closeTick } }),
      };
    });
  },

  reset: () => {
    epoch += 1;
    set({ sessions: {} });
  },

  ingestEvent: (sessionId, event, deviceId) => {
    const state = get();
    // `session_id` is globally unique (amendment A5); `device_id` only narrows
    // the lookup when the gateway supplied it.
    const entry = deviceId
      ? (state.sessions[sessionKey(deviceId, sessionId)] ??
        Object.values(state.sessions).find((c) => c.sessionId === sessionId))
      : Object.values(state.sessions).find((c) => c.sessionId === sessionId);
    if (!entry) return;
    const key = entry.key;
    // At or below the cursor an event moves no cursor and leaves the session's
    // state alone. It can be a block the transcript is missing, which the reducer
    // merges, or a todos or queue snapshot newer than the one held.
    if (event.seq <= entry.timeline.lastSeq) {
      patch(set, key, (chat) => {
        const folded = foldLate(chat, event);
        const timeline = applyEvent(folded.timeline, event);
        return timeline === folded.timeline ? folded : adopt(folded, timeline);
      });
      return;
    }
    getSocket()?.updateCursor(sessionId, event.seq);
    patch(set, key, (chat) => {
      const folded = foldChat(chat, event);
      return adopt(folded, applyEvent(folded.timeline, event));
    });
    upsertSessionFrom(key, [event]);
    // A skipped `seq` is asked for at once.
    repairIfOpen(get, key);
  },

  loadOlder: async (key) => {
    const chat = get().sessions[key];
    if (!chat || chat.historyLoading || !chat.historyHasMore) return;
    const sent = epoch;
    // The rows this page is for: if they are started again before it answers,
    // it has nothing to join and is dropped.
    const generation = chat.generation;
    // Only the first page restores details, and only until a live event arrives.
    // History never changes the authoritative session summary.
    const isNewestPage = chat.timeline.oldestSeq === null;
    // Until the newest page has been read, a failure is the one the reader is
    // told about; an older page that fails is asked for again by scrolling up.
    const first = !chat.historyLoaded;
    const requestedSeq = chat.timeline.lastSeq;
    // What the device counted as its last event when the newest page was asked
    // for: the page holds everything stored up to it.
    const counted = isNewestPage ? (useSessions.getState().sessions[key]?.last_seq ?? 0) : 0;
    patch(set, key, (c) => ({ ...c, historyLoading: true }));
    try {
      const params: { session_id: string; before_seq?: number; limit: number } = {
        session_id: chat.sessionId,
        limit: HISTORY_PAGE,
      };
      if (chat.timeline.oldestSeq !== null) params.before_seq = chat.timeline.oldestSeq;
      const result = await rpc('session.history', params);
      if (sent !== epoch) return;
      patch(set, key, (c) => {
        if (c.generation !== generation) return c;
        const folded = foldPage(c, result.events, isNewestPage && c.timeline.lastSeq === requestedSeq);
        return {
          ...folded,
          timeline: coverThrough(
            isNewestPage
              ? mergeNewestPage(folded.timeline, result.events)
              : mergeHistory(folded.timeline, result.events),
            counted,
          ),
          historyLoading: false,
          historyHasMore: result.has_more,
          historyLoaded: true,
          error: first ? null : c.error,
        };
      });
      // A hole noticed while the failure held the repair back is asked for now.
      repairIfOpen(get, key);
    } catch (err) {
      if (sent !== epoch) return;
      patch(set, key, (c) =>
        c.generation !== generation
          ? c
          : {
              ...c,
              historyLoading: false,
              error: first ? errorText(err, strings.errors.historyFailed) : c.error,
            },
      );
    }
  },

  repair: async (key) => {
    const chat = get().sessions[key];
    if (!chat || repairsInFlight.has(key)) return;
    const sent = epoch;
    const after = chat.timeline.contiguousSeq;
    // What the device counted as its last event when the repair began: once the
    // walk is complete, nothing is missing up to it.
    const counted = useSessions.getState().sessions[key]?.last_seq ?? 0;
    repairsInFlight.add(key);
    patch(set, key, (c) => ({ ...c, repairing: true, timeline: beginRepair(c.timeline) }));
    try {
      let cursor = after;
      for (let page = 0; page < REPAIR_PAGES; page += 1) {
        const requested = get().sessions[key]?.timeline.lastSeq ?? 0;
        const result = await rpc('session.history', {
          session_id: chat.sessionId,
          after_seq: cursor,
          limit: HISTORY_PAGE,
        });
        if (sent !== epoch) return;
        // A live event that arrived while the page was on its way is newer than
        // anything the page says about the session's state.
        patch(set, key, (c) => absorbMissed(c, result.events, c.timeline.lastSeq === requested));
        const last = result.events.at(-1);
        if (!result.has_more || !last) {
          patch(set, key, (c) => ({
            ...c,
            timeline: coverThrough(c.timeline, counted),
            error: null,
          }));
          break;
        }
        cursor = last.seq;
        if (page === REPAIR_PAGES - 1) {
          // Too much to join: start the transcript again from the newest page.
          patch(set, key, restart);
          void get().loadOlder(key);
          return;
        }
      }
    } catch (err) {
      if (sent !== epoch) return;
      patch(set, key, (c) => ({
        ...c,
        timeline: reopenGap(c.timeline, after),
        error: errorText(err, strings.errors.historyFailed),
      }));
      return;
    } finally {
      repairsInFlight.delete(key);
      patch(set, key, (c) => (c.repairing ? { ...c, repairing: false } : c));
    }
    // A skip noticed while the pages were on their way opened a new hole.
    repairIfOpen(get, key);
  },

  retry: (key) => {
    const chat = get().sessions[key];
    if (!chat || chat.historyLoading || repairsInFlight.has(key)) return;
    get().open(chat.deviceId, chat.sessionId);
  },

  expandBlock: async (key, blockId) => {
    const chat = get().sessions[key];
    if (!chat) return;
    const sent = epoch;
    const result = await rpc('session.block', { session_id: chat.sessionId, block_id: blockId });
    if (sent !== epoch) return;
    patch(set, key, (c) => ({ ...c, timeline: replaceBlock(c.timeline, result.event) }));
  },

  send: async (key, input) => {
    const chat = get().sessions[key];
    if (!chat) return;
    // A12: the request id is the block id the device will echo, so the message
    // can be rendered now and replaced in place when the device confirms it.
    const id = requestId();
    const attachments = input.attachments ?? [];
    const at = Date.now();
    showPending(set, key, { id, text: input.text, attachments, at });
    const pending: PendingSend = {
      id,
      sessionKey: key,
      sessionId: chat.sessionId,
      text: input.text,
      attachments,
      mode: input.mode,
      ...(input.queueTs === undefined ? {} : { queueTs: input.queueTs }),
      at,
      error: null,
    };
    useOutbox.getState().add(pending);
    await settle(set, key, id, deliver(pending));
  },

  retrySend: async (id) => {
    const entry = useOutbox.getState().pending[id];
    if (!entry) return;
    useOutbox.getState().add({ ...entry, error: null });
    // The row is normally still there; a retry after a resync has to re-add it.
    showPending(set, entry.sessionKey, {
      id,
      text: entry.text,
      attachments: entry.attachments,
      at: entry.at,
    });
    await settle(set, entry.sessionKey, id, deliver(entry));
  },

  /**
   * A27: `session.command` echoes the command as a `user_message` under this
   * request's id, exactly as `session.send` echoes a message, so the row is
   * drawn now and replaced in place when the device confirms it. The result is
   * `{}` and the outcome arrives as events; there is nothing to queue and
   * nothing to retry, so a refusal takes the row away and travels on to the
   * composer, which reports it under the field.
   */
  runCommand: async (key, name, argument) => {
    const chat = get().sessions[key];
    if (!chat) return;
    const id = requestId();
    const text = argument ? `/${name} ${argument}` : `/${name}`;
    showPending(set, key, { id, text, attachments: [], at: Date.now() });
    try {
      await rpc(
        'session.command',
        { session_id: chat.sessionId, name, ...(argument ? { argument } : {}) },
        { id },
      );
    } catch (err) {
      patch(set, key, (c) => ({ ...c, timeline: removeOptimistic(c.timeline, id) }));
      throw err;
    }
    // The device took it, so the row is no longer an uncertain delivery; it
    // stands until the echo arrives under the same id.
    patch(set, key, (c) => ({ ...c, timeline: markAccepted(c.timeline, id, 'sent') }));
  },

  stop: async (key) => {
    const chat = get().sessions[key];
    if (!chat) return;
    await rpc('session.stop', { session_id: chat.sessionId });
  },

  approve: async (key, request_id, option_id, message) => {
    const chat = get().sessions[key];
    if (!chat) return;
    await rpc('session.approve', {
      session_id: chat.sessionId,
      request_id,
      option_id,
      ...(message ? { message } : {}),
    });
  },

  answer: async (key, request_id, answers) => {
    const chat = get().sessions[key];
    if (!chat) return;
    await rpc('session.answer', { session_id: chat.sessionId, request_id, answers });
  },

  removeQueued: async (key, queued_id) => {
    const chat = get().sessions[key];
    if (!chat) return;
    await rpc('session.queue_remove', { session_id: chat.sessionId, queued_id });
    // A12: the queue entry carries the request id, so a send left unconfirmed
    // must not go on offering a Retry for a message that is gone.
    useOutbox.getState().clear(queued_id);
  },
}));

type Setter = (fn: (s: ChatState) => Partial<ChatState>) => void;

/** Put a message in the timeline before the device has echoed it back. */
function showPending(
  set: Setter,
  key: string,
  input: { id: string; text: string; attachments: OutgoingAttachment[]; at: number },
): void {
  patch(set, key, (chat) => ({
    ...chat,
    timeline: addOptimistic(chat.timeline, {
      id: input.id,
      text: input.text,
      attachments: input.attachments.map(metadataOf),
      at: input.at,
    }),
  }));
}

/** A3: the wire carries base64 and the device computes the size; so do we. */
function metadataOf(attachment: OutgoingAttachment): Attachment {
  return {
    name: attachment.name,
    mime: attachment.mime,
    size: base64Size(attachment.data_base64),
  };
}

/**
 * Apply the outcome of a `session.send` to its pending row. A definite refusal
 * takes the row away, and so does `queued`: the composer's Up next list stands
 * for the message until the device dequeues it and emits the `user_message`
 * under the same id (A43).
 *
 * `sent` and `steered` leave the row where it is and only note the answer. A
 * steered message reaches the agent at its next step, so the device emits its
 * block after whatever was already streaming (A14); the row waits there, for
 * however long that takes, and no longer counts as an uncertain delivery.
 */
async function settle(
  set: Setter,
  key: string,
  id: string,
  delivery: Promise<SendResult | null>,
): Promise<void> {
  let result: SendResult | null;
  try {
    result = await delivery;
  } catch (err) {
    patch(set, key, (chat) => ({ ...chat, timeline: removeOptimistic(chat.timeline, id) }));
    throw err;
  }
  if (result?.accepted === 'queued') {
    patch(set, key, (chat) => ({ ...chat, timeline: removeOptimistic(chat.timeline, id) }));
  } else if (result?.accepted === 'sent' || result?.accepted === 'steered') {
    const accepted = result.accepted;
    patch(set, key, (chat) => ({ ...chat, timeline: markAccepted(chat.timeline, id, accepted) }));
  }
}

/** Error codes that mean the gateway definitely rejected the message. */
const DEFINITE_FAILURES = new Set([
  'bad_request',
  'unauthorized',
  'forbidden',
  'not_found',
  'conflict',
  'agent_unavailable',
  'unsupported',
  'too_large',
  'device_offline',
]);

/**
 * Send what the outbox holds under its own request id. A Retry sends the same
 * entry again, so everything the first attempt carried — the files, the mode,
 * and the A43 `queue_ts` that puts an edited message back in its place — goes
 * with it.
 */
async function deliver(send: PendingSend): Promise<SendResult | null> {
  const outbox = useOutbox.getState();
  try {
    const result = await rpc(
      'session.send',
      {
        session_id: send.sessionId,
        text: send.text,
        mode: send.mode,
        ...(send.attachments.length > 0 ? { attachments: send.attachments } : {}),
        ...(send.queueTs === undefined ? {} : { queue_ts: send.queueTs }),
      },
      { id: send.id },
    );
    outbox.clear(send.id);
    return result;
  } catch (err) {
    const code = err instanceof RequestError ? err.code : 'internal';
    const message = err instanceof Error ? err.message : 'send failed';
    if (DEFINITE_FAILURES.has(code)) {
      outbox.clear(send.id);
      throw err;
    }
    // Uncertain delivery: never auto-resend, let the user retry with the same id.
    outbox.fail(send.id, message);
    return null;
  }
}

export const chatOf = (key: string) => (s: ChatState): ChatSession | undefined => s.sessions[key];
