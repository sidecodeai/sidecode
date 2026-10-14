import { useState } from 'react';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { api } from '../../lib/api';
import { userErrorText } from '../../lib/accountErrors';
import { strings } from '../../strings';

interface Props {
  username: string;
  onClose: () => void;
  onDone: () => Promise<void>;
}

/** The shortest password the gateway takes (PROTOCOL.md §3.1). */
const MIN_PASSWORD = 8;

/**
 * A24: the admin sets a member's password outright. It never asks for the old
 * one — the admin does not have it — and the account's open sign-ins stay valid.
 */
export function ResetPasswordModal({ username, onClose, onDone }: Props) {
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await api.patchUser(username, { password });
      await onDone();
      onClose();
    } catch (err) {
      setError(userErrorText(err, strings.account.notAllowed));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={strings.users.resetPasswordTitle(username)}
      width={420}
      footer={
        <>
          <Button onClick={onClose}>{strings.common.cancel}</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={password.length < MIN_PASSWORD}
            onClick={() => void submit()}
          >
            {strings.common.save}
          </Button>
        </>
      }
    >
      <div className="form-stack">
        <label className="label" htmlFor="rc-reset-password">
          {strings.account.newPassword}
        </label>
        <input
          id="rc-reset-password"
          className="field"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
