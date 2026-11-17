/**
 * A62 — the disclosure before the task board is turned on, from a
 * conversation's panel or from Settings, with one wording for every gateway:
 * what the board is for, what is sent where and when, what is kept and for how
 * long, and what to be careful of. Agree and continue writes the account's
 * `task_board: true`, and nothing is sent for the board before that; Not now
 * writes nothing. A refusal stays in the dialog.
 */
import { useState } from 'react';
import { Modal } from '../../../components/Modal';
import { strings } from '../../../strings';
import { usePreferences } from '../../../stores/preferences';
import './board.css';

interface Props {
  open: boolean;
  /** Closed: agreed and written, or not now. */
  onClose: () => void;
}

export function BoardConsent({ open, onClose }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const c = strings.settings.taskBoardConsent;

  const close = () => {
    setError(null);
    onClose();
  };

  const agree = async () => {
    setBusy(true);
    setError(null);
    try {
      await usePreferences.getState().setTaskBoard(true);
      onClose();
    } catch {
      setError(strings.errors.setFailed);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title={c.title}
      width={480}
      footer={
        <>
          <button type="button" className="btn" onClick={close}>
            {c.notNow}
          </button>
          <button type="button" className="btn primary" disabled={busy} onClick={() => void agree()}>
            {c.agree}
          </button>
        </>
      }
    >
      <div className="board-consent">
        <p className="hint">{c.lead}</p>
        <ul className="board-consent-points">
          <li>{c.sends}</li>
          <li>{c.keeps}</li>
          <li>{c.caution}</li>
        </ul>
        {error === null ? null : (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
