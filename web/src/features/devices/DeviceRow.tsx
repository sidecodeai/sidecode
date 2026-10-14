/**
 * One registered device, as `docs/DESIGN.md` § "The device row" rules it: a
 * computer glyph at the leading edge, the name once, a status line of dot,
 * online word and platform word, and the agents as logos alone. A third line
 * appears only when an update is running or has failed (A36): the client
 * version is nobody's to watch, because the gateway keeps every device on the
 * wheel it serves without being asked. The hostname, the architecture and the
 * build hash live on the device's own page.
 *
 * A38, rule 20: the row's own tap opens a terminal on the machine. A device
 * that is offline or offers no shell says which of the two it is, beside the
 * row, and goes nowhere. Everything else is the menu's, in one order:
 * Rename · Retry update (only while failed) · Show quota · Revoke.
 */
import { useEffect, useRef, useState } from 'react';
import { LaptopMinimal, MoreHorizontal } from 'lucide-react';
import { Link } from 'react-router';
import { AgentLogo } from '../../components/AgentLogo';
import { Popover } from '../../components/Popover';
import { OnlineDot } from '../../components/StatusDot';
import { cx } from '../../lib/cx';
import { latency, relativeTime } from '../../lib/format';
import { agentLabel, platformLabel, strings } from '../../strings';
import { updateNotice } from '../../stores/devices';
import type { ClientBuildInfo } from '../../lib/api';
import type { Device } from '../../protocol/types';

interface Props {
  device: Device;
  sessionCount: number;
  /** A22: the client the gateway serves — its version and build — when it serves one. */
  served: ClientBuildInfo | undefined;
  /** A22: why this device's last `device.update` was refused outright. */
  updateError: string | undefined;
  onRename: () => void;
  onRetryUpdate: () => void;
  onRevoke: () => void;
}

/** How long the row keeps saying why it opened nothing. */
const NOTE_MS = 4_000;

export function DeviceRow({
  device,
  sessionCount,
  served,
  updateError,
  onRename,
  onRetryUpdate,
  onRevoke,
}: Props) {
  const agents = device.agents.filter((a) => a.available);
  const reach = device.online
    ? latency(device.latency_ms)
    : strings.devices.lastSeen(relativeTime(device.last_seen));

  const notice = updateNotice(device, updateError);
  const updating = device.update_state === 'updating';

  // A36: nobody asks for an update; a person only tries a failed one again. The
  // item is there while the failure is, and says why it cannot be pressed.
  const failed = notice?.tone === 'failed';
  const blocked = !device.online
    ? strings.devices.deviceOffline
    : served === undefined
      ? strings.devices.updateNoBuild
      : null;

  // A38: the tap opens a shell, or says why it cannot. `terminal` is absent on
  // a client older than the amendment, which is the same answer as `false`.
  const terminal = device.online && device.terminal === true;
  const refusal = !device.online ? strings.devices.deviceOffline : strings.devices.noTerminal;
  const [note, setNote] = useState<string | null>(null);
  const noteTimer = useRef<number | null>(null);
  useEffect(
    () => () => {
      if (noteTimer.current !== null) globalThis.clearTimeout(noteTimer.current);
    },
    [],
  );
  const refuse = () => {
    setNote(refusal);
    if (noteTimer.current !== null) globalThis.clearTimeout(noteTimer.current);
    noteTimer.current = globalThis.setTimeout(() => setNote(null), NOTE_MS) as unknown as number;
  };

  return (
    <li className="device-row">
      {/* The same glyph on every device, whatever its platform: a minimal
          outline laptop, a rounded screen over one base line. The app cannot
          tell a laptop from a desktop, and the mark is what keeps two rows
          apart now that nothing is drawn between them. */}
      <LaptopMinimal className="device-glyph" strokeWidth={1.5} aria-hidden />

      <div className="device-main">
        <div className="device-name">
          {/* A38: the row itself opens the terminal. The link is stretched over
              the whole row in CSS, and the menu is lifted above it, so its
              actions keep working and none of them navigates. A device that
              has no shell to give keeps the same box as a button that opens
              nothing, so the row does not change shape between the two. */}
          {terminal ? (
            <Link className="device-open" to={`/devices/${device.device_id}/terminal`}>
              {device.name}
            </Link>
          ) : (
            <button type="button" className="device-open" onClick={refuse}>
              {device.name}
            </button>
          )}
        </div>
        <div className="device-meta">
          <span className="device-status">
            <OnlineDot online={device.online} pulse={updating} />
            {device.online ? strings.devices.online : strings.devices.offline} ·{' '}
            {platformLabel(device.platform)}
          </span>
          <span className="device-reach">
            {sessionCount > 0
              ? strings.devices.sessionsCount(sessionCount)
              : strings.devices.noSessions}{' '}
            · {reach}
          </span>
        </div>
        {notice ? (
          <div className={cx('device-client', notice.tone)}>{notice.text}</div>
        ) : null}
        {note ? (
          <div className="device-note" role="status">
            {note}
          </div>
        ) : null}
      </div>

      <div className="device-agents">
        {agents.length === 0 ? (
          <span className="hint">{strings.devices.noAgents}</span>
        ) : (
          agents.map((agent) => (
            // The logo alone; its name is what a screen reader and a hover get,
            // and the version lives on the device page's agent card.
            <span
              className="device-agent"
              key={agent.agent}
              role="img"
              aria-label={agentLabel(agent.agent)}
              title={agentLabel(agent.agent)}
            >
              <AgentLogo agent={agent.agent} />
            </span>
          ))
        )}
      </div>

      <Popover
        chevron={false}
        ariaLabel={strings.a11y.openMenu}
        align="end"
        triggerClassName="device-menu-trigger"
        label={<MoreHorizontal size={16} aria-hidden />}
      >
        {(close) => (
          <ul className="menu" role="menu">
            <li>
              <button
                type="button"
                role="menuitem"
                className="menu-item"
                onClick={() => {
                  close();
                  onRename();
                }}
              >
                <span className="menu-label">{strings.common.rename}</span>
              </button>
            </li>
            {failed ? (
              <li>
                <button
                  type="button"
                  role="menuitem"
                  className="menu-item"
                  disabled={blocked !== null}
                  title={blocked ?? undefined}
                  onClick={() => {
                    close();
                    onRetryUpdate();
                  }}
                >
                  <span className="menu-label">{strings.devices.retryUpdate}</span>
                </button>
              </li>
            ) : null}
            {/* A33's page, reached from here now that the row itself opens a
                shell: the agents on the machine and what is left of each quota. */}
            <li>
              <Link
                role="menuitem"
                className="menu-item"
                to={`/devices/${device.device_id}`}
                onClick={close}
              >
                <span className="menu-label">{strings.devices.showQuota}</span>
              </Link>
            </li>
            <li>
              <button
                type="button"
                role="menuitem"
                className="menu-item danger-text"
                onClick={() => {
                  close();
                  onRevoke();
                }}
              >
                <span className="menu-label">{strings.common.revoke}</span>
              </button>
            </li>
          </ul>
        )}
      </Popover>
    </li>
  );
}
