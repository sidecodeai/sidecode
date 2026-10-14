import { useState } from 'react';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { Segmented } from '../../components/Segmented';
import { api } from '../../lib/api';
import { userErrorText } from '../../lib/accountErrors';
import type { UserRole } from '../../protocol/types';
import { roleLabel, strings } from '../../strings';

interface Props {
  onClose: () => void;
  onDone: () => Promise<void>;
}

/** The shortest password the gateway takes (PROTOCOL.md §3.1). */
const MIN_PASSWORD = 8;

/** Member first, and the default: an admin is the exception on this screen. */
const ROLES: UserRole[] = ['member', 'admin'];

/**
 * A24: the admin makes an account without waiting for anyone to register. The
 * caller mounts this only while it is open, so every opening starts empty.
 */
export function AddUserModal({ onClose, onDone }: Props) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<UserRole>('member');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ready = username.trim().length > 0 && password.length >= MIN_PASSWORD;

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await api.createUser(username.trim(), password, role);
      await onDone();
      onClose();
    } catch (err) {
      setError(userErrorText(err, strings.account.taken));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={strings.users.add}
      width={420}
      footer={
        <>
          <Button onClick={onClose}>{strings.common.cancel}</Button>
          <Button variant="primary" busy={busy} disabled={!ready} onClick={() => void submit()}>
            {strings.users.add}
          </Button>
        </>
      }
    >
      <div className="form-stack">
        <label className="label" htmlFor="rc-new-username">
          {strings.account.username}
        </label>
        <input
          id="rc-new-username"
          className="field"
          type="text"
          autoCapitalize="none"
          autoCorrect="off"
          spellCheck={false}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <label className="label" htmlFor="rc-new-user-password">
          {strings.account.password}
        </label>
        <input
          id="rc-new-user-password"
          className="field"
          type="password"
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <span className="label">{strings.account.role}</span>
        <Segmented
          ariaLabel={strings.account.role}
          value={role}
          onChange={setRole}
          options={ROLES.map((id) => ({ value: id, label: roleLabel(id) }))}
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
