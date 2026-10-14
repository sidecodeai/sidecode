import { useId } from 'react';
import { StatusDot } from '../../components/StatusDot';
import { UnseenDot } from '../../components/UnseenDot';
import { cx } from '../../lib/cx';
import { baseName, relativeTime } from '../../lib/format';
import { sessionTitle } from '../../strings';
import type { Session } from '../../protocol/types';

interface Props {
  session: Session;
  online: boolean;
  active: boolean;
  /** A47: whether the row draws the red dot. */
  unseen: boolean;
  onOpen: () => void;
}

/** One session in the chat sidebar. */
export function SidebarItem({ session, online, active, unseen, onOpen }: Props) {
  const unseenId = useId();
  return (
    <li>
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
        </span>
      </button>
    </li>
  );
}
