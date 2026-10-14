import type { KeyboardEvent } from 'react';
import { Button } from '../../components/Button';
import { strings } from '../../strings';

interface Props {
  name: string;
  busy: boolean;
  error: string | null;
  onChange: (name: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
}

/**
 * A37: the one row the directory picker reveals to name a folder. It owns no
 * request — the picker sends `device.mkdir` and hands the outcome back — so
 * the name survives a clash and can be edited where it was typed.
 */
export function NewFolderRow({ name, busy, error, onChange, onSubmit, onCancel }: Props) {
  // Escape belongs to the row, not to the modal around it: the modal's own
  // handler sits on `document`, which the native event never reaches once the
  // row has taken the key.
  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      onSubmit();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      onCancel();
    }
  };

  return (
    <div className="picker-new">
      <div className="picker-new-row">
        <input
          className="field mono"
          autoFocus
          spellCheck={false}
          value={name}
          disabled={busy}
          placeholder={strings.newSession.newFolderName}
          aria-label={strings.newSession.newFolderName}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <Button
          variant="primary"
          small
          disabled={busy || name.trim() === ''}
          onClick={onSubmit}
        >
          {strings.newSession.newFolderCreate}
        </Button>
        <Button small disabled={busy} onClick={onCancel}>
          {strings.common.cancel}
        </Button>
      </div>
      {error ? <p className="picker-new-error">{error}</p> : null}
    </div>
  );
}
