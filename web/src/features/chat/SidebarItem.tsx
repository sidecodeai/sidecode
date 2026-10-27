import { useId } from 'react';
import { StatusDot } from '../../components/StatusDot';
import { UnseenDot } from '../../components/UnseenDot';
import { cx } from '../../lib/cx';
import { baseName, relativeTime } from '../../lib/format';
import { sessionTitle } from '../../strings';
import { keyOf } from '../../stores/sessions';
import { useSessionMenu } from '../sessions/useSessionMenu';
import { DictationRowLabel } from '../voice/DictationRowLabel';
import type { Session } from '../../protocol/types';

interface Props {
  session: Session;
  online: boolean;
  active: boolean;
  /** A47: whether the row draws the red dot. */
  unseen: boolean;
  onOpen: () => void;
  /** A55: present only where the device can rename this session; see `SessionRow`. */
  onRename?: (session: Session) => void;
}

/** One session in the chat sidebar. */
export function SidebarItem({ session, online, active, unseen, onOpen, onRename }: Props) {
  const unseenId = useId();
  const { triggerProps, menu } = useSessionMenu(session, onRename);
  return (
    <li {...triggerProps}>
      <button
        type="button"
        className={cx('sidebar-item', active && 'active')}
        onClick={onOpen}
        aria-describedby={unseen ? unseenId : undefined}
      >
        <StatusDot state={session.state} control={session.control} online={online} />
        <span className="sidebar-item-text">
          <span className="sidebar-title-line">
            {unseen ? <UnseenDot id={unseenId} /> : null}
            <span className="sidebar-title">{sessionTitle(session)}</span>
          </span>
          {/* The folder and the time, whatever the session is doing: the dot
              beside them carries the state (docs/DESIGN.md § "The session row
              says where it came from"). */}
          <span className="sidebar-sub">
            {`${baseName(session.cwd)} · ${relativeTime(session.updated_at)}`}
          </span>
          <DictationRowLabel sessionKey={keyOf(session)} />
        </span>
      </button>
      {menu}
    </li>
  );
}
