/**
 * A33 — a device has a page: the machine's own facts, then one card per coding
 * agent it found, how each is signed in and what is left of its quota.
 * `docs/DESIGN.md` § "A device has a page".
 *
 * The hostname and the architecture live here alone: the row dropped both, and
 * this line under the title is where someone goes to check them. No client
 * version and no build hash, here or anywhere else (A36) — a device keeps
 * itself current, so the number is nobody's to watch.
 *
 * Rename and Revoke belong to the row and are deliberately not repeated here.
 * What is here is what went wrong: the same "Updating…" and "Update failed"
 * the row says, and the Retry the failure earns.
 */
import { useEffect, useState } from 'react';
import { ChevronLeft, RefreshCw } from 'lucide-react';
import { Link, useParams } from 'react-router';
import { Button } from '../../components/Button';
import { ConfirmDialog } from '../../components/Modal';
import { OnlineDot } from '../../components/StatusDot';
import { cx } from '../../lib/cx';
import { strings } from '../../strings';
import { useAuth } from '../../stores/auth';
import { selectDevice, updateNotice, useDevices } from '../../stores/devices';
import { AgentCard } from './AgentCard';
import { useDeviceQuota } from './useDeviceQuota';
import './device-page.css';

export function DevicePage() {
  const { deviceId } = useParams();
  const device = useDevices(selectDevice(deviceId));
  const loaded = useDevices((s) => s.loaded);
  const load = useDevices((s) => s.load);
  const requestUpdate = useDevices((s) => s.requestUpdate);
  const updateError = useDevices((s) => (deviceId ? s.updateErrors[deviceId] : undefined));
  // A22: the wheel this gateway serves, and the version a retry would install.
  const served = useAuth((s) => s.config?.client);
  const quota = useDeviceQuota(deviceId, device?.online ?? false);
  const [retrying, setRetrying] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!loaded) void load().catch(() => undefined);
  }, [loaded, load]);

  const back = (
    <Link to="/devices" className="device-page-back">
      <ChevronLeft size={15} aria-hidden />
      {strings.devicePage.back}
    </Link>
  );

  if (!device) {
    return (
      <>
        {back}
        {loaded ? (
          <div className="card empty">
            <strong>{strings.devicePage.gone}</strong>
            {strings.devicePage.goneHint}
          </div>
        ) : null}
      </>
    );
  }

  const agents = device.agents.filter((agent) => agent.available);
  const notice = updateNotice(device, updateError);
  // A36: the same two reasons the row gives for a retry it cannot send.
  const blocked = !device.online
    ? strings.devices.deviceOffline
    : served === undefined
      ? strings.devices.updateNoBuild
      : null;

  return (
    <>
      {back}
      <div className="page-head">
        <div className="device-page-head">
          <h1 className="device-page-title">
            <OnlineDot online={device.online} />
            <span>{device.name}</span>
          </h1>
          <p className="hint mono">
            {device.hostname} · {device.platform} · {device.arch}
          </p>
          {notice ? (
            <div className={cx('device-page-update', notice.tone)}>
              <span>{notice.text}</span>
              {notice.tone === 'failed' ? (
                <Button
                  small
                  disabled={blocked !== null}
                  title={blocked ?? undefined}
                  onClick={() => setRetrying(true)}
                >
                  {strings.devices.retryUpdate}
                </Button>
              ) : null}
            </div>
          ) : null}
        </div>
        <Button onClick={quota.refresh} disabled={quota.status === 'checking'}>
          <RefreshCw size={15} aria-hidden />
          {strings.devicePage.refresh}
        </Button>
      </div>

      {agents.length === 0 ? (
        <p className="hint device-page-none">{strings.devices.noAgents}</p>
      ) : (
        <ul className="device-page-agents">
          {agents.map((agent) => (
            <AgentCard
              key={agent.agent}
              agent={agent}
              accounts={quota.accounts[agent.agent] ?? agent.accounts}
              status={quota.status}
              error={quota.error}
            />
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={retrying}
        title={strings.devices.updateTitle}
        body={strings.devices.updateBody(device.name, served?.version)}
        confirmLabel={strings.devices.updateConfirm}
        busy={busy}
        onClose={() => setRetrying(false)}
        onConfirm={async () => {
          if (served === undefined) return;
          setBusy(true);
          try {
            await requestUpdate(device.device_id, served.build);
            setRetrying(false);
          } finally {
            setBusy(false);
          }
        }}
      />
    </>
  );
}
