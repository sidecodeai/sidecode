import { useState } from 'react';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { ApiError, api } from '../../lib/api';
import { strings } from '../../strings';

interface Props {
  onClose: () => void;
}

/** The shortest password the gateway takes (PROTOCOL.md §3.1). */
const MIN_PASSWORD = 8;

/**
 * A24: a member changes its own password here. `admin`'s is `RC_PASSWORD` and
 * the gateway refuses it, which is why the row that opens this is a member's.
 * The caller mounts this only while it is open, so every opening starts empty.
 */
export function ChangePasswordModal({ onClose }: Props) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ready = current.length > 0 && next.length >= MIN_PASSWORD;

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await api.changePassword(current, next);
      onClose();
    } catch (err) {
      setError(passwordErrorText(err));
      setBusy(false);
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={strings.settings.changePassword}
      width={420}
      footer={
        <>
          <Button onClick={onClose}>{strings.common.cancel}</Button>
          <Button variant="primary" busy={busy} disabled={!ready} onClick={() => void submit()}>
            {strings.common.save}
          </Button>
        </>
      }
    >
      <div className="form-stack">
        <label className="label" htmlFor="rc-current-password">
          {strings.account.currentPassword}
        </label>
        <input
          id="rc-current-password"
          className="field"
          type="password"
          autoComplete="current-password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
        <label className="label" htmlFor="rc-new-password">
          {strings.account.newPassword}
        </label>
        <input
          id="rc-new-password"
          className="field"
          type="password"
          autoComplete="new-password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
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

function passwordErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return strings.account.wrongCurrentPassword;
    if (err.status === 403) return strings.account.notAllowed;
    if (err.status === 400) return strings.account.rules;
    return err.message || strings.errors.generic;
  }
  return strings.errors.generic;
}
