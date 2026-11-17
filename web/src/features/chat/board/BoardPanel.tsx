/**
 * A62 — the task board panel: the title with Refresh and a menu (Start over,
 * Turn off in Settings) while the gateway keeps the board, the board grouped
 * Doing, To do and Done recently, and under it the note its status calls for.
 * Start over asks first, in the panel itself. Everything it shows is the
 * store's copy; every action is a request to the gateway, whose answer arrives
 * through the store.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { strings } from '../../../strings';
import { useBoards, type BoardEntry } from '../../../stores/board';
import { BoardActions } from './BoardActions';
import { BoardItems } from './BoardItems';
import { BoardNote } from './BoardNote';
import { bodyKind, isKept } from './boardView';

interface Props {
  deviceId: string;
  sessionId: string;
  /** The store's copy, or nothing while the gateway has not answered. */
  entry: BoardEntry | undefined;
  /** Enable was pressed: the panel closes and the disclosure opens. */
  onEnable: () => void;
  /** A link out of the panel was followed, to Settings. */
  onLeave: () => void;
}

export function BoardPanel({ deviceId, sessionId, entry, onEnable, onLeave }: Props) {
  const titleId = useId();
  const [confirming, setConfirming] = useState(false);
  const status = entry?.status ?? null;
  const refresh = (rebuild = false) => void useBoards.getState().refresh(deviceId, sessionId, rebuild);

  return (
    <section className="board-panel" aria-labelledby={titleId}>
      <div className="board-head">
        <h2 className="board-title" id={titleId}>
          {strings.chat.board.title}
        </h2>
        {isKept(entry?.status) && !confirming ? (
          <BoardActions
            updating={status?.state === 'updating'}
            onRefresh={() => refresh()}
            onStartOver={() => setConfirming(true)}
            onLeave={onLeave}
          />
        ) : null}
      </div>
      {confirming ? (
        <StartOver
          onConfirm={() => {
            setConfirming(false);
            refresh(true);
          }}
          onCancel={() => setConfirming(false)}
        />
      ) : (
        <>
          <BoardBody entry={entry} />
          <BoardNote entry={entry} onEnable={onEnable} onRetry={() => refresh()} />
        </>
      )}
    </section>
  );
}

/** The board, "No tasks yet", or three quiet lines while the first one is made. */
function BoardBody({ entry }: { entry: BoardEntry | undefined }) {
  const kind = bodyKind(entry);
  if (kind === 'items' && entry?.board) return <BoardItems board={entry.board} />;
  if (kind === 'empty') return <p className="board-empty">{strings.chat.board.empty}</p>;
  if (kind === 'skeleton') {
    return (
      <div className="board-skeleton" aria-hidden>
        <span />
        <span />
        <span />
      </div>
    );
  }
  return null;
}

/** Start over's question, in place of the board until it is answered. Cancel has the focus. */
function StartOver({ onConfirm, onCancel }: { onConfirm: () => void; onCancel: () => void }) {
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => cancel.current?.focus(), []);
  const s = strings.chat.board;

  return (
    <div className="board-confirm" role="group" aria-label={s.startOver}>
      <p className="board-confirm-ask">{s.startOverAsk}</p>
      <div className="board-confirm-actions">
        <button type="button" className="btn small" ref={cancel} onClick={onCancel}>
          {strings.common.cancel}
        </button>
        <button type="button" className="btn small primary" onClick={onConfirm}>
          {s.startOver}
        </button>
      </div>
    </div>
  );
}
