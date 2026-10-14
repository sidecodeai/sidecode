import { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import { AgentLogo } from '../../components/AgentLogo';
import { Button } from '../../components/Button';
import { Drawer } from '../../components/Modal';
import { Menu } from '../../components/Popover';
import { Segmented } from '../../components/Segmented';
import { Switch } from '../../components/Switch';
import { OnlineDot } from '../../components/StatusDot';
import { latency, relativeAgo, tildePath } from '../../lib/format';
import { rpc } from '../../lib/gateway';
import { agentLabel, strings } from '../../strings';
import { useDevices } from '../../stores/devices';
import { useSessions } from '../../stores/sessions';
import type { AgentInfo, Choice, Device } from '../../protocol/types';
import type { SessionOptions } from '../chat/sessionOptions';
import { DirectoryPicker } from './DirectoryPicker';
import { useDirectoryProbe, type DirStatus } from './useDirectoryProbe';

/** The row id the picker uses for "no tier", which the wire spells as null. */
const STANDARD_SPEED = 'standard';

interface Props {
  open: boolean;
  onClose: () => void;
  presetDeviceId?: string;
}

/**
 * Mounted only while open so every field starts from a fresh, derived default
 * instead of being reset from an effect.
 */
export function NewSessionDrawer({ open, onClose, presetDeviceId }: Props) {
  const devices = useDevices((s) => s.devices);
  const online = useMemo(() => devices.filter((d) => d.online), [devices]);
  if (!open) return null;
  return (
    <NewSessionForm
      devices={online}
      onClose={onClose}
      {...(presetDeviceId ? { presetDeviceId } : {})}
    />
  );
}

interface FormProps {
  devices: Device[];
  presetDeviceId?: string;
  onClose: () => void;
}

function NewSessionForm({ devices, presetDeviceId, onClose }: FormProps) {
  const createSession = useSessions((s) => s.create);
  const navigate = useNavigate();

  const [deviceId, setDeviceId] = useState<string | null>(() => {
    if (presetDeviceId && devices.some((d) => d.device_id === presetDeviceId)) return presetDeviceId;
    return devices[0]?.device_id ?? null;
  });

  const device = useMemo(
    () => devices.find((d) => d.device_id === deviceId) ?? null,
    [devices, deviceId],
  );
  const agents = useMemo(() => device?.agents ?? [], [device]);

  const [pickedAgent, setPickedAgent] = useState<string | null>(null);
  const agent: AgentInfo | null = useMemo(() => {
    const explicit = agents.find((a) => a.agent === pickedAgent);
    return explicit ?? agents.find((a) => a.available) ?? agents[0] ?? null;
  }, [agents, pickedAgent]);

  /**
   * What the reader chose instead of the agent's own defaults. Cleared with the
   * agent, because a model id belongs to one agent and means nothing to another.
   */
  const [options, setOptions] = useState<SessionOptions>({});
  const models = agent?.models ?? [];
  const efforts = agent?.efforts ?? [];
  const modes = agent?.permission_modes ?? [];
  const speeds = agent?.speeds ?? [];
  const model = options.model ?? agent?.default_model ?? null;
  const effort = options.effort ?? agent?.default_effort ?? null;
  const permissionMode = options.permission_mode ?? agent?.default_permission_mode ?? null;
  const speed = options.speed ?? null;
  const labelOf = (list: { id: string; label: string }[], value: string | null) =>
    list.find((item) => item.id === value)?.label ?? '';

  const [cwd, setCwd] = useState('');
  const [cwdTouched, setCwdTouched] = useState(false);
  const [worktree, setWorktree] = useState(false);
  const [recent, setRecent] = useState<{ path: string; last_used: number }[]>([]);
  const [browsing, setBrowsing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load the device's home listing to seed the recent list and the default cwd.
  useEffect(() => {
    if (!deviceId) return;
    let cancelled = false;
    void rpc('device.dirs', { device_id: deviceId })
      .then((result) => {
        if (cancelled) return;
        setRecent(result.recent);
        setCwd((current) => (current.length > 0 ? current : (result.recent[0]?.path ?? result.path)));
      })
      .catch(() => {
        if (!cancelled) setRecent([]);
      });
    return () => {
      cancelled = true;
    };
  }, [deviceId]);

  const probe = useDirectoryProbe(deviceId, cwd);
  const canWorktree = agent?.capabilities.includes('worktree') ?? false;
  const canStart =
    !busy && device !== null && agent !== null && agent.available && cwd.trim().length > 0;

  const start = useCallback(async () => {
    if (!canStart || !device || !agent) return;
    setBusy(true);
    setError(null);
    try {
      const session = await createSession({
        device_id: device.device_id,
        agent: agent.agent,
        cwd: cwd.trim(),
        ...(model ? { model } : {}),
        ...(permissionMode ? { permission_mode: permissionMode } : {}),
        ...(effort ? { effort } : {}),
        // A21: omitted for the standard speed, which is what null means.
        ...(speed ? { speed } : {}),
        ...(canWorktree && worktree ? { worktree: true } : {}),
      });
      onClose();
      navigate(`/sessions/${session.device_id}/${session.session_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : strings.newSession.startFailed);
    } finally {
      setBusy(false);
    }
  }, [
    canStart,
    device,
    agent,
    cwd,
    model,
    effort,
    permissionMode,
    speed,
    canWorktree,
    worktree,
    createSession,
    onClose,
    navigate,
  ]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault();
        void start();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [start]);

  const git = probe.git;

  return (
    <Drawer
      open
      onClose={onClose}
      title={strings.newSession.title}
      subtitle={device ? strings.newSession.continuingOn(device.name) : strings.newSession.pickDevice}
      footer={
        <>
          {error ? <p className="login-error">{error}</p> : null}
          <Button variant="primary" block busy={busy} disabled={!canStart} onClick={() => void start()}>
            {busy ? strings.newSession.starting : strings.newSession.start}
            <kbd className="kbd-hint">⌘↵</kbd>
          </Button>
        </>
      }
    >
      <section>
        <span className="label">{strings.newSession.device}</span>
        <Menu
          ariaLabel={strings.newSession.device}
          align="start"
          disabled={devices.length === 0}
          value={deviceId}
          onSelect={(id) => {
            setDeviceId(id);
            setPickedAgent(null);
            setOptions({});
            if (!cwdTouched) setCwd('');
          }}
          options={devices.map((d) => ({
            id: d.device_id,
            label: d.name,
            description: latency(d.latency_ms),
          }))}
          label={
            <span className="select-row">
              <OnlineDot online={device?.online ?? false} />
              <span className="select-name">{device?.name ?? strings.newSession.noDevices}</span>
              <span className="mono select-aux">{device ? latency(device.latency_ms) : ''}</span>
            </span>
          }
        />
      </section>

      <section>
        <span className="label">{strings.newSession.agent}</span>
        {/* A25: the agents no longer fit side by side under their names, so each
            one is its logo and the line under the row names the one that is
            chosen (`docs/DESIGN.md` § "Agents"). */}
        <Segmented<string>
          ariaLabel={strings.newSession.agent}
          value={agent?.agent ?? ''}
          onChange={(next) => {
            setPickedAgent(next);
            setOptions({});
          }}
          options={agents.map((a) => ({
            value: a.agent,
            disabled: !a.available,
            name: a.available
              ? agentLabel(a.agent)
              : `${agentLabel(a.agent)} · ${strings.newSession.agentUnavailable}`,
            label: <AgentLogo agent={a.agent} />,
          }))}
        />
        {agent ? (
          <p className="agent-line">
            {agentLabel(agent.agent)}
            <span className="mono">
              {agent.version ? ` ${agent.version}` : ''}
              {agent.default_model ? ` · ${agent.default_model}` : ''}
            </span>
          </p>
        ) : null}
      </section>

      {models.length > 0 ? (
        <section>
          <span className="label">{strings.newSession.model}</span>
          <Menu
            ariaLabel={strings.newSession.model}
            align="start"
            value={model}
            onSelect={(id) => setOptions((current) => ({ ...current, model: id }))}
            options={models.map((m) => ({ id: m.id, label: m.label }))}
            label={labelOf(models, model)}
          />
        </section>
      ) : null}

      {efforts.length > 0 ? (
        <section>
          <span className="label">{strings.newSession.effort}</span>
          <Menu
            ariaLabel={strings.newSession.effort}
            align="start"
            value={effort}
            onSelect={(id) => setOptions((current) => ({ ...current, effort: id }))}
            options={efforts.map((e) => ({ id: e.id, label: e.label }))}
            label={labelOf(efforts, effort)}
          />
        </section>
      ) : null}

      {modes.length > 0 ? (
        <section>
          <span className="label">{strings.newSession.permissions}</span>
          <Menu
            ariaLabel={strings.newSession.permissions}
            align="start"
            value={permissionMode}
            onSelect={(id) => setOptions((current) => ({ ...current, permission_mode: id }))}
            options={modes.map((m) => ({ id: m.id, label: m.label }))}
            label={labelOf(modes, permissionMode)}
          />
        </section>
      ) : null}

      {speeds.length > 0 ? (
        <section>
          <div className="speed-row">
            <span className="label">{strings.newSession.speed}</span>
            <SpeedField
              speeds={speeds}
              value={speed}
              onChange={(next) => setOptions((current) => ({ ...current, speed: next }))}
            />
          </div>
        </section>
      ) : null}

      <section>
        <div className="label-row">
          <span className="label">{strings.newSession.workingDirectory}</span>
          <button
            type="button"
            className="link-btn"
            onClick={() => setBrowsing(true)}
            disabled={!deviceId}
          >
            {strings.newSession.browse}
          </button>
        </div>
        <div className="dir-field">
          <input
            className="field mono"
            value={cwd}
            spellCheck={false}
            aria-label={strings.newSession.workingDirectory}
            onChange={(e) => {
              setCwd(e.target.value);
              setCwdTouched(true);
            }}
          />
          <span className={`dir-status ${probe.status}`}>{dirStatusLabel(probe.status)}</span>
        </div>
        {recent.length > 0 ? (
          <ul className="recent-list">
            {recent.slice(0, 4).map((entry) => (
              <li key={entry.path}>
                <button
                  type="button"
                  onClick={() => {
                    setCwd(entry.path);
                    setCwdTouched(true);
                  }}
                >
                  <span className="mono">{tildePath(entry.path)}</span>
                  <span className="hint">{relativeAgo(entry.last_used)}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : null}
      </section>

      <section>
        <span className="label">{strings.newSession.git}</span>
        <div className="git-row">
          {git?.is_repo ? (
            <>
              <span className="mono git-branch">{git.branch}</span>
              <span className="hint">
                {git.dirty ? strings.newSession.gitDirty : strings.newSession.gitClean}
                {typeof git.ahead === 'number' ? ` · ${strings.newSession.gitAhead(git.ahead)}` : ''}
              </span>
            </>
          ) : (
            <span className="hint">{strings.newSession.notARepo}</span>
          )}
          {canWorktree && git?.is_repo ? (
            <div className="git-worktree">
              <span>{strings.newSession.isolateWorktree}</span>
              <Switch
                checked={worktree}
                onChange={setWorktree}
                label={strings.newSession.isolateWorktree}
              />
            </div>
          ) : null}
        </div>
      </section>

      <DirectoryPicker
        open={browsing}
        deviceId={deviceId}
        {...(cwd.trim() ? { startPath: cwd.trim() } : {})}
        onClose={() => setBrowsing(false)}
        onPick={(path) => {
          setCwd(path);
          setCwdTouched(true);
        }}
      />
    </Drawer>
  );
}

function dirStatusLabel(status: DirStatus): string {
  switch (status) {
    case 'exists':
      return strings.newSession.dirExists;
    case 'missing':
      return strings.newSession.dirMissing;
    case 'checking':
      return strings.newSession.dirChecking;
    default:
      return '';
  }
}

/**
 * A21: the speed tier a new session starts at. One tier is a switch — on is
 * that tier, off is the standard speed; an agent that ever lists several gets
 * the same list picker as the three fields above it.
 */
function SpeedField({
  speeds,
  value,
  onChange,
}: {
  speeds: Choice[];
  value: string | null;
  onChange: (next: string | null) => void;
}) {
  const only = speeds[0];
  if (speeds.length === 1 && only) {
    return (
      <Switch
        checked={value === only.id}
        onChange={(on) => onChange(on ? only.id : null)}
        label={strings.newSession.speed}
      />
    );
  }
  return (
    <Menu
      ariaLabel={strings.newSession.speed}
      align="end"
      value={value}
      onSelect={(id) => onChange(id === STANDARD_SPEED ? null : id)}
      options={[
        { id: STANDARD_SPEED, label: strings.composer.speedStandard },
        ...speeds.map((s) => ({ id: s.id, label: s.label })),
      ]}
      label={speeds.find((s) => s.id === value)?.label ?? strings.composer.speedStandard}
    />
  );
}
