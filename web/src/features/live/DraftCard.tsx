import { Button } from '../../components/Button';
import { liveStrings as t } from './strings';
import type { LiveCall } from './useLiveCall';

/** The message the assistant has drafted and is waiting on a yes for. */
export function DraftCard({ call }: { call: LiveCall }) {
  const { draft } = call.view;
  const { draftState } = call;
  if (!draft) {
    return draftState === 'sent' ? <p className="live-note live-sent">{t.sent}</p> : null;
  }
  return (
    <div className="live-draft">
      <div className="live-draft-title">{t.draftTitle}</div>
      <div className="live-draft-to">{t.draftTo(draft.session)}</div>
      <p className="live-draft-text">{draft.text}</p>
      {draftState === 'failed' ? <p className="live-error">{t.errorSend}</p> : null}
      <div className="live-draft-actions">
        <Button small onClick={call.cancelDraft} disabled={draftState === 'sending'}>
          {t.cancel}
        </Button>
        <Button small variant="primary" onClick={call.sendDraft} busy={draftState === 'sending'}>
          {draftState === 'sending' ? t.sending : t.send}
        </Button>
      </div>
    </div>
  );
}
