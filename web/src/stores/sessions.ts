/** Gateway session index: the list rendered by Sessions and the chat sidebar. */
import { create } from 'zustand';
import { dotTone } from '../components/dotTone';
import { matchesStatus, type StatusFilter } from '../components/statusFilter';
import { api } from '../lib/api';
import { rpc } from '../lib/gateway';
import { sessionTitle } from '../strings';
import type { CreateSessionParams } from '../protocol/frames';
import type { Device, Session } from '../protocol/types';

export type SessionKey = string;

export const sessionKey = (deviceId: string, sessionId: string): SessionKey =>
  `${deviceId}/${sessionId}`;

export const keyOf = (session: Session): SessionKey =>
  sessionKey(session.device_id, session.session_id);

interface SessionsState {
  sessions: Record<SessionKey, Session>;
  loaded: boolean;
  /**
   * The agent the lists are filtered to, `null` for all of them. In memory on
   * purpose: it lives here rather than in a page so the Sessions page and the
   * chat sidebar always show the same slice.
   */
  agentFilter: string | null;
  setAgentFilter: (agent: string | null) => void;
  /**
   * The status the Sessions page is narrowed to, `'all'` for none. In memory on
   * purpose, like the agent filter, and here rather than in the page so that
   * opening a conversation and coming back keeps the choice. The chat sidebar
   * does not read it: the conversation open beside that list changes status
   * under the reader's eyes, and a filtered list would drop its row the moment
   * it did.
   */
  statusFilter: StatusFilter;
  setStatusFilter: (status: StatusFilter) => void;
  load: () => Promise<void>;
  replaceAll: (sessions: Session[]) => void;
  upsert: (session: Session) => void;
  remove: (deviceId: string, sessionId: string) => void;
  create: (params: CreateSessionParams) => Promise<Session>;
  /**
   * A39: end a session the device drives. The frame is `session.archive`
   * with `archived: true`; the device interrupts the turn, ends what it
   * holds for the agent and only then replies with the session archived,
   * unowned and stopped, which is what the list then draws.
   */
  close: (session: Session) => Promise<void>;
  /**
   * A55: rename a session in the agent itself. Nothing is drawn ahead of the
   * device: it normalises the name, may suffix it, and answers with the
   * session carrying the name the agent now holds, which is what the list then
   * draws. It rejects with the device's refusal, a `RequestError` whose code
   * is `conflict` for a busy terminal and `unsupported` for a session that
   * cannot be named from here.
   */
  rename: (sessionId: string, title: string) => Promise<Session>;
  takeover: (session: Session) => Promise<Session>;
  /**
   * A47: the person has this conversation in front of them. `session.seen`
   * goes only while this tab's copy says `unseen`, and once at a time; the
   * gateway's `session.updated` is what clears the copy. A failure says
   * nothing: the request is idempotent, and the next time the conversation
   * comes to the front it goes again.
   */
  markSeen: (deviceId: string, sessionId: string) => void;
  /** Sign-out: nothing of the previous account stays in the tab (`signOut`). */
  reset: () => void;
}

/** A47: the `session.seen` requests on their way, by session key. */
const seenInFlight = new Set<SessionKey>();

