import { strings } from '../../strings';

interface Props {
  /** Why the conversation could not be read, in the app's words. */
  message: string;
  onRetry: () => void;
}

/**
 * The transcript could not be read from its device, or part of it could not be
 * mended (`docs/DESIGN.md` § "When the history cannot be read"). It stands where
 * the missing rows would be, rather than over the transcript, and never says the
 * conversation is empty: that is only said once a read has succeeded.
 */
export function HistoryNotice({ message, onRetry }: Props) {
  return (
    <div className="history-notice" role="status">
      <span>{message}</span>
      <button type="button" className="btn small" onClick={onRetry}>
        {strings.common.retry}
      </button>
    </div>
  );
}
