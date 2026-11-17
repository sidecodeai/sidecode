/**
 * A62 — the task board's way in from the conversation header: a chip like the
 * Todos chip, "Tasks" with the count of what is open, which opens the panel. It
 * is drawn in both timeline details whenever the gateway keeps boards, and not
 * at all otherwise. The disclosure the panel's Enable leads to is a dialog of
 * its own, opened here once the panel has closed, since a dialog inside the
 * panel would close it.
 */
import { useState } from 'react';
import { ListChecks } from 'lucide-react';
import { Popover } from '../../../components/Popover';
import { strings } from '../../../strings';
import { useBoards } from '../../../stores/board';
import { useConnection } from '../../../stores/connection';
import { sessionKey } from '../../../stores/sessions';
import { BoardConsent } from './BoardConsent';
import { BoardPanel } from './BoardPanel';
import { hidesChip, openCount } from './boardView';
import './board.css';

interface Props {
  deviceId: string;
  sessionId: string;
}

export function BoardChip({ deviceId, sessionId }: Props) {
  const offered = useConnection((s) => s.board?.enabled === true);
  const entry = useBoards((s) => s.boards[sessionKey(deviceId, sessionId)]);
  const [consenting, setConsenting] = useState(false);

  if (!offered || hidesChip(entry?.status)) return null;
  const open = openCount(entry);
  const s = strings.chat.board;

  return (
    <>
      <Popover
        align="end"
        className="board-chip"
        label={
          <>
            <ListChecks size={13} aria-hidden />
            {open > 0 ? s.chipCount(open) : s.chip}
          </>
        }
      >
        {(close) => (
          <BoardPanel
            deviceId={deviceId}
            sessionId={sessionId}
            entry={entry}
            onEnable={() => {
              close();
              setConsenting(true);
            }}
            onLeave={close}
          />
        )}
      </Popover>
      <BoardConsent open={consenting} onClose={() => setConsenting(false)} />
    </>
  );
}
