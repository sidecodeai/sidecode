/**
 * A62 — each conversation's task board as this tab last heard it from the
 * gateway: the board, or none, and why it is or is not kept current. The
 * gateway makes and stores the board; this is the tab's copy, in memory only,
 * for the account signed in.
 *
 * Three things write here: the reply to `board.get`, sent once the
 * conversation's subscribe has answered — when it opens, after a reconnect
 * subscribed it again, and when it comes back to the front — the reply to
 * `board.refresh`, which carries a status alone; and every `board.updated` the
 * socket receives for a board it watches. The gateway numbers every write of
 * any board from one counter, so a conversation's `revision` only grows, by
 * steps of any size, even across a board deleted and made again: a board with a
 * lower revision than the one held is older and is dropped with its status. A
 * frame with no board means the gateway holds none: the copy goes, and the
 * frame's status is taken.
 *
 * Turning the board off deletes every board of the account on the gateway, so
 * every copy here reads `off` at once. Sign-out empties the store, and so does a
 * `hello` for another account.
 */
import { create } from 'zustand';
import { rpc } from '../lib/gateway';
import { RequestError } from '../lib/ws';
import type { BoardUpdatedFrame } from '../protocol/frames';
import type { Board, BoardStatus } from '../protocol/types';
import { sessionKey, type SessionKey } from './sessions';

export interface BoardEntry {
  board: Board | null;
  status: BoardStatus;
}

const KNOWN_STATES = ['off', 'unavailable', 'current', 'updating', 'stale'];

const OFF: BoardStatus = { state: 'off', reason: null };

/** A gateway that keeps no boards: the chip goes, as for `board.enabled: false`. */
const UNSUPPORTED: BoardStatus = { state: 'unavailable', reason: 'unsupported' };

/** A state this build does not know reads as `stale`: the board stays and Retry is offered. */
export const readBoardStatus = (status: BoardStatus): BoardStatus =>
  KNOWN_STATES.includes(status.state) ? status : { ...status, state: 'stale' };

/** Whether `next` may replace `held`: no board at all always does, an older one never. */
export const supersedes = (held: Board | null, next: Board | null): boolean =>
  next === null || held === null || next.revision >= held.revision;

interface BoardsState {
  boards: Record<SessionKey, BoardEntry>;
  /** Ask for the stored board and its status; one request per conversation at a time. */
  get: (deviceId: string, sessionId: string) => Promise<void>;
  /** Ask the gateway to update the board now, or to start it over. */
  refresh: (deviceId: string, sessionId: string, rebuild?: boolean) => Promise<void>;
  /** A `board.updated` frame. */
  apply: (frame: BoardUpdatedFrame) => void;
  /** The account has the board off: the gateway keeps none of its boards. */
  turnedOff: () => void;
  /** `hello` named the account; one other than the boards belong to empties the store. */
  forAccount: (username: string) => void;
  /** Sign-out: nothing of the previous account stays in the tab (`signOut`). */
  reset: () => void;
}

/** Bumped by every reset, so a reply sent for the previous account writes nothing. */
let epoch = 0;
/** The account the copies belong to, from the last `hello`. */
let account: string | null = null;
/** The conversations whose `board.get` is out. */
const asking = new Set<SessionKey>();

type Boards = Record<SessionKey, BoardEntry>;

/** `boards` with what the gateway said of one conversation, unless it is older than the copy. */
function withAnswer(boards: Boards, key: SessionKey, board: Board | null, status: BoardStatus): Boards {
  const held = boards[key]?.board ?? null;
  if (!supersedes(held, board)) return boards;
  return { ...boards, [key]: { board, status: readBoardStatus(status) } };
}

/** `boards` with a new status for one conversation and its board as held. */
function withStatus(boards: Boards, key: SessionKey, status: BoardStatus): Boards {
  return { ...boards, [key]: { board: boards[key]?.board ?? null, status: readBoardStatus(status) } };
}

/** A refusal that says the gateway keeps no boards, which no later request changes. */
const unsupported = (err: unknown): boolean => err instanceof RequestError && err.code === 'unsupported';

export const useBoards = create<BoardsState>((set) => ({
  boards: {},

  get: async (deviceId, sessionId) => {
    const key = sessionKey(deviceId, sessionId);
    if (asking.has(key)) return;
    asking.add(key);
    const sent = epoch;
    try {
      const result = await rpc('board.get', { session_id: sessionId });
      if (sent === epoch) set((s) => ({ boards: withAnswer(s.boards, key, result.board, result.status) }));
    } catch (err) {
      // Any other failure keeps what is held; the next open or return to the front asks again.
      if (sent === epoch && unsupported(err)) set((s) => ({ boards: withStatus(s.boards, key, UNSUPPORTED) }));
    } finally {
      if (sent === epoch) asking.delete(key);
    }
  },

  refresh: async (deviceId, sessionId, rebuild = false) => {
    const key = sessionKey(deviceId, sessionId);
    const sent = epoch;
    const params = rebuild ? { session_id: sessionId, rebuild } : { session_id: sessionId };
    try {
      const result = await rpc('board.refresh', params);
      if (sent === epoch) set((s) => ({ boards: withStatus(s.boards, key, result.status) }));
    } catch (err) {
      if (sent === epoch && unsupported(err)) set((s) => ({ boards: withStatus(s.boards, key, UNSUPPORTED) }));
    }
  },

  apply: (frame) => {
    const key = sessionKey(frame.device_id, frame.session_id);
    set((s) => ({ boards: withAnswer(s.boards, key, frame.board, frame.status) }));
  },

  turnedOff: () =>
    set((s) => {
      // A copy this already turned off is left alone, so every later frame that
      // says the board is still off redraws nothing.
      const on = Object.keys(s.boards).filter((key) => s.boards[key]?.status !== OFF);
      if (on.length === 0) return {};
      const off = Object.fromEntries(on.map((key) => [key, { board: null, status: OFF }]));
      return { boards: { ...s.boards, ...off } };
    }),

  forAccount: (username) => {
    if (account !== null && account !== username) useBoards.getState().reset();
    account = username;
  },

  reset: () => {
    epoch += 1;
    account = null;
    asking.clear();
    set({ boards: {} });
  },
}));
