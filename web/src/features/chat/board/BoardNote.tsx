/**
 * A62 — what the panel says under the board: off, what the board is for and
 * Enable; otherwise one line for the status — when it was last updated, that it
 * is updating, or why it is not — with Retry after a failed update, and, for a
 * board built without the conversation's older rounds, a line that says so.
 */
import { relativeAgo } from '../../../lib/format';
import { useNow } from '../../../lib/useNow';
import type { Board } from '../../../protocol/types';
import { strings } from '../../../strings';
import type { BoardEntry } from '../../../stores/board';
import { useConnection } from '../../../stores/connection';
import { noteKind, type NoteKind } from './boardView';

interface Props {
  entry: BoardEntry | undefined;
  onEnable: () => void;
  onRetry: () => void;
}

export function BoardNote({ entry, onEnable, onRetry }: Props) {
  if (!entry) return null;
  const kind = noteKind(entry.status);
  if (kind === 'off') return <OffNote onEnable={onEnable} />;
  const { board } = entry;
  // A current board that was never made has no time to tell.
  const line = kind === 'updated' && board === null ? null : kind;
  if (line === null && !board?.partial) return null;

  return (
    <div className="board-foot" role="status">
      {line === null ? null : <Line kind={line} board={board} onRetry={onRetry} />}
      {board?.partial ? <p className="board-foot-line">{strings.chat.board.partial}</p> : null}
    </div>
  );
}

function OffNote({ onEnable }: { onEnable: () => void }) {
  const s = strings.chat.board;
  return (
    <div className="board-off">
      <p className="board-off-lead">{s.offLead}</p>
      <button type="button" className="btn small primary" onClick={onEnable}>
        {s.enable}
      </button>
    </div>
  );
}

/** The sentence each kind of note is, apart from the two drawn with more than words. */
const SENTENCE: Record<Exclude<NoteKind, 'off' | 'updating' | 'updated'>, () => string> = {
  offline: () => strings.chat.board.offline,
  failed: () => strings.chat.board.failed,
  paused: () => strings.chat.board.paused,
  quota: () => strings.chat.board.quota,
  unavailable: () => strings.chat.board.unavailable,
};

function Line({ kind, board, onRetry }: { kind: Exclude<NoteKind, 'off'>; board: Board | null; onRetry: () => void }) {
  if (kind === 'updating') {
    return (
      <p className="board-foot-line">
        <span className="spinner board-spinner" aria-hidden />
        {strings.chat.board.updating}
      </p>
    );
  }
  if (kind === 'updated') return board ? <Updated at={board.updated_at} /> : null;
  return (
    <p className="board-foot-line">
      <span>{SENTENCE[kind]()}</span>
      {kind === 'failed' ? (
        <button type="button" className="btn small ghost board-retry" onClick={onRetry}>
          {strings.common.retry}
        </button>
      ) : null}
    </p>
  );
}

/** "Updated 4m ago", kept current while the panel is open, on the gateway's clock. */
function Updated({ at }: { at: number }) {
  const now = useNow(30_000);
  const skew = useConnection((s) => s.clockSkewMs);
  return <p className="board-foot-line">{strings.chat.board.updated(relativeAgo(at, now + skew))}</p>;
}
