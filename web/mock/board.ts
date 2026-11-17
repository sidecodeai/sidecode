/**
 * A62 — the task board as the mock gateway keeps it, with no model behind it.
 * "Fix flaky auth test" has the demo board: the first time it is asked for
 * with the board on it is being made, and it arrives a moment later; after that
 * it is current. Every other conversation's board comes out empty. Refresh and
 * Start over answer `updating` and bring the board back with a higher
 * revision, and Start over gives every item a new id, since a board never
 * reuses one. As on the gateway, every write of any board takes the next value
 * of one counter, so a conversation's revision only grows, even after its board
 * was deleted and made again; it starts from the clock, so a tab left open
 * across a restart of the mock never sees it go back. The account's
 * `task_board` decides as on the gateway: off answers `off` and keeps nothing,
 * and turning it off deletes every board of the account. Pure, so the rules can
 * be read and tested without a socket; `server.ts` sends the frames.
 */
import type { Board, BoardItem, BoardStatus } from '../src/protocol/types';

/** The conversation with the demo board. */
export const DEMO_BOARD_SESSION = 'ses-flaky';

/** How long a run takes before its `board.updated` goes out. */
export const RUN_MS = 1_500;

export const OFF: BoardStatus = { state: 'off', reason: null };
export const CURRENT: BoardStatus = { state: 'current', reason: null };
export const UPDATING: BoardStatus = { state: 'updating', reason: null };

/** The demo's requests in the order the person made them, as `objects/board.json` has them. */
const DEMO: Pick<BoardItem, 'text' | 'status'>[] = [
  { text: 'Reproduce the flaky auth refresh test', status: 'done' },
  { text: 'Fix the refresh window in tests/test_auth.py', status: 'done' },
  { text: 'Run the refresh test 20 times in a row', status: 'doing' },
  { text: 'Check the settings drawer for the same timing bug', status: 'todo' },
];

interface Kept {
  revision: number;
  /** The number of the next `t<n>`. */
  next: number;
  items: BoardItem[];
  updated_at: number;
}

const kept = new Map<string, Kept>();

/** The last revision written, of any board. */
let revision = Date.now();

/** Why the account has no board kept, or null when it is on. */
export function refusal(on: boolean): BoardStatus | null {
  return on ? null : OFF;
}

const RANK = { doing: 0, todo: 1, done: 2 };

/** Doing, then to do, each as made; then done, the latest first. */
const byWireOrder = (a: BoardItem, b: BoardItem): number =>
  RANK[a.status] - RANK[b.status] || (a.status === 'done' ? b.updated_at - a.updated_at : 0);

/** The board as the wire carries it. */
function wire(sessionId: string, deviceId: string, board: Kept): Board {
  return {
    session_id: sessionId,
    device_id: deviceId,
    revision: board.revision,
    items: [...board.items].sort(byWireOrder),
    updated_at: board.updated_at,
    partial: false,
  };
}

/** The board kept for a session, or null while none was made. */
export function boardOf(sessionId: string, deviceId: string): Board | null {
  const board = kept.get(sessionId);
  return board ? wire(sessionId, deviceId, board) : null;
}

/**
 * What `board.get` answers. `build` says the demo board was asked for the first
 * time: it is being made, and `run` makes it.
 */
export function answerGet(
  sessionId: string,
  deviceId: string,
  refused: BoardStatus | null,
): { board: Board | null; status: BoardStatus; build: boolean } {
  if (refused?.state === 'off') return { board: null, status: refused, build: false };
  const board = boardOf(sessionId, deviceId);
  if (refused) return { board, status: refused, build: false };
  if (board === null && sessionId === DEMO_BOARD_SESSION) return { board, status: UPDATING, build: true };
  return { board, status: CURRENT, build: false };
}

/** The demo's items, or none, numbered on from `next`, a minute apart and done in order. */
function made(sessionId: string, next: number, now: number): BoardItem[] {
  const requests = sessionId === DEMO_BOARD_SESSION ? DEMO : [];
  return requests.map((request, index) => {
    const at = now - (requests.length - index) * 60_000;
    return { id: `t${next + index}`, text: request.text, status: request.status, created_at: at, updated_at: at };
  });
}

/** A run: the board written again, made afresh when it is new or started over. */
export function run(sessionId: string, deviceId: string, rebuild: boolean, now = Date.now()): Board {
  const held = kept.get(sessionId);
  const next = held?.next ?? 1;
  const items = held && !rebuild ? held.items : made(sessionId, next, now);
  revision += 1;
  const written: Kept = {
    revision,
    next: held && !rebuild ? next : next + items.length,
    items,
    updated_at: now,
  };
  kept.set(sessionId, written);
  return wire(sessionId, deviceId, written);
}

/** The session's board goes: the account turned the board off, or the session was deleted. */
export function forgetBoard(sessionId: string): boolean {
  return kept.delete(sessionId);
}

/** Every board, as a mock that starts again has none. */
export function resetBoards(): void {
  kept.clear();
}
