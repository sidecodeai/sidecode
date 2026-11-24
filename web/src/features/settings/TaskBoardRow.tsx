/**
 * A62 — the account's task board, the last row of Reading: a switch with the
 * note that says what turning it on sends, and that turning it off deletes
 * every board. The switch shows what the account stored, and moves when the
 * gateway has answered; a refusal is said in place of the note.
 *
 * Turning off is always possible while the board is on, whatever the gateway
 * says: it writes at once, and the gateway deletes every board. Turning on
 * shows the disclosure first and writes nothing until the person agrees, and
 * only on a gateway that keeps boards; one that keeps none draws the off switch
 * disabled, and says so. A gateway too old to keep boards draws no row.
 */
import { useState } from 'react';
import { Switch } from '../../components/Switch';
import { strings } from '../../strings';
import { useConnection } from '../../stores/connection';
import { usePreferences } from '../../stores/preferences';
import { BoardConsent } from '../chat/board/BoardConsent';
import { useProviderNames } from '../consent/providerNames';
import { SettingsRow } from './SettingsRow';

export function TaskBoardRow() {
  const board = useConnection((s) => s.board);
  const preferences = usePreferences((s) => s.preferences);
  const name = useProviderNames().board;
  const [consenting, setConsenting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (board === null) return null;
  const s = strings.settings;
  const on = preferences?.task_board === true;
  // Nothing to write to on a gateway that holds no preferences, and nothing to
  // turn on where the gateway keeps no boards.
  const disabled = preferences === undefined || busy || (!on && !board.enabled);

  const turnOff = () => {
    setBusy(true);
    usePreferences
      .getState()
      .setTaskBoard(false)
      .catch(() => setError(strings.errors.setFailed))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <SettingsRow
        title={s.taskBoard}
        sentence={error ?? (board.enabled ? s.taskBoardNote(name) : s.taskBoardUnsupported)}
        target
        control={
          <Switch
            label={s.taskBoard}
            checked={on}
            disabled={disabled}
            onChange={(next) => {
              setError(null);
              if (!next) turnOff();
              else setConsenting(true);
            }}
          />
        }
      />
      <BoardConsent open={consenting} onClose={() => setConsenting(false)} />
    </>
  );
}