export const useSessions = create<SessionsState>((set, get) => ({
  sessions: {},
  loaded: false,
  agentFilter: null,
  statusFilter: 'all',

  setAgentFilter: (agentFilter) => set({ agentFilter }),
  setStatusFilter: (statusFilter) => set({ statusFilter }),

  load: async () => {
    const { sessions } = await api.sessions();
    get().replaceAll(sessions);
  },

  replaceAll: (sessions) =>
    set(() => {
      const next: Record<SessionKey, Session> = {};
      for (const s of sessions) next[keyOf(s)] = s;
      return { sessions: next, loaded: true };
    }),

  upsert: (session) =>
    set((s) => ({ sessions: { ...s.sessions, [keyOf(session)]: session } })),

  remove: (deviceId, sessionId) =>
    set((s) => {
      const next = { ...s.sessions };
      delete next[sessionKey(deviceId, sessionId)];
      return { sessions: next };
    }),

  reset: () => {
    seenInFlight.clear();
    set({ sessions: {}, loaded: false, agentFilter: null, statusFilter: 'all' });
  },

  create: async (params) => {
    const { session } = await rpc('session.create', params);
    get().upsert(session);
    return session;
  },

  close: async (session) => {
    const result = await rpc('session.archive', {
      session_id: session.session_id,
      archived: true,
    });
    get().upsert(result.session);
  },

  rename: async (sessionId, title) => {
    const result = await rpc('session.set', { session_id: sessionId, title });
    get().upsert(result.session);
    return result.session;
  },

  takeover: async (session) => {
    const result = await rpc('session.takeover', { session_id: session.session_id });
    get().upsert(result.session);
    return result.session;
  },

  markSeen: (deviceId, sessionId) => {
    const key = sessionKey(deviceId, sessionId);
    if (get().sessions[key]?.unseen !== true || seenInFlight.has(key)) return;
    seenInFlight.add(key);
    void rpc('session.seen', { session_id: sessionId })
      .catch(() => undefined)
      .finally(() => seenInFlight.delete(key));
  },
}));

/** Every session the user has not archived, newest activity first. */
export function selectSessionList(sessions: Record<SessionKey, Session>): Session[] {
  return Object.values(sessions)
    .filter((s) => !s.archived)
    .sort((a, b) => b.updated_at - a.updated_at);
}

const ATTENTION_STATES = new Set(['needs_approval', 'needs_input']);
const BUSY_STATES = new Set(['running', 'starting']);

export function countWaiting(sessions: Session[]): number {
  return sessions.filter((s) => ATTENTION_STATES.has(s.state)).length;
}

/**
 * Active is what a CLI or the device still holds: `control` is `remote`,
 * `terminal` or `shared`. `control: "none"` means the CLI exited and nothing
 * owns the session any more, so it belongs to the Archive with the sessions the
 * user archived by hand.
 */
export function isActiveSession(session: Session): boolean {
  return !session.archived && session.control !== 'none';
}

/**
 * A47: whether the session's row draws the red dot, and the app's icon counts
 * it. Only an active session does — one in the Archive waits for nobody,
 * whatever mark a gateway left on it — and the conversation in front of the
 * person draws none: it is being looked at.
 */
export function showsUnseenDot(session: Session, front: SessionKey | null = null): boolean {
  return session.unseen === true && isActiveSession(session) && keyOf(session) !== front;
}

/** A47: the number on the app's icon — the red dots the app draws. */
export function countUnseen(
  sessions: Record<SessionKey, Session>,
  front: SessionKey | null = null,
): number {
  return Object.values(sessions).filter((s) => showsUnseenDot(s, front)).length;
}

/** Attention first, then a running turn, then the rest. Lower sorts earlier. */
function activityRank(session: Session): number {
  if (ATTENTION_STATES.has(session.state)) return 0;
  if (BUSY_STATES.has(session.state)) return 1;
  return 2;
}

/** One device and everything of its own the current filters let through. */
export interface DeviceGroup {
  device: Device;
  /** Whether the user folded this device shut. Groups are open by default. */
  collapsed: boolean;
  active: Session[];
  archive: Session[];
  archiveExpanded: boolean;
  /**
   * A search or a status is showing this group whatever the user folded, and
   * its Archive open if it holds a match. Folding either then changes nothing on
   * screen, so a header that took the click would only flip the stored choice
   * behind the user's back: it takes none.
   */
  revealing: boolean;
}

export interface SessionLayoutOptions {
  /** Restrict the whole list to one device. */
  deviceFilter?: string | null;
  /** Restrict the whole list to one agent. Applied before grouping. */
  agentFilter?: string | null;
  /**
   * Restrict the whole list to one status: a session passes by the dot it shows
   * on its own device, so one on a device the gateway no longer lists counts as
   * offline. Applied before grouping, and like a search it shows what it found.
   */
  statusFilter?: StatusFilter;
  /** Free-text filter over title, working directory and device name. */
  query?: string;
  /** Device ids the user folded shut. */
  collapsedDevices?: string[];
  /** Device ids whose Archive sub-group is open. */
  archiveExpanded?: string[];
}

