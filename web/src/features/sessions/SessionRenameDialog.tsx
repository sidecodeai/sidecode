/**
 * A55: the dialog that renames a session — the device rename's dialog
 * (`DevicesPage`) with one field, kept to 60 code points. Save waits for the
 * device: nothing is drawn ahead of its reply, which carries the name the agent
 * now holds and can differ from what was typed, and a typed Claude rename can
 * take a few seconds. A refusal stays under the field with the dialog open. The
 * caller mounts this only while it is open, so every opening starts from the
 * session's name.
 */
import { useEffect, useId, useRef, useState, type KeyboardEvent } from 'react';
import { Button } from '../../components/Button';
import { Modal } from '../../components/Modal';
import { strings } from '../../strings';
import { useSessions } from '../../stores/sessions';
import type { Session } from '../../protocol/types';
import { useImeGuard } from '../chat/useImeGuard';
import { NAME_LIMIT, checkName, keepToLimit, renameFailure, trimName } from './rename';

interface Props {
  session: Session;
  onClose: () => void;
}

export function SessionRenameDialog({ session, onClose }: Props) {
  const rename = useSessions((s) => s.rename);
  const [value, setValue] = useState(trimName(session.title));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The last edit was cut at the limit, which the line under the field says.
  const [capped, setCapped] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  // The text before an input method's composition began, which its result grows.
  const composedFrom = useRef('');
  const ime = useImeGuard();
  const inputId = useId();
  const lineId = useId();

  // The name is selected, so typing replaces it. This runs after the Modal has
  // put focus in the field.
  useEffect(() => {
    mounted.current = true;
    input.current?.select();
    return () => {
      mounted.current = false;
    };
  }, []);

  const check = checkName(value, session.title);
  const canSave = check === 'ok' && !busy;
  // One line under the field: the device's refusal, else what stops Save or
  // what the field has just refused. A refusal and a name past the limit — a
  // longer one the agent holds, or an input method's buffer — are errors; an
  // empty field is a prompt.
  const tooLong = check === 'too-long' || capped;
  const invalid = error !== null || tooLong;
  const line =
    error ??
    (tooLong
      ? strings.sessions.renameTooLong(NAME_LIMIT)
      : check === 'empty'
        ? strings.sessions.renameEmpty
        : null);

  /** What the field holds after an edit: kept to the limit, and no refusal outlives it. */
  const edit = (next: string, previous: string) => {
    const kept = keepToLimit(next, previous);
    setValue(kept);
    setCapped(kept !== next);
    setError(null);
  };

  /** A dialog dismissed while the device was answering has nothing left to close. */
  const save = async () => {
    // Focus stays in the field while it is locked: a click on Save would
    // otherwise leave it on a button that is about to be disabled, and a refusal
    // would find nothing focused to edit.
    input.current?.focus();
    setBusy(true);
    setError(null);
    try {
      await rename(session.session_id, trimName(value));
      if (mounted.current) onClose();
    } catch (err) {
      if (!mounted.current) return;
      setError(renameFailure(err));
      setBusy(false);
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    // Escape in the middle of a composition cancels the composition, which is
    // the input method's; the Modal's listener on `document` never sees it.
    if (e.key === 'Escape' && e.nativeEvent.isComposing) {
      e.stopPropagation();
      return;
    }
    if (e.key !== 'Enter' || ime.ownsEnter(e)) return;
    e.preventDefault();
    if (canSave) ime.sendUnlessComposing(() => void save());
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={strings.sessions.renameTitle}
      width={420}
      footer={
        <>
          <Button onClick={onClose}>{strings.common.cancel}</Button>
          <Button variant="primary" busy={busy} disabled={!canSave} onClick={() => void save()}>
            {strings.common.save}
          </Button>
        </>
      }
    >
      <div className="form-stack">
        <label className="label" htmlFor={inputId}>
          {strings.sessions.renameLabel}
        </label>
        <input
          id={inputId}
          ref={input}
          className="field"
          value={value}
          readOnly={busy}
          autoComplete="off"
          aria-invalid={invalid}
          aria-describedby={line === null ? undefined : lineId}
          onChange={(e) => {
            // What an input method has composed so far is not text yet, and
            // cutting it would end the composition: it is kept to the limit
            // when it is committed.
            if ((e.nativeEvent as InputEvent).isComposing) {
              setValue(e.target.value);
              setCapped(false);
              setError(null);
              return;
            }
            edit(e.target.value, value);
          }}
          onCompositionStart={() => {
            composedFrom.current = value;
            ime.onCompositionStart();
          }}
          onCompositionEnd={(e) => {
            ime.onCompositionEnd();
            edit(e.currentTarget.value, composedFrom.current);
          }}
          onKeyDown={onKeyDown}
        />
        {line === null ? null : (
          <p
            id={lineId}
            className={invalid ? 'form-error' : 'field-note'}
            role={error === null ? undefined : 'alert'}
          >
            {line}
          </p>
        )}
      </div>
    </Modal>
  );
}
