/**
 * A35 — the notice a session with a pending resume carries above its
 * transcript: what happened, when it resumes, and the two actions the ruling
 * allows and no more (`docs/DESIGN.md` § "Paused by the usage limit").
 *
 * The status dot is not touched: the session is idle and the dot says so; this
 * bar is what carries the pause.
 */
import { useId, useState } from 'react';
import { Popover } from '../../components/Popover';
import { strings } from '../../strings';
import type { SessionResume } from '../../protocol/types';
import {
  RESUME_MAX_AHEAD_MS,
  RESUME_MIN_AHEAD_MS,
  fromLocalInputValue,
  resumeBoundError,
  resumeNoticeText,
  toLocalInputValue,
} from './resume';

interface Props {
  resume: SessionResume;
  /** Resolves when the device took the new time; rejects when it refused. */
  onSet: (at: number) => Promise<void>;
  onCancel: () => void;
}

export function ResumeNotice({ resume, onSet, onCancel }: Props) {
  return (
    <div className="resume-notice" role="status">
      <span>{resumeNoticeText(resume)}</span>
      <Popover
        label={strings.chat.resumeChange}
        ariaLabel={strings.chat.resumeChange}
        align="end"
        chevron={false}
      >
        {(close) => <ChangeForm resume={resume} onSet={onSet} onDone={close} />}
      </Popover>
      <button type="button" className="btn small ghost" onClick={onCancel}>
        {strings.chat.resumeCancel}
      </button>
    </div>
  );
}

/**
 * The smallest time picker the platform has: one `datetime-local` field
 * prefilled with the time the device holds. The bounds are on the field and
 * checked again on Set, because a typed value reaches neither `min` nor `max`.
 */
function ChangeForm({
  resume,
  onSet,
  onDone,
}: {
  resume: SessionResume;
  onSet: (at: number) => Promise<void>;
  onDone: () => void;
}) {
  const fieldId = useId();
  const [value, setValue] = useState(() => toLocalInputValue(resume.at));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The bounds are read once, when the popover opens: the field is a moment's
  // work, and a clock ticking under an open picker would move them as it is used.
  const [now] = useState(() => Date.now());

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const at = fromLocalInputValue(value);
    if (at === null) {
      setError(strings.chat.resumeTooSoon);
      return;
    }
    const bound = resumeBoundError(at);
    if (bound !== null) {
      setError(bound);
      return;
    }
    setError(null);
    setBusy(true);
    onSet(at)
      .then(onDone)
      .catch(() => setError(strings.errors.resumeSetFailed))
      .finally(() => setBusy(false));
  };

  // `noValidate`: `min` and `max` are on the field so a real picker greys out
  // the days outside them, but the refusal the reader sees is this app's own
  // sentence rather than the browser's bubble, which no two browsers word the
  // same way.
  return (
    <form className="resume-form" noValidate onSubmit={submit}>
      <label className="resume-form-label" htmlFor={fieldId}>
        {strings.chat.resumeAt}
      </label>
      <input
        id={fieldId}
        type="datetime-local"
        className="resume-form-field"
        value={value}
        min={toLocalInputValue(now + RESUME_MIN_AHEAD_MS)}
        max={toLocalInputValue(now + RESUME_MAX_AHEAD_MS)}
        onChange={(event) => {
          setValue(event.target.value);
          setError(null);
        }}
      />
      {error ? <p className="resume-form-error">{error}</p> : null}
      <button type="submit" className="btn small primary" disabled={busy}>
        {strings.chat.resumeSet}
      </button>
    </form>
  );
}
