import { useMemo } from 'react';
import { useNavigate } from 'react-router';
import { ArchiveGroupHeader, DeviceGroupHeader } from '../../components/GroupHeader';
import { strings } from '../../strings';
import { useDevices } from '../../stores/devices';
import { useFront } from '../../stores/front';
import { selectSessionLayout, showsUnseenDot, useSessions } from '../../stores/sessions';
import { useSettings } from '../../stores/settings';
import type { Device, Session } from '../../protocol/types';
import { deviceCanRename } from '../sessions/rename';
import { SidebarItem } from './SidebarItem';

interface Props {
  activeKey: string;
  /** The sidebar's own search, as typed. */
  query: string;
  /** A55: opens the Rename dialog, which the sidebar owns. */
  onRename: (session: Session) => void;
}

/** The sidebar's sessions, by device, with each device's Archive folded under it. */
export function SidebarList({ activeKey, query, onRename }: Props) {
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
        onRename={deviceCanRename(device, session) ? onRename : undefined}
      />
    );
  };

  return (
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
  );
}
