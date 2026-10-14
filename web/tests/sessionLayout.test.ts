/**
 * The grouping rule every session list follows: a group per device, its active
 * rows, then that device's own Archive. Both the Sessions page and the chat
 * sidebar read it from this one selector, so it is tested here rather than
 * through either list.
 */
import { describe, expect, it } from 'vitest';
import { keyOf, selectAgents, selectSessionLayout, type DeviceGroup } from '../src/stores/sessions';
import type { ControlOwner, Device, Session, SessionState } from '../src/protocol/types';

interface Part {
  id: string;
  device?: string;
  agent?: string;
  state?: SessionState;
  control?: ControlOwner;
  archived?: boolean;
  minutesAgo?: number;
  title?: string;
  cwd?: string;
}

function session(part: Part): Session {
  return {
    session_id: part.id,
    device_id: part.device ?? 'dev-a',
    title: part.title ?? part.id,
    cwd: part.cwd ?? '/work/api',
    agent: part.agent ?? 'claude',
    git: null,
    state: part.state ?? 'idle',
    state_detail: null,
    origin: 'remote',
    control: part.control ?? 'remote',
    model: null,
    permission_mode: null,
    effort: null,
    created_at: 0,
    updated_at: -(part.minutesAgo ?? 0) * 60_000,
    last_seq: 0,
    archived: part.archived ?? false,
    turn: null,
    todos: null,
    usage: null,
    queued: 0,
  };
}

function device(id: string, name = id, online = true): Device {
  return {
    device_id: id,
    name,
    platform: 'linux',
    hostname: name,
    arch: 'x86_64',
    client_version: '0.1.0',
    online,
    last_seen: 0,
    created_at: 0,
    latency_ms: null,
    agents: [],
  };
}

const index = (list: Session[]): Record<string, Session> =>
  Object.fromEntries(list.map((s) => [keyOf(s), s]));

const ids = (list: Session[]): string[] => list.map((s) => s.session_id);

const find = (groups: DeviceGroup[], deviceId: string): DeviceGroup => {
  const group = groups.find((g) => g.device.device_id === deviceId);
  if (!group) throw new Error(`no group for ${deviceId}`);
  return group;
};

const DEVICES = [device('dev-a', 'mac-studio'), device('dev-b', 'ci-runner', false)];

