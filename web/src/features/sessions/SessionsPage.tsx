import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus, Search } from 'lucide-react';
import { ArchiveGroupHeader, DeviceGroupHeader } from '../../components/GroupHeader';
import { Button } from '../../components/Button';
import { Menu } from '../../components/Popover';
import { strings } from '../../strings';
import { useDevices } from '../../stores/devices';
import { selectAgents, selectSessionLayout, useSessions } from '../../stores/sessions';
import { useSettings } from '../../stores/settings';
import { AgentOption } from './AgentOption';
import { NewSessionDrawer } from './NewSessionDrawer';
import { SessionLegend } from './SessionLegend';
import { SessionRow } from './SessionRow';
import './sessions.css';

/** The agent filter uses this id for "no filter", so it stays a plain string. */
const ALL = '__all__';

export function SessionsPage() {
  const sessionsRecord = useSessions((s) => s.sessions);
  const sessionsLoaded = useSessions((s) => s.loaded);
  const loadSessions = useSessions((s) => s.load);
  const agentFilter = useSessions((s) => s.agentFilter);
  const setAgentFilter = useSessions((s) => s.setAgentFilter);
  const devices = useDevices((s) => s.devices);
  const collapsedDevices = useSettings((s) => s.collapsedDevices);
  const archiveExpanded = useSettings((s) => s.archiveExpanded);
  const toggleDeviceCollapsed = useSettings((s) => s.toggleDeviceCollapsed);
  const toggleArchiveExpanded = useSettings((s) => s.toggleArchiveExpanded);
  const [query, setQuery] = useState('');
  const [deviceFilter, setDeviceFilter] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    if (!sessionsLoaded) void loadSessions().catch(() => undefined);
  }, [sessionsLoaded, loadSessions]);

  const deviceNames = useMemo(
    () => Object.fromEntries(devices.map((d) => [d.device_id, d.name])),
    [devices],
  );
  const agents = useMemo(() => selectAgents(sessionsRecord), [sessionsRecord]);

  const needle = query.trim();
  const groups = useMemo(
    () =>
      selectSessionLayout(sessionsRecord, devices, {
        deviceFilter,
        agentFilter,
        query: needle,
        collapsedDevices,
        archiveExpanded,
      }),
    [sessionsRecord, devices, deviceFilter, agentFilter, needle, collapsedDevices, archiveExpanded],
  );

  const openSession = (deviceId: string, sessionId: string) =>
    navigate(`/sessions/${deviceId}/${sessionId}`);

  return (
    <>
      <div className="page-head">
        <h1>{strings.sessions.title}</h1>
        <Button variant="primary" onClick={() => setCreating(true)}>
          <Plus size={15} aria-hidden />
          {strings.sessions.new}
        </Button>
      </div>

      <div className="sessions-toolbar">
        <label className="search-field">
          <Search size={15} aria-hidden />
          <input
            type="search"
            placeholder={strings.sessions.searchPlaceholder}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label={strings.sessions.searchPlaceholder}
          />
        </label>
        {agents.length > 1 ? (
          <Menu
            ariaLabel={strings.sessions.agentFilter}
            align="end"
            // No filter is the "All agents" row, which the menu then marks.
            value={agentFilter ?? ALL}
            onSelect={(id) => setAgentFilter(id === ALL ? null : id)}
            label={agentFilter ? <AgentOption agent={agentFilter} /> : strings.sessions.allAgents}
            options={[
              { id: ALL, label: strings.sessions.allAgents },
              ...agents.map((agent) => ({ id: agent, label: <AgentOption agent={agent} /> })),
            ]}
          />
        ) : null}
        <Menu
          ariaLabel={strings.sessions.allDevices}
          align="end"
          label={deviceFilter ? (deviceNames[deviceFilter] ?? deviceFilter) : strings.sessions.allDevices}
          value={deviceFilter}
          onSelect={(id) => setDeviceFilter(id === ALL ? null : id)}
          options={[
            { id: ALL, label: strings.sessions.allDevices },
            ...devices.map((d) => ({ id: d.device_id, label: d.name })),
          ]}
        />
      </div>

      {groups.length > 0 ? <SessionLegend /> : null}

      {groups.length === 0 ? (
        <div className="card empty">
          <strong>{needle ? strings.sessions.noMatches : strings.sessions.empty}</strong>
          {needle ? null : strings.sessions.emptyHint}
        </div>
      ) : (
        groups.map((group) => (
          <section className="session-group" key={group.device.device_id}>
            <DeviceGroupHeader
              name={group.device.name}
              online={group.device.online}
              expanded={!group.collapsed}
              onToggle={() => toggleDeviceCollapsed(group.device.device_id)}
            />
            {group.collapsed ? null : (
              <>
                {group.active.length > 0 ? (
                  <ul className="session-list surface">
                    {group.active.map((session) => (
                      <SessionRow
                        key={`${session.device_id}/${session.session_id}`}
                        session={session}
                        online={group.device.online}
                        onOpen={() => openSession(session.device_id, session.session_id)}
                      />
                    ))}
                  </ul>
                ) : null}
                {group.archive.length > 0 ? (
                  <div className="session-archive-group">
                    <ArchiveGroupHeader
                      count={group.archive.length}
                      expanded={group.archiveExpanded}
                      onToggle={() => toggleArchiveExpanded(group.device.device_id)}
                    />
                    {group.archiveExpanded ? (
                      <ul className="session-list surface">
                        {group.archive.map((session) => (
                          <SessionRow
                            key={`${session.device_id}/${session.session_id}`}
                            session={session}
                            online={group.device.online}
                            onOpen={() => openSession(session.device_id, session.session_id)}
                          />
                        ))}
                      </ul>
                    ) : null}
                  </div>
                ) : null}
              </>
            )}
          </section>
        ))
      )}

      <NewSessionDrawer open={creating} onClose={() => setCreating(false)} />
    </>
  );
}
