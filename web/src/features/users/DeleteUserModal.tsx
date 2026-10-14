import { useState } from 'react';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { api } from '../../lib/api';
import { userErrorText } from '../../lib/accountErrors';
import type { UserRecord } from '../../protocol/types';
import { strings } from '../../strings';

interface Props {
  user: UserRecord;
  onClose: () => void;
  onDone: () => Promise<void>;
}

/**
 * A24: deleting an account revokes its devices and their sessions leave the
 * gateway, so the confirmation names how many go with it.
 */
export function DeleteUserModal({ user, onClose, onDone }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await api.deleteUser(user.username);
      await onDone();
      onClose();
    } catch (err) {
      setError(userErrorText(err, strings.account.notAllowed));
      setBusy(false);
    }
  }

  const body =
    user.devices === 0
      ? strings.users.deleteBody(user.username)
      : strings.users.deleteBodyDevices(user.username, user.devices);

  return (
    <Modal
      open
      onClose={onClose}
      title={strings.users.deleteTitle}
      width={440}
      footer={
        <>
          <Button onClick={onClose}>{strings.common.cancel}</Button>
          <Button variant="danger" busy={busy} onClick={() => void submit()}>
            {strings.users.deleteConfirm}
          </Button>
        </>
      }
    >
      <div className="form-stack">
        <p className="hint">{body}</p>
        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}