describe('selectSessionLayout', () => {
  it('splits a device into the rows a CLI still holds and its own Archive', () => {
    const sessions = index([
      session({ id: 'remote', control: 'remote' }),
      session({ id: 'terminal', control: 'terminal' }),
      session({ id: 'shared', control: 'shared' }),
      session({ id: 'gone', control: 'none', state: 'stopped' }),
    ]);

    const group = find(selectSessionLayout(sessions, DEVICES), 'dev-a');

    expect(ids(group.active).sort()).toEqual(['remote', 'shared', 'terminal']);
    expect(ids(group.archive)).toEqual(['gone']);
  });

  it('files a manually archived session under its own device, whatever holds it', () => {
    const sessions = index([
      session({ id: 'live', control: 'remote' }),
      session({ id: 'filed', control: 'remote', archived: true }),
      session({ id: 'filed-b', device: 'dev-b', control: 'shared', archived: true }),
    ]);

    const groups = selectSessionLayout(sessions, DEVICES);

    expect(ids(find(groups, 'dev-a').active)).toEqual(['live']);
    expect(ids(find(groups, 'dev-a').archive)).toEqual(['filed']);
    expect(ids(find(groups, 'dev-b').active)).toEqual([]);
    expect(ids(find(groups, 'dev-b').archive)).toEqual(['filed-b']);
  });

  it('leaves the archive empty for a device that has nothing in it', () => {
    const sessions = index([session({ id: 'live' })]);

    expect(find(selectSessionLayout(sessions, DEVICES), 'dev-a').archive).toEqual([]);
  });

  it('renders no group for a device with nothing to show', () => {
    const sessions = index([session({ id: 'only', device: 'dev-a' })]);

    const groups = selectSessionLayout(sessions, DEVICES);

    expect(groups.map((g) => g.device.device_id)).toEqual(['dev-a']);
  });

  it('puts devices with something live first, then both halves by last activity', () => {
    const sessions = index([
      session({ id: 'quiet-new', device: 'dev-b', control: 'none', minutesAgo: 1 }),
      session({ id: 'live-old', device: 'dev-a', control: 'remote', minutesAgo: 90 }),
      session({ id: 'live-new', device: 'dev-c', control: 'remote', minutesAgo: 5 }),
      session({ id: 'quiet-old', device: 'dev-d', control: 'none', minutesAgo: 300 }),
    ]);
    const devices = [...DEVICES, device('dev-c'), device('dev-d')];

    const groups = selectSessionLayout(sessions, devices);

    expect(groups.map((g) => g.device.device_id)).toEqual(['dev-c', 'dev-a', 'dev-b', 'dev-d']);
  });

  it('orders a device by attention, then a running turn, then last activity', () => {
    const sessions = index([
      session({ id: 'idle-new', state: 'idle', minutesAgo: 1 }),
      session({ id: 'running-old', state: 'running', minutesAgo: 30 }),
      session({ id: 'starting-new', state: 'starting', minutesAgo: 2 }),
      session({ id: 'approval-old', state: 'needs_approval', minutesAgo: 90 }),
      session({ id: 'input-new', state: 'needs_input', minutesAgo: 5 }),
      session({ id: 'stopped-new', state: 'stopped', minutesAgo: 0 }),
    ]);

    const group = find(selectSessionLayout(sessions, DEVICES), 'dev-a');

    expect(ids(group.active)).toEqual([
      'input-new',
      'approval-old',
      'starting-new',
      'running-old',
      'stopped-new',
      'idle-new',
    ]);
  });

  it('orders one device’s Archive by last activity', () => {
    const sessions = index([
      session({ id: 'old', control: 'none', minutesAgo: 120 }),
      session({ id: 'new', archived: true, minutesAgo: 3 }),
      session({ id: 'mid', control: 'none', minutesAgo: 40 }),
    ]);

    const group = find(selectSessionLayout(sessions, DEVICES), 'dev-a');

    expect(ids(group.archive)).toEqual(['new', 'mid', 'old']);
  });

  it('drops a whole group when the agent filter empties it', () => {
    const sessions = index([
      session({ id: 'a-claude', device: 'dev-a', agent: 'claude' }),
      session({ id: 'a-codex', device: 'dev-a', agent: 'codex' }),
      session({ id: 'b-codex', device: 'dev-b', agent: 'codex' }),
    ]);

    const claude = selectSessionLayout(sessions, DEVICES, { agentFilter: 'claude' });
    expect(claude.map((g) => g.device.device_id)).toEqual(['dev-a']);
    expect(ids(claude[0]!.active)).toEqual(['a-claude']);

    const codex = selectSessionLayout(sessions, DEVICES, { agentFilter: 'codex' });
    expect(codex.map((g) => g.device.device_id).sort()).toEqual(['dev-a', 'dev-b']);
  });

  it('restricts the list to one device', () => {
    const sessions = index([
      session({ id: 'a-live', device: 'dev-a' }),
      session({ id: 'a-gone', device: 'dev-a', control: 'none' }),
      session({ id: 'b-live', device: 'dev-b' }),
    ]);

    const groups = selectSessionLayout(sessions, DEVICES, { deviceFilter: 'dev-a' });

    expect(groups.map((g) => g.device.device_id)).toEqual(['dev-a']);
    expect(ids(groups[0]!.active)).toEqual(['a-live']);
    expect(ids(groups[0]!.archive)).toEqual(['a-gone']);
  });

  it('reads the collapse and archive state off the given device ids', () => {
    const sessions = index([
      session({ id: 'a-live', device: 'dev-a' }),
      session({ id: 'a-gone', device: 'dev-a', control: 'none' }),
      session({ id: 'b-live', device: 'dev-b' }),
      session({ id: 'b-gone', device: 'dev-b', control: 'none' }),
    ]);

    const groups = selectSessionLayout(sessions, DEVICES, {
      collapsedDevices: ['dev-b'],
      archiveExpanded: ['dev-a'],
    });

    expect(find(groups, 'dev-a').collapsed).toBe(false);
    expect(find(groups, 'dev-a').archiveExpanded).toBe(true);
    expect(find(groups, 'dev-b').collapsed).toBe(true);
    expect(find(groups, 'dev-b').archiveExpanded).toBe(false);
  });

  it('opens an Archive a search reached into, without touching the stored ids', () => {
    const sessions = index([
      session({ id: 'live', title: 'Fix the ingest regression' }),
      session({ id: 'gone', control: 'none', title: 'Ingest docs rewrite' }),
    ]);

    const matched = find(selectSessionLayout(sessions, DEVICES, { query: 'docs' }), 'dev-a');
    expect(ids(matched.active)).toEqual([]);
    expect(ids(matched.archive)).toEqual(['gone']);
    expect(matched.archiveExpanded).toBe(true);

    const missed = find(selectSessionLayout(sessions, DEVICES, { query: 'regression' }), 'dev-a');
    expect(ids(missed.archive)).toEqual([]);
    expect(missed.archiveExpanded).toBe(false);
  });

  it('opens a folded device a search matched, without touching the stored ids', () => {
    const sessions = index([
      session({ id: 'live', title: 'Fix the ingest regression' }),
      session({ id: 'gone', control: 'none', title: 'Ingest docs rewrite' }),
    ]);
    const collapsedDevices = ['dev-a'];

    const shut = find(selectSessionLayout(sessions, DEVICES, { collapsedDevices }), 'dev-a');
    expect(shut.collapsed).toBe(true);

    const searched = find(
      selectSessionLayout(sessions, DEVICES, { collapsedDevices, query: 'regression' }),
      'dev-a',
    );
    expect(searched.collapsed).toBe(false);
    expect(ids(searched.active)).toEqual(['live']);
    expect(collapsedDevices).toEqual(['dev-a']);

    // A match that is only inside the Archive opens the group and the Archive.
    const inArchive = find(
      selectSessionLayout(sessions, DEVICES, { collapsedDevices, query: 'docs' }),
      'dev-a',
    );
    expect(inArchive.collapsed).toBe(false);
    expect(inArchive.archiveExpanded).toBe(true);

    // Clearing the query hands the group back to the stored state.
    const cleared = find(selectSessionLayout(sessions, DEVICES, { collapsedDevices }), 'dev-a');
    expect(cleared.collapsed).toBe(true);
  });

  it('searches the title, the working directory and the device name', () => {
    const sessions = index([
      session({ id: 'one', title: 'Fix flaky auth test', cwd: '/work/gateway' }),
      session({ id: 'two', title: 'Add traces', cwd: '/work/ingest' }),
      session({ id: 'three', device: 'dev-b', title: 'Nightly sweep', cwd: '/work/api' }),
    ]);

    const byTitle = selectSessionLayout(sessions, DEVICES, { query: 'FLAKY' });
    expect(ids(find(byTitle, 'dev-a').active)).toEqual(['one']);

    const byPath = selectSessionLayout(sessions, DEVICES, { query: 'ingest' });
    expect(ids(find(byPath, 'dev-a').active)).toEqual(['two']);

    const byDevice = selectSessionLayout(sessions, DEVICES, { query: 'ci-runner' });
    expect(byDevice.map((g) => g.device.device_id)).toEqual(['dev-b']);
  });

  it('gives a session on a device the gateway never listed a group of its own', () => {
    const sessions = index([session({ id: 'orphan', device: 'dev-gone' })]);

    const groups = selectSessionLayout(sessions, DEVICES);

    expect(groups.map((g) => g.device.name)).toEqual(['dev-gone']);
    expect(groups[0]!.device.online).toBe(false);
  });
});

describe('selectAgents', () => {
  it('lists the agents the sessions actually run, once each', () => {
    const sessions = index([
      session({ id: 'one', agent: 'codex' }),
      session({ id: 'two', agent: 'claude' }),
      session({ id: 'three', agent: 'codex' }),
    ]);

    expect(selectAgents(sessions)).toEqual(['claude', 'codex']);
  });
});
