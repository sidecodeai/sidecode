/**
 * A65 — the permission the voice assistant asks for before the first call, in the second version
 * of its words, which asks everyone again: what a call shares, the AI speech service it goes to,
 * and how a message gets sent. Agreeing keeps it in this browser for the account, for the service
 * named, and starts the call. It is drawn inside the panel, which sits above the page, so that its
 * shade covers the panel too.
 */
import { grantConsent } from '../../stores/aiConsent';
import { useAuth } from '../../stores/auth';
import { ConsentDialog } from '../consent/ConsentDialog';
import { liveStrings as t } from './strings';

interface Props {
  /** The service the gateway named when Start was pressed, or null for none; the dialog is closed while undefined. */
  provider: string | null | undefined;
  onClose: () => void;
  /** Agreed and kept: the call Start was pressed for starts. */
  onAgreed?: () => void;
}

export function LiveConsent({ provider, onClose, onAgreed }: Props) {
  const username = useAuth((s) => s.username);
  const name = provider ?? null;

  return (
    <ConsentDialog
      open={provider !== undefined}
      title={t.consentHeading}
      points={[t.consentVoice, t.consentSessions, t.consentDrafts]}
      paragraphs={[t.consentGoesTo(name), t.consentSend, t.consentWithdraw]}
      onAgree={() => {
        if (username) grantConsent(username, 'assistant', name);
        onAgreed?.();
      }}
      onClose={onClose}
    />
  );
}
