/**
 * A38 — the shell on a device, at `/devices/:deviceId/terminal`.
 *
 * `docs/DESIGN.md` § "The terminal": the device's name as the title, a thin
 * status line under it, Close at the trailing edge, and the emulator taking
 * the rest of the page. Copy and paste are xterm's own, and the web has no key
 * bar — the keyboard here is real.
 *
 * Nothing that travels through the terminal is stored or logged by this app.
 */
import { useEffect } from 'react';
import { ArrowLeft } from 'lucide-react';
import { Link, useNavigate, useParams } from 'react-router';
import { Button } from '../../components/Button';
import { cx } from '../../lib/cx';
import { strings } from '../../strings';
import { selectDevice, useDevices } from '../../stores/devices';
import { useTerminal, type TerminalStatus } from './useTerminal';
import '@xterm/xterm/css/xterm.css';
import './terminal.css';

export function TerminalPage() {
  const { deviceId = '' } = useParams();
  const navigate = useNavigate();
  const device = useDevices(selectDevice(deviceId));
  const loaded = useDevices((s) => s.loaded);
  const load = useDevices((s) => s.load);

  useEffect(() => {
    if (!loaded) void load().catch(() => undefined);
  }, [loaded, load]);

  // Rule 20: a device that is offline or offers no terminal is never asked for
  // one. The row says so before it navigates; this is the same rule for a link
  // somebody kept, and for a device that goes offline while the page is open.
  const available = device?.online === true && device.terminal === true;
  // Destructured at once: an object that holds the emulator's ref is one the
  // compiler will not let a render read fields off.
  const { host, status, exitCode, error, gap, started, reconnect, restart, close } = useTerminal(
    deviceId,
    available,
  );

  const blocked = !loaded
    ? null
    : !device
      ? strings.terminal.gone
      : !device.online
        ? strings.devices.deviceOffline
        : device.terminal !== true
          ? strings.devices.noTerminal
          : null;
  const say = blocked !== null && !started ? blocked : null;

  return (
    <div className="terminal-page">
      <header className="terminal-head">
        <Link to="/devices" className="icon-btn terminal-back" aria-label={strings.terminal.back}>
          <ArrowLeft size={17} />
        </Link>
        <div className="terminal-heading">
          <h1>{device?.name ?? strings.terminal.title}</h1>
          {/* A device with no shell to give says so once, where the shell
              would have been, rather than in the status line as well. */}
          {say === null ? (
            <StatusLine
              status={status}
              exitCode={exitCode}
              error={error}
              gap={gap}
              onReconnect={reconnect}
              onRestart={restart}
            />
          ) : null}
        </div>
        <Button
          small
          onClick={() => {
            close();
            navigate('/devices');
          }}
        >
          {strings.common.close}
        </Button>
      </header>

      <div className="terminal-body">
        <div className="terminal-view" ref={host} />
        {say !== null ? <p className="terminal-blocked">{say}</p> : null}
      </div>
    </div>
  );
}

/**
 * Connecting · Connected · Disconnected with a Reconnect, and the shell's own
 * end with a New shell. A gap in `seq` is said rather than guessed at: bytes
 * were lost, and the screen below is missing them.
 */
interface StatusProps {
  status: TerminalStatus;
  exitCode: number | null;
  error: string | null;
  gap: boolean;
  onReconnect: () => void;
  onRestart: () => void;
}

function StatusLine({ status, exitCode, error, gap, onReconnect, onRestart }: StatusProps) {
  const word =
    status === 'exited'
      ? exitCode === null
        ? strings.terminal.exited
        : strings.terminal.exitedCode(exitCode)
      : status === 'connected'
        ? strings.terminal.connected
        : status === 'disconnected'
          ? strings.terminal.disconnected
          : strings.terminal.connecting;

  return (
    <p className="terminal-status" role="status">
      <span className={cx('terminal-state', status)}>{word}</span>
      {error !== null ? <span className="terminal-reason">{error}</span> : null}
      {gap ? <span className="terminal-reason">{strings.terminal.gap}</span> : null}
      {status === 'disconnected' ? (
        <button type="button" className="link-btn terminal-action" onClick={onReconnect}>
          {strings.terminal.reconnect}
        </button>
      ) : null}
      {status === 'exited' ? (
        <button type="button" className="link-btn terminal-action" onClick={onRestart}>
          {strings.terminal.newShell}
        </button>
      ) : null}
    </p>
  );
}
