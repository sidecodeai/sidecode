/**
 * Folds of the state-only events — todos, queue, status, meta, turns — into the
 * side state they describe: the conversation's own (`foldChat`) and the session
 * summary (`foldSession`). Pure, so the store, a subscribe replay and a page of
 * history all read these events the same way.
 */
import type { Session, SessionEvent } from '../protocol/types';
import type { ChatSession } from './chat';
import { dropQueued } from './timeline';

/**
 * Fold one state-only event into the chat's own side state. Pure. A todos or
 * queue snapshot is held with its `seq`, and one that is not newer than the
 * snapshot held is stale: that is judged by the snapshots alone, never by the
 * timeline's cursor, which a page of history or a count of the device's last
 * event can move past a snapshot that is still on its way.
 */
export function foldChat(chat: ChatSession, event: SessionEvent): ChatSession {
  switch (event.kind) {
    case 'todos':
      return event.seq > chat.todosSeq
        ? { ...chat, todos: event.items, todosSeq: event.seq }
        : chat;
    case 'queue':
      // A12: a queued item keeps the request id, so it names the pending row it
      // takes over from.
      return event.seq > chat.queueSeq
        ? {
            ...chat,
            queue: event.pending,
            queueSeq: event.seq,
            timeline: dropQueued(chat.timeline, event.pending.map((item) => item.id)),
          }
        : chat;
    case 'turn_completed':
      return event.usage ? { ...chat, usage: event.usage } : chat;
    default:
      return chat;
  }
}

/**
 * Fold a page of history. The snapshots carry their `seq`, so only the usage a
 * completed turn reports — a figure with no version — waits for `quiet`: no
 * live event has arrived since the page was asked for.
 */
export function foldPage(
  chat: ChatSession,
  events: readonly SessionEvent[],
  quiet: boolean,
): ChatSession {
  return events.reduce(
    (next, event) => (event.kind === 'turn_completed' && !quiet ? next : foldChat(next, event)),
    chat,
  );
}

/**
 * Fold an event the timeline's cursor has already passed: only a snapshot, and
 * only if it is newer than the one held. Everything else it says is old news.
 */
export function foldLate(chat: ChatSession, event: SessionEvent): ChatSession {
  return event.kind === 'todos' || event.kind === 'queue' ? foldChat(chat, event) : chat;
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
