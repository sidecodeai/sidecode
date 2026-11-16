/**
 * The Close action at the end of a session row (A39, A61, `docs/DESIGN.md`
 * § "Close, then the Archive"). It ends the session on the machine — the
 * device interrupts the turn and lets go of the agent, or quits it in the
 * terminal that holds it — and the row lands in the Archive when the device's
 * reply comes back.
 *
 * It asks first on a row a terminal holds, always, and on a session the device
 * drives while it is working, by the dot's own tone; an idle session the device
 * drives closes on the tap, because there is nothing to lose. A refusal stays
 * under the question with the dialog open.
 */
import { useState } from 'react';
import { CircleX } from 'lucide-react';
import { ConfirmDialog } from '../../components/Modal';
import { dotTone } from '../../components/dotTone';
import { strings } from '../../strings';
import { useSessions } from '../../stores/sessions';
import type { Device, Session } from '../../protocol/types';
import { closeFailure, closeQuestion } from './close';

interface Props {
  session: Session;
  device: Device;
}

export function SessionCloseButton({ session, device }: Props) {
  const close = useSessions((s) => s.close);
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const working = dotTone(session.state, session.control, device.online) === 'working';
  const question = closeQuestion(session, device.name, working);

  /** Closes, and keeps the dialog open with the refusal when the device refused. */
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await close(session);
      setAsking(false);
    } catch (err) {
      setError(closeFailure(err));
    } finally {
      setBusy(false);
    }
  };

  const dismiss = () => {
    setAsking(false);
    setError(null);
  };

  return (
    <>
      <button
        type="button"
        className="icon-btn session-close"
        title={strings.sessions.close}
        aria-label={strings.sessions.close}
        disabled={busy}
        onClick={() => {
          if (question.length === 0) {
            void run();
            return;
          }
          setError(null);
          setAsking(true);
        }}
      >
        <CircleX size={15} />
      </button>
      <ConfirmDialog
        open={asking}
        title={strings.sessions.closeTitle}
        body={question}
        confirmLabel={strings.sessions.close}
        danger
        busy={busy}
        error={error}
        onClose={dismiss}
        onConfirm={() => void run()}
      />
    </>
  );
}
