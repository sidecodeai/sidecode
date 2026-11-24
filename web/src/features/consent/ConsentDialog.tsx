/**
 * The permission an AI feature asks for before it sends anything (A65), drawn
 * in the protocol's order: the title, what the feature is for, the points on
 * what is sent and to whom, the paragraphs after them, then Not now beside
 * Agree and continue. Nothing is sent and nothing is kept until the person
 * agrees; what agreeing does is the caller's, and a refusal of it stays in the
 * dialog, which stays open. Not now is the button that has the focus.
 */
import { useState } from 'react';
import { Modal } from '../../components/Modal';
import { strings } from '../../strings';
import './consent.css';

interface Props {
  open: boolean;
  title: string;
  /** What the feature is for, over the points; none where the title says it. */
  lead?: string;
  /** What is sent and to whom: a sentence or two each. */
  points: readonly string[];
  /** After the points, in order: who keeps what, and how to withdraw. */
  paragraphs: readonly string[];
  /** The person agreed: keep it, and go on with what they reached for. A rejection keeps the dialog. */
  onAgree: () => void | Promise<void>;
  /** Closed: not now, or agreed and done. */
  onClose: () => void;
  /** Not now, Escape or a press outside: said before the dialog closes. */
  onDecline?: () => void;
  /** What a rejected `onAgree` is said as. */
  failure?: string;
}

export function ConsentDialog({
  open,
  title,
  lead,
  points,
  paragraphs,
  onAgree,
  onClose,
  onDecline,
  failure = strings.errors.setFailed,
}: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const c = strings.consent;

  const decline = () => {
    setError(null);
    onDecline?.();
    onClose();
  };

  const agree = async () => {
    setBusy(true);
    setError(null);
    try {
      await onAgree();
      onClose();
    } catch {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={decline}
      title={title}
      width={480}
      footer={
        <>
          <button type="button" className="btn" onClick={decline}>
            {c.notNow}
          </button>
          <button type="button" className="btn primary" disabled={busy} onClick={() => void agree()}>
            {c.agree}
          </button>
        </>
      }
    >
      <div className="consent-body">
        {lead ? <p className="hint">{lead}</p> : null}
        <ul className="consent-points">
          {points.map((point) => (
            <li key={point}>{point}</li>
          ))}
        </ul>
        {paragraphs.map((paragraph) => (
          <p key={paragraph} className="consent-paragraph">
            {paragraph}
          </p>
        ))}
        {error === null ? null : (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
