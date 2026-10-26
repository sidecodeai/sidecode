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
import { requestId } from '../lib/ids';
import { rpc, getSocket } from '../lib/gateway';
import { RequestError } from '../lib/ws';
import type { SendMode, SendResult } from '../protocol/frames';
import type {
  Attachment,
  OutgoingAttachment,
  QuestionAnswers,
  QueuedMessage,
  Session,
  SessionEvent,
  TodoItem,
  Usage,
} from '../protocol/types';
import {
  addOptimistic,
  applyEvent,
  applyEvents,
  dropQueued,
  emptyTimeline,
  keepOptimistic,
  markAccepted,
  mergeHistory,
  removeOptimistic,
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
  queue: QueuedMessage[];
  usage: Usage | null;
  ready: boolean;
  historyLoading: boolean;
  historyHasMore: boolean;
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
    queue: [],
    usage: null,
    ready: false,
    historyLoading: false,
    historyHasMore: true,
    error: null,
    closedSeq: null,
  };
}

/** Bumped by every `close`, so the order conversations were left in is exact. */
let closeTick = 0;
let epoch = 0;

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

/** Fold one state-only event into the chat's own side state. Pure. */
export function foldChat(chat: ChatSession, event: SessionEvent): ChatSession {
  switch (event.kind) {
    case 'todos':
      return { ...chat, todos: event.items };
    case 'queue':
      // A12: a queued item keeps the request id, so it names the pending row it
      // takes over from.
      return {
        ...chat,
        queue: event.pending,
        timeline: dropQueued(chat.timeline, event.pending.map((item) => item.id)),
      };
    case 'turn_completed':
      return event.usage ? { ...chat, usage: event.usage } : chat;
    default:
      return chat;
  }
}

/** Fold live state-only events into a single Session summary patch. Pure. */
export function foldSession(session: Session, events: readonly SessionEvent[]): Session {
  let next = session;
  for (const event of events) {
    switch (event.kind) {
      case 'todos':
        next = {
          ...next,
          todos: {
            total: event.items.length,
            done: event.items.filter((i) => i.status === 'completed').length,
          },
        };
        break;
      case 'queue':
        next = { ...next, queued: event.pending.length };
        break;
      case 'status':
        next = { ...next, state: event.state, state_detail: event.detail ?? null };
        break;
      case 'meta':
        next = {
          ...next,
          ...(event.title !== undefined ? { title: event.title } : {}),
          ...(event.model !== undefined ? { model: event.model } : {}),
          ...(event.permission_mode !== undefined
            ? { permission_mode: event.permission_mode }
            : {}),
          ...(event.effort !== undefined ? { effort: event.effort } : {}),
          ...(event.speed !== undefined ? { speed: event.speed } : {}),
          ...(event.cwd !== undefined ? { cwd: event.cwd } : {}),
          ...(event.git !== undefined ? { git: event.git } : {}),
          ...(event.control !== undefined ? { control: event.control } : {}),
        };
        break;
      case 'turn_started':
        next = { ...next, turn: { turn_id: event.turn_id, started_at: event.ts } };
        break;
      case 'turn_completed':
        next = { ...next, turn: null, ...(event.usage ? { usage: event.usage } : {}) };
        break;
      default:
        break;
    }
  }
  return next;
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
          // A resync starts the timeline again but keeps the unconfirmed sends.
          const base = result.resync
            ? { ...blank(deviceId, sessionId), timeline: keepOptimistic(chat.timeline) }
            : chat;
          // Buffered events carry todos and queue snapshots the timeline drops.
          const folded = result.events.reduce(foldChat, base);
          // Amendment A6: an explicit queue snapshot wins over replayed events.
          const queue = result.queue ? result.queue.pending : folded.queue;
          const timeline = dropQueued(
            applyEvents(folded.timeline, result.events),
            queue.map((item) => item.id),
          );
          return {
            ...folded,
            timeline,
            historyHasMore: folded.historyHasMore || timeline.dropped > folded.timeline.dropped,
            queue,
            usage: result.session.usage ?? folded.usage,
            ready: true,
            error: null,
          };
        });
        const chat = get().sessions[key];
        const needsHistory =
          result.resync || (chat !== undefined && chat.timeline.order.length === 0);
        if (needsHistory) void get().loadOlder(key);
      },
      onError: (err) => {
        if (sent !== epoch) return;
        patch(set, key, (chat) => ({
          ...chat,
          ready: true,
          error: err instanceof Error ? err.message : 'subscribe failed',
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
    if (event.seq <= entry.timeline.lastSeq) return;
    getSocket()?.updateCursor(sessionId, event.seq);
    patch(set, key, (chat) => {
      const folded = foldChat(chat, event);
      const timeline = applyEvent(folded.timeline, event);
      return {
        ...folded,
        timeline,
        // The cap dropped the oldest rows, so there is older history again
        // even where the last page said there was none.
        historyHasMore: folded.historyHasMore || timeline.dropped > folded.timeline.dropped,
      };
    });
    upsertSessionFrom(key, [event]);
  },

  loadOlder: async (key) => {
    const chat = get().sessions[key];
    if (!chat || chat.historyLoading || !chat.historyHasMore) return;
    const sent = epoch;
    // Only the first page restores details, and only until a live event arrives.
    // History never changes the authoritative session summary.
    const isNewestPage = chat.timeline.oldestSeq === null;
    const requestedSeq = chat.timeline.lastSeq;
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
        const folded =
          isNewestPage && c.timeline.lastSeq === requestedSeq
            ? result.events.reduce(foldChat, c)
            : c;
        return {
          ...folded,
          timeline: mergeHistory(folded.timeline, result.events),
          historyLoading: false,
          historyHasMore: result.has_more,
        };
      });
    } catch (err) {
      if (sent !== epoch) return;
      patch(set, key, (c) => ({
        ...c,
        historyLoading: false,
        error: err instanceof Error ? err.message : 'history failed',
      }));
    }
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
