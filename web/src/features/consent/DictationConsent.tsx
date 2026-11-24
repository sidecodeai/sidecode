/**
 * A65 — the permission dictation asks for before the first recording, at the
 * microphone or at the Dictation switch in Settings: the audio of each
 * dictation goes through the gateway to the speech service it names. Agreeing
 * keeps the permission in this browser for the account, then goes on with what
 * the person reached for.
 */
import { strings } from '../../strings';
import { grantConsent } from '../../stores/aiConsent';
import { useAuth } from '../../stores/auth';
import { ConsentDialog } from './ConsentDialog';
import { useProviderNames } from './providerNames';

interface Props {
  open: boolean;
  onClose: () => void;
  /** Agreed and kept: the dictation the microphone was pressed for starts. */
  onAgreed?: () => void;
}

export function DictationConsent({ open, onClose, onAgreed }: Props) {
  const username = useAuth((s) => s.username);
  const name = useProviderNames().dictation;
  const d = strings.consent.dictation;

  return (
    <ConsentDialog
      open={open}
      title={d.title}
      lead={d.lead}
      points={[d.sends, d.goesTo(name), d.draft]}
      paragraphs={[d.withdraw]}
      onAgree={() => {
        if (username) grantConsent(username, 'dictation', name);
        onAgreed?.();
      }}
      onClose={onClose}
    />
  );
}