/**
 * A session on a device the gateway no longer lists still needs a group, so it
 * gets one named after its own id rather than disappearing from the list.
 */
function placeholderDevice(deviceId: string): Device {
  return {
    device_id: deviceId,
    name: deviceId,
    platform: 'linux',
    hostname: deviceId,
    arch: '',
    client_version: '',
    online: false,
    last_seen: 0,
    created_at: 0,
    latency_ms: null,
    agents: [],
  };
}

/**
 * The one grouping rule every session list follows: a group per device, its
 * active sessions first, then that device's own Archive. A device with nothing
 * left after the filters is not rendered at all.
 */
export function selectSessionLayout(
  sessions: Record<SessionKey, Session>,
  devices: Device[],
  options: SessionLayoutOptions = {},
): DeviceGroup[] {
  const {
    deviceFilter = null,
    agentFilter = null,
    statusFilter = 'all',
    collapsedDevices = [],
    archiveExpanded = [],
  } = options;

  const known = new Map(devices.map((d) => [d.device_id, d]));
  const needle = (options.query ?? '').trim().toLowerCase();
  const matches = (session: Session): boolean => {
    if (!needle) return true;
    const name = known.get(session.device_id)?.name ?? '';
    return `${sessionTitle(session)} ${session.cwd} ${name}`.toLowerCase().includes(needle);
  };

  const passesStatus = (session: Session): boolean =>
    matchesStatus(
      statusFilter,
      dotTone(session.state, session.control, known.get(session.device_id)?.online ?? false),
    );

  const visible = Object.values(sessions)
    .filter((s) => (deviceFilter ? s.device_id === deviceFilter : true))
    .filter((s) => (agentFilter ? s.agent === agentFilter : true))
    .filter(passesStatus)
    .filter(matches);

  const buckets = new Map<string, { active: Session[]; archive: Session[] }>();
  for (const session of visible) {
    let bucket = buckets.get(session.device_id);
    if (!bucket) {
      bucket = { active: [], archive: [] };
      buckets.set(session.device_id, bucket);
    }
    if (isActiveSession(session)) bucket.active.push(session);
    else bucket.archive.push(session);
  }

  // A search or a status reveals what it matched without persisting anything: a
  // device only reaches this point when it still holds a matching row, so under
  // either its group is open whatever the user folded shut, and so is any
  // Archive holding one — an Ended filter would otherwise show nothing but
  // closed Archives. Clearing the query or choosing All states hands both back
  // to the stored state.
  const revealing = needle.length > 0 || statusFilter !== 'all';

  const groups = [...buckets.entries()].map(([deviceId, bucket]) => ({
    device: known.get(deviceId) ?? placeholderDevice(deviceId),
    collapsed: !revealing && collapsedDevices.includes(deviceId),
    active: bucket.active.sort(
      (a, b) => activityRank(a) - activityRank(b) || b.updated_at - a.updated_at,
    ),
    archive: bucket.archive.sort((a, b) => b.updated_at - a.updated_at),
    archiveExpanded:
      archiveExpanded.includes(deviceId) || (revealing && bucket.archive.length > 0),
    revealing,
  }));

  // Devices with something live first, each side by its most recent activity.
  const lastActivity = (group: DeviceGroup): number =>
    Math.max(...[...group.active, ...group.archive].map((s) => s.updated_at));
  return groups.sort(
    (a, b) =>
      Number(b.active.length > 0) - Number(a.active.length > 0) ||
      lastActivity(b) - lastActivity(a),
  );
}

/** The agents present in the list, in a stable order, for the agent filter. */
export function selectAgents(sessions: Record<SessionKey, Session>): string[] {
  return [...new Set(Object.values(sessions).map((s) => s.agent))].sort();
}
