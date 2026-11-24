/**
 * A65 — the permission AI polish asks for: what was dictated, with the recent
 * messages of the conversation as context, goes through the gateway to the
 * model provider it names. Asked when polish is turned on in Settings, and at
 * the first dictation to be polished in a browser that never agreed while the
 * account's polish is on. Agreeing keeps the permission here for the account;
 * what follows — writing the switch, polishing that dictation — is the caller's.
 */
import { strings } from '../../strings';
import { grantConsent } from '../../stores/aiConsent';
import { useAuth } from '../../stores/auth';
import { ConsentDialog } from './ConsentDialog';
import { useProviderNames } from './providerNames';

interface Props {
  open: boolean;
  onClose: () => void;
  onAgreed?: () => void;
  /** Not now. */
  onDeclined?: () => void;
}

export function PolishConsent({ open, onClose, onAgreed, onDeclined }: Props) {
  const username = useAuth((s) => s.username);
  const name = useProviderNames().polish;
  const p = strings.consent.polish;

  return (
    <ConsentDialog
      open={open}
      title={p.title}
      lead={p.lead}
      points={[p.sends, p.goesTo(name), p.draft]}
      paragraphs={[p.withdraw]}
      onAgree={() => {
        if (username) grantConsent(username, 'polish', name);
        onAgreed?.();
      }}
      onClose={onClose}
      onDecline={onDeclined}
    />
  );
}
