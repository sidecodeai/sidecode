/**
 * A62, A65 — the disclosure before the task board is turned on, from a
 * conversation's panel or from Settings: what the board is for, what is sent,
 * to which model provider and when, what is kept and for how long, what to be
 * careful of, and that the permission is the account's. Agree and continue
 * writes the account's `task_board: true`, and nothing is sent for the board
 * before that; Not now writes nothing. A refusal stays in the dialog.
 */
import { strings } from '../../../strings';
import { usePreferences } from '../../../stores/preferences';
import { ConsentDialog } from '../../consent/ConsentDialog';
import { useProviderNames } from '../../consent/providerNames';

interface Props {
  open: boolean;
  /** Closed: agreed and written, or not now. */
  onClose: () => void;
}

export function BoardConsent({ open, onClose }: Props) {
  const name = useProviderNames().board;
  const c = strings.settings.taskBoardConsent;
  return (
    <ConsentDialog
      open={open}
      title={c.title}
      lead={c.lead}
      points={[c.sends(name), c.keeps, c.caution, c.account]}
      paragraphs={[]}
      onAgree={() => usePreferences.getState().setTaskBoard(true)}
      onClose={onClose}
    />
  );
}
