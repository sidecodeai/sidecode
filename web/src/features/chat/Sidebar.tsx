import { useMemo, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link, useNavigate } from 'react-router';
import { Plus, Search } from 'lucide-react';
import { Mark } from '../../layout/Mark';
import { ArchiveGroupHeader, DeviceGroupHeader } from '../../components/GroupHeader';
import { strings } from '../../strings';
import { useDevices } from '../../stores/devices';
import { useFront } from '../../stores/front';
import {
  countWaiting,
  selectSessionLayout,
  selectSessionList,
  showsUnseenDot,
  useSessions,
} from '../../stores/sessions';
import { useSettings } from '../../stores/settings';
import type { Device, Session } from '../../protocol/types';
import { SessionRenameDialog } from '../sessions/SessionRenameDialog';
import { deviceCanRename } from '../sessions/rename';
import { SidebarItem } from './SidebarItem';

interface Props {
  activeKey: string;
  onNewSession: () => void;
}

export function Sidebar({ activeKey, onNewSession }: Props) {
  const sessionsRecord = useSessions((s) => s.sessions);
  // The agent filter is shared with the Sessions page; the status filter is not.
  // The conversation open beside this list changes status under the reader's
  // eyes, and a filtered list would drop its row the moment it did.
  const agentFilter = useSessions((s) => s.agentFilter);
  const devices = useDevices((s) => s.devices);
  const collapsedDevices = useSettings((s) => s.collapsedDevices);
  const archiveExpanded = useSettings((s) => s.archiveExpanded);
  const toggleDeviceCollapsed = useSettings((s) => s.toggleDeviceCollapsed);
  const toggleArchiveExpanded = useSettings((s) => s.toggleArchiveExpanded);
  const front = useFront((s) => s.key);
  const [query, setQuery] = useState('');
  // A55: the session whose Rename dialog is open, kept here and not in a row so
  // that the dialog outlives a row that moves between lists.
  const [renaming, setRenaming] = useState<Session | null>(null);
  const navigate = useNavigate();

  const needle = query.trim();
  const groups = useMemo(
    () =>
      selectSessionLayout(sessionsRecord, devices, {
        agentFilter,
        query: needle,
        collapsedDevices,
        archiveExpanded,
      }),
    [sessionsRecord, devices, agentFilter, needle, collapsedDevices, archiveExpanded],
  );

  const waiting = useMemo(
    () => countWaiting(selectSessionList(sessionsRecord)),
    [sessionsRecord],
  );

  const item = (session: Session, device: Device) => {
    const key = `${session.device_id}/${session.session_id}`;
    return (
      <SidebarItem
        key={key}
        session={session}
        online={device.online}
        active={key === activeKey}
        // A47: the conversation in front of the person is being looked at, so
        // its row never shows the dot the `session.seen` on its way clears.
        unseen={showsUnseenDot(session, front)}
        onOpen={() => navigate(`/sessions/${key}`)}
        onRename={deviceCanRename(device, session) ? setRenaming : undefined}
      />
    );
  };

  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <Link to="/sessions" className="brand sidebar-brand">
          <Mark size={18} />
          <span>{strings.productName}</span>
        </Link>
        <button
          type="button"
          className="icon-btn"
          aria-label={strings.chat.newSession}
          onClick={onNewSession}
        >
          <Plus size={16} />
        </button>
      </div>

      <label className="search-field sidebar-search">
        <Search size={14} aria-hidden />
        <input
          type="search"
          value={query}
          placeholder={strings.chat.searchPlaceholder}
          aria-label={strings.chat.searchPlaceholder}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>

      <nav className="sidebar-list scroll-thin" aria-label={strings.nav.sessions}>
        {groups.map((group) => (
          <section key={group.device.device_id}>
            <DeviceGroupHeader
              name={group.device.name}
              online={group.device.online}
              expanded={!group.collapsed}
              onToggle={() => toggleDeviceCollapsed(group.device.device_id)}
              locked={group.revealing}
            />
            {group.collapsed ? null : (
              <>
                {group.active.length > 0 ? (
                  <ul>{group.active.map((session) => item(session, group.device))}</ul>
                ) : null}
                {group.archive.length > 0 ? (
                  <>
                    <ArchiveGroupHeader
                      count={group.archive.length}
                      expanded={group.archiveExpanded}
                      onToggle={() => toggleArchiveExpanded(group.device.device_id)}
                      locked={group.revealing}
                    />
                    {group.archiveExpanded ? (
                      <ul>{group.archive.map((session) => item(session, group.device))}</ul>
                    ) : null}
                  </>
                ) : null}
              </>
            )}
          </section>
        ))}
      </nav>

      <footer className="sidebar-foot hint">
        {strings.chat.sidebarFooter(devices.length, waiting)}
      </footer>

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
