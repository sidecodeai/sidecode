import type { LiveStatus } from './api';
import type { CallView } from './callState';
import { blockShown, noteText } from './words';

interface Props {
  view: CallView;
  status: LiveStatus;
  /** The person followed a link out of the panel, which then has no reason to stay. */
  onFollow: () => void;
}

/**
 * The line under the title with no call on: why the assistant cannot be had, how the last call
 * ended, or what the assistant is for.
 */
export function Note(props: Props) {
  const block = blockShown(props.view, props.status);
  return <p className="live-note">{noteText(props.view, block)}</p>;
}
