/**
 * The Close action at the end of a session row (A39, `docs/DESIGN.md`
 * § "Close, then the Archive"). It ends the session on the machine — the
 * device interrupts the turn and lets go of the agent — and the row lands in
 * the Archive when the device's reply comes back.
 *
 * The question is asked only while the session is working, by the dot's own
 * tone, because that is the only moment there is unfinished work to lose. An
 * idle session closes on the tap.
 */
import { useState } from 'react';
import { CircleX } from 'lucide-react';
import { ConfirmDialog } from '../../components/Modal';
import { dotTone } from '../../components/dotTone';
import { strings } from '../../strings';
import { useSessions } from '../../stores/sessions';
import type { Session } from '../../protocol/types';

interface Props {
  session: Session;
  online: boolean;
}

export function SessionCloseButton({ session, online }: Props) {
  const close = useSessions((s) => s.close);
  const [asking, setAsking] = useState(false);
  const [busy, setBusy] = useState(false);
  const asksFirst = dotTone(session.state, session.control, online) === 'working';

  /** Closes, and keeps the dialog open when the device refused. */
  const run = async () => {
    setBusy(true);
    try {
      await close(session);
      setAsking(false);
    } finally {
      setBusy(false);
    }
  };

  const request = () => void run().catch(() => undefined);

  return (
    <>
      <button
        type="button"
        className="icon-btn session-close"
        title={strings.sessions.close}
        aria-label={strings.sessions.close}
        disabled={busy}
        onClick={() => {
          if (asksFirst) setAsking(true);
          else request();
        }}
      >
        <CircleX size={15} />
      </button>
      <ConfirmDialog
        open={asking}
        title={strings.sessions.closeTitle}
        body={strings.sessions.closeBody}
        confirmLabel={strings.sessions.close}
        danger
        busy={busy}
        onClose={() => setAsking(false)}
        onConfirm={request}
      />
    </>
  );
}
