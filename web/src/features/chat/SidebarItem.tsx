import { useId, useRef } from 'react';
import { Ellipsis } from 'lucide-react';
import { StatusDot } from '../../components/StatusDot';
import { UnseenDot } from '../../components/UnseenDot';
import { cx } from '../../lib/cx';
import { baseName, relativeTime } from '../../lib/format';
import { sessionTitle, strings } from '../../strings';
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

/**
 * One session in the chat sidebar. A row with a menu also has a More button at
 * its trailing edge, beside the row's own button rather than inside it, which
 * opens the menu a right-click opens.
 */
export function SidebarItem({ session, online, active, unseen, onOpen, onRename }: Props) {
  const unseenId = useId();
  const { triggerProps, menu, open, openFrom } = useSessionMenu(session, onRename);
  return (
    <li
      {...triggerProps}
      // `menu-open` while the menu is up, from More or a right-click: focus and
      // the pointer are in the menu's portal then, not on the row.
      className={cx(
        'sidebar-row',
        active && 'active',
        onRename && 'has-more',
        open && 'menu-open',
      )}
    >
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
      {onRename ? <MoreButton open={open} onOpen={openFrom} /> : null}
      {menu}
    </li>
  );
}

/**
 * The row's More. A press on it while its menu is open closes the menu, as a
 * press anywhere outside the menu does, and the click that follows must not
 * open it again. Its name is the app's for every row menu, never the title,
 * which the row's own button already says.
 */
function MoreButton({ open, onOpen }: { open: boolean; onOpen: (button: HTMLElement) => void }) {
  const pressedOpen = useRef(false);
  return (
    <button
      type="button"
      className="icon-btn sidebar-more"
      aria-label={strings.a11y.openMenu}
      aria-haspopup="menu"
      aria-expanded={open}
      onMouseDown={() => {
        pressedOpen.current = open;
      }}
      onClick={(e) => {
        const closing = pressedOpen.current;
        pressedOpen.current = false;
        if (closing) return;
        // Focus is where the menu hands it back, and Safari gives none to a clicked button.
        e.currentTarget.focus({ preventScroll: true });
        onOpen(e.currentTarget);
      }}
    >
      <Ellipsis size={16} aria-hidden />
    </button>
  );
}
