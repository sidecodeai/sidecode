import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router';
import { Plus, Search } from 'lucide-react';
import { Mark } from '../../layout/Mark';
import { TooltipButton } from '../../components/TooltipButton';
import { cx } from '../../lib/cx';
import { strings } from '../../strings';
import { useDevices } from '../../stores/devices';
import { countWaiting, selectSessionList, useSessions } from '../../stores/sessions';
import type { Session } from '../../protocol/types';
import { AccountButton } from '../account-menu/AccountButton';
import { SessionRenameDialog } from '../sessions/SessionRenameDialog';
import { SidebarList } from './SidebarList';
import { SidebarToggle } from './SidebarToggle';
import { useSidebarFold } from './useSidebarFold';
import './sidebar.css';

interface Props {
  activeKey: string;
  onNewSession: () => void;
}

/**
 * The chat's first column (`docs/DESIGN.md` § "The chat sidebar"): the toggle,
 * the brand and New session, the search, the sessions, and at its foot the
 * count and the account. Folded by hand it is a rail of the toggle, New
 * session, Search and the account's initials. Whatever a state does not draw is
 * not in the page at all, so nothing hidden can take focus or be read out, and
 * the controls both states draw stay the same elements, focus included.
 */
export function Sidebar({ activeKey, onNewSession }: Props) {
  const sessionsRecord = useSessions((s) => s.sessions);
  const devices = useDevices((s) => s.devices);
  const [query, setQuery] = useState('');
  // A55: the session whose Rename dialog is open, kept here and not in a row so
  // that the dialog outlives a row that moves between lists.
  const [renaming, setRenaming] = useState<Session | null>(null);
  const { collapsed, moving, setCollapsed, aside, toggle } = useSidebarFold();
  const id = useId();
  const searchField = useRef<HTMLInputElement>(null);
  // The rail's Search opens the sidebar and puts the caret in the field once it
  // is drawn: after the fold has moved the focus it took away, and before paint.
  const searchAsked = useRef(false);

  useLayoutEffect(() => {
    if (collapsed || !searchAsked.current) return;
    searchAsked.current = false;
    searchField.current?.focus({ preventScroll: true });
  }, [collapsed]);

  const waiting = useMemo(
    () => countWaiting(selectSessionList(sessionsRecord)),
    [sessionsRecord],
  );

  const side = collapsed ? 'right' : 'bottom';

  return (
    <aside
      ref={aside}
      id={id}
      className={cx('sidebar', collapsed && 'collapsed', moving && 'moving')}
    >
      <div className="sidebar-inner">
        <div className="sidebar-head">
          <SidebarToggle
            ref={toggle}
            collapsed={collapsed}
            waiting={waiting}
            controls={id}
            onToggle={() => setCollapsed(!collapsed)}
          />
          {collapsed ? null : (
            <Link to="/sessions" className="brand sidebar-brand">
              <Mark size={18} />
              <span>{strings.productName}</span>
            </Link>
          )}
          <TooltipButton
            className="sidebar-action"
            label={strings.chat.newSession}
            side={side}
            onClick={onNewSession}
          >
            <Plus size={20} aria-hidden />
          </TooltipButton>
          {collapsed ? (
            <TooltipButton
              className="sidebar-action"
              label={strings.common.search}
              side={side}
              onClick={() => {
                searchAsked.current = true;
                setCollapsed(false);
              }}
            >
              <Search size={20} aria-hidden />
            </TooltipButton>
          ) : null}
        </div>

        {collapsed ? null : (
          <>
            <label className="search-field sidebar-search">
              <Search size={14} aria-hidden />
              <input
                ref={searchField}
                type="search"
                value={query}
                placeholder={strings.chat.searchPlaceholder}
                aria-label={strings.chat.searchPlaceholder}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            <SidebarList activeKey={activeKey} query={query} onRename={setRenaming} />
          </>
        )}

        <footer className="sidebar-foot">
          {collapsed ? null : (
            <p className="sidebar-count">{strings.chat.sidebarFooter(devices.length, waiting)}</p>
          )}
          <AccountButton place={collapsed ? 'rail' : 'sidebar'} />
        </footer>
      </div>

      {/* On the body, because this pane is hidden below 1024 px and a dialog
          inside it would go with it. */}
      {renaming
        ? createPortal(
            <SessionRenameDialog session={renaming} onClose={() => setRenaming(null)} />,
            document.body,
          )
        : null}
    </aside>
  );
}
