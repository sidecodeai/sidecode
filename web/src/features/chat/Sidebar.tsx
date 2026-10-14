import { useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { Plus, Search } from 'lucide-react';
import { Mark } from '../../layout/Mark';
import { ArchiveGroupHeader, DeviceGroupHeader } from '../../components/GroupHeader';
import { strings } from '../../strings';
import { useDevices } from '../../stores/devices';
import { useFront } from '../../stores/front';
import { countWaiting, selectSessionLayout, selectSessionList, useSessions } from '../../stores/sessions';
import { useSettings } from '../../stores/settings';
import type { Session } from '../../protocol/types';
import { SidebarItem } from './SidebarItem';

interface Props {
  activeKey: string;
  onNewSession: () => void;
}

export function Sidebar({ activeKey, onNewSession }: Props) {
  const sessionsRecord = useSessions((s) => s.sessions);
  const agentFilter = useSessions((s) => s.agentFilter);
  const devices = useDevices((s) => s.devices);
  const collapsedDevices = useSettings((s) => s.collapsedDevices);
  const archiveExpanded = useSettings((s) => s.archiveExpanded);
  const toggleDeviceCollapsed = useSettings((s) => s.toggleDeviceCollapsed);
  const toggleArchiveExpanded = useSettings((s) => s.toggleArchiveExpanded);
  const front = useFront((s) => s.key);
  const [query, setQuery] = useState('');
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

  const item = (session: Session, online: boolean) => {
    const key = `${session.device_id}/${session.session_id}`;
    return (
      <SidebarItem
        key={key}
        session={session}
        online={online}
        active={key === activeKey}
        // A47: the conversation in front of the person is being looked at, so
        // its row never shows the dot the `session.seen` on its way clears.
        unseen={session.unseen === true && key !== front}
        onOpen={() => navigate(`/sessions/${key}`)}
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
            />
            {group.collapsed ? null : (
              <>
                {group.active.length > 0 ? (
                  <ul>{group.active.map((session) => item(session, group.device.online))}</ul>
                ) : null}
                {group.archive.length > 0 ? (
                  <>
                    <ArchiveGroupHeader
                      count={group.archive.length}
                      expanded={group.archiveExpanded}
                      onToggle={() => toggleArchiveExpanded(group.device.device_id)}
                    />
                    {group.archiveExpanded ? (
                      <ul>{group.archive.map((session) => item(session, group.device.online))}</ul>
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
    </aside>
  );
}
