/**
 * A62 — what the task board's chip and panel draw, read from the board and its
 * status alone, so the rules can be read and tested without rendering.
 */
import type { Board, BoardItem, BoardItemStatus, BoardStatus } from '../../../protocol/types';
import type { BoardEntry } from '../../../stores/board';

/** The groups, in the order the panel draws them. */
const GROUP_ORDER: readonly BoardItemStatus[] = ['doing', 'todo', 'done'];

export interface BoardGroup {
  status: BoardItemStatus;
  items: BoardItem[];
}

/** The items by group, each in the gateway's order. A group with nothing in it is not drawn. */
export function groupsOf(board: Board): BoardGroup[] {
  return GROUP_ORDER.map((status) => ({
    status,
    items: board.items.filter((item) => item.status === status),
  })).filter((group) => group.items.length > 0);
}

/** Whether the gateway keeps this board for the account now: on, and not refused. */
export const isKept = (status: BoardStatus | undefined): boolean =>
  status?.state === 'current' || status?.state === 'updating' || status?.state === 'stale';

/** What the chip counts: doing and to do. None while off, unavailable or empty. */
export function openCount(entry: BoardEntry | undefined): number {
  if (!entry?.board || !isKept(entry.status)) return 0;
  return entry.board.items.filter((item) => item.status === 'doing' || item.status === 'todo').length;
}

/** A gateway that keeps no boards takes the chip away, as `board.enabled: false` does. */
export const hidesChip = (status: BoardStatus | undefined): boolean =>
  status?.state === 'unavailable' && status.reason === 'unsupported';

/** What stands where the board goes. */
export type BodyKind = 'items' | 'empty' | 'skeleton' | 'none';

/**
 * The board's items, or "No tasks yet" for a board with none or a current one
 * that was never made, or three skeleton lines while the first one is being
 * built — and before the gateway has answered at all. Off draws no board.
 */
export function bodyKind(entry: BoardEntry | undefined): BodyKind {
  if (!entry) return 'skeleton';
  const { board, status } = entry;
  if (status.state === 'off') return 'none';
  if (board) return board.items.length > 0 ? 'items' : 'empty';
  if (status.state === 'current') return 'empty';
  return status.state === 'updating' ? 'skeleton' : 'none';
}

/** What the line under the board says. */
export type NoteKind =
  | 'off'
  | 'updating'
  | 'updated'
  | 'offline'
  | 'failed'
  | 'paused'
  | 'quota'
  | 'unavailable';

/**
 * The note for a status. An unavailable reason this build does not know reads
 * as the generic "isn't available" line, a stale one as "Couldn't update", with
 * Retry; a state it does not know already reads as stale (`readBoardStatus`).
 */
export function noteKind(status: BoardStatus): NoteKind {
  switch (status.state) {
    case 'off':
      return 'off';
    case 'updating':
      return 'updating';
    case 'current':
      return 'updated';
    case 'unavailable':
      return status.reason === 'quota_exceeded' ? 'quota' : 'unavailable';
    default:
      if (status.reason === 'device_offline') return 'offline';
      return status.reason === 'rate_limited' ? 'paused' : 'failed';
  }
}
