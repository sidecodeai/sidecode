/**
 * Mock gateway for local web development (`npm run dev:mock`).
 *
 * Speaks the app-facing half of PROTOCOL-FROZEN.md: the HTTP API, `WS /ws/app`
 * with two scripted devices and five sessions, a scripted live turn, pairing
 * progress, history paging, and a fake `WS /ws/stt`.
 */
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { randomUUID } from 'node:crypto';
import { WebSocketServer, type WebSocket } from 'ws';
import type {
  AgentInfo,
  Device,
  Preferences,
  Session,
  SessionEvent,
  TodoItem,
  QueuedMessage,
} from '../src/protocol/types';
import {
  CLIENT_BUILD,
  CLIENT_VERSION,
  HOME,
  commandsFor,
  deviceAgents,
  devices,
  historyFor,
  queueFor,
  recentDirs,
  sessions,
} from './fixtures';
import {
  MEMBER_PASSWORD,
  MEMBER_USERNAME,
  createAccount,
  deleteAccount,
  getAccount,
  isRegistrationOpen,
  listAccounts,
  recordView,
  seedAccounts,
  setRegistrationOpen,
  userView,
  validPassword,
  validUsername,
  type Account,
  type Role,
  type State,
} from './accounts';
import {
  ECHO_DELAY_MS,
  afterAnswer,
  afterApproval,
  codexSharedAfterApproval,
  codexSharedTurn,
  commandScript,
  sharedAfterApproval,
  sharedTurn,
  turnScript,
  type Step,
} from './script';
import {
  ANSWER_THE_PROMPT,
  TERMINAL_BUSY,
  TYPING_MS,
  lockedKeys,
  promptOnScreen,
  settingKeys,
  terminalBusy,
  typedSession,
} from './typing';
import { ServedSends, needsResync, replayFor } from './replay';
import { filesField, heldTs, hold, readQueueTs } from './queue';
import { dirEntries, makeDir } from './dirs';
import { emptyPreferences, patchPreferences } from './preferences';
import { FakeShell } from './shell';
import { clearMark, moveState } from './unseen';

const PORT = Number(process.env.PORT ?? 8787);
const PASSWORD = process.env.RC_PASSWORD ?? 'dev';
const GATEWAY_VERSION = '0.1.0-mock';
const COOKIE = 'rc_session';

/* ----------------------------------------------------------------- state */

const state = {
  devices: structuredClone(devices) as Device[],
  sessions: structuredClone(sessions) as Session[],
  events: new Map<string, SessionEvent[]>(),
  todos: new Map<string, TodoItem[]>(),
  queues: new Map<string, QueuedMessage[]>(),
  pairings: new Map<string, { expires_at: number; timers: NodeJS.Timeout[] }>(),
  /** A23: claim tokens a host asked for; `code` appears once an app claims it. */
  claims: new Map<string, { expires_at: number; code?: string }>(),
  /** A24: the account each login token belongs to. */
  tokens: new Map<string, string>(),
  /** A24: who enrolled each device. The scripted ones are the admin's. */
  deviceOwner: new Map<string, string>(),
  /** A24: who asked for each outstanding pairing code. */
  pairingOwner: new Map<string, string>(),
  /** A35: one row of preferences per account; an absent row is the defaults. */
  preferences: new Map<string, Preferences>(),
  /**
   * A10: messages the device accepted but could not inject yet, per session,
   * in the `ts` order of the queue entries that stand for them (A43).
   */
  held: new Map<string, { block_id: string; text: string; queued_id: string; ts: number }[]>(),
  /** A12: what each `session.send` request id was already answered with. */
  served: new ServedSends(),
  /** A38: the shells this mock is running, by terminal id. */
  terminals: new Map<string, FakeShell>(),
  /** A38: the ten minutes a detached shell is kept, by terminal id. */
  detached: new Map<string, NodeJS.Timeout>(),
  /** A39: sessions an app closed. Nothing they were still saying gets out. */
  closed: new Set<string>(),
};

seedAccounts(PASSWORD);
for (const device of state.devices) state.deviceOwner.set(device.device_id, 'admin');

for (const session of state.sessions) {
  const history = historyFor(session.session_id);
  state.events.set(session.session_id, history);
  session.last_seq = history.at(-1)?.seq ?? 0;
}

// A43: what a session holds behind its turn when the mock starts, so the Up
// next chip, its list and an edit can be seen without queueing anything first.
for (const session of state.sessions) {
  const queue = queueFor(session.session_id);
  if (queue.length === 0) continue;
  state.queues.set(session.session_id, queue);
  session.queued = queue.length;
}

const findSession = (id: string): Session | undefined =>
  state.sessions.find((s) => s.session_id === id);

const agentFor = (session: Session): AgentInfo | undefined =>
  state.devices
    .find((d) => d.device_id === session.device_id)
    ?.agents.find((a) => a.agent === session.agent);

const blockIdOf = (event: SessionEvent): string | undefined =>
  'block_id' in event && typeof event.block_id === 'string' ? event.block_id : undefined;

/**
 * Amendment A8: every block event carries the seq at which the block first
 * appeared, so an app can order a late-finishing tool call correctly.
 */
function stampFirstSeq(history: readonly SessionEvent[], event: SessionEvent): void {
  const blockId = blockIdOf(event);
  if (!blockId || event.first_seq !== undefined) return;
  const first = history.find((e) => blockIdOf(e) === blockId);
  event.first_seq = first ? (first.first_seq ?? first.seq) : event.seq;
}

function nextSeq(sessionId: string): number {
  const session = findSession(sessionId);
  const seq = (session?.last_seq ?? 0) + 1;
  if (session) session.last_seq = seq;
  return seq;
}

/* ------------------------------------------------------------ connections */

interface AppConn {
  socket: WebSocket;
  /** A24: the account this socket signed in as. */
  username: string;
  subscriptions: Set<string>;
  /** A38: the terminals this connection holds, which it loses when it closes. */
  terminals: Set<string>;
}

const conns = new Set<AppConn>();

const send = (socket: WebSocket, frame: unknown): void => {
  if (socket.readyState === socket.OPEN) socket.send(JSON.stringify(frame));
};

/**
 * A24: every pushed frame belongs to the account that owns the device it is
 * about, so the owner is read from the frame rather than from every call site.
 */
function ownerOfFrame(frame: unknown): string | undefined {
  const f = frame as {
    type?: string;
    device?: { device_id?: string };
    device_id?: string;
    session?: { device_id?: string };
    code?: string;
  };
  if (f.type === 'pairing.progress') return state.pairingOwner.get(String(f.code));
  const deviceId = f.device?.device_id ?? f.session?.device_id ?? f.device_id;
  return deviceId ? state.deviceOwner.get(deviceId) : undefined;
}

function broadcast(frame: unknown): void {
  const owner = ownerOfFrame(frame);
  for (const conn of conns) {
    if (owner === undefined || conn.username === owner) send(conn.socket, frame);
  }
}

/** A35: a frame that is about the account itself rather than about a device. */
function broadcastTo(username: string, frame: unknown): void {
  for (const conn of conns) {
    if (conn.username === username) send(conn.socket, frame);
  }
}

/** A35: the account's preferences; an account that chose nothing reads off. */
const preferencesOf = (username: string): Preferences =>
  state.preferences.get(username) ?? emptyPreferences();

/**
 * A35 §6.3: a message the person sends into a session with a pending resume
 * cancels it. They got there first, and a second "continue" a minute later
 * would only spend the window again.
 */
function cancelResumeOnSend(sessionId: string): void {
  const session = findSession(sessionId);
  if (!session || session.resume == null) return;
  session.resume = null;
  broadcast({ type: 'session.updated', session });
  emit(sessionId, {
    seq: nextSeq(sessionId),
    ts: Date.now(),
    kind: 'resume',
    status: 'cancelled',
    reason: 'you sent a message',
  });
}

/** The devices and sessions one account may see. */
const devicesOf = (username: string) =>
  state.devices.filter((d) => state.deviceOwner.get(d.device_id) === username);

const sessionsOf = (username: string) =>
  state.sessions.filter((s) => state.deviceOwner.get(s.device_id) === username);

const ownsSession = (username: string, sessionId: string): boolean => {
  const session = findSession(sessionId);
  return session !== undefined && state.deviceOwner.get(session.device_id) === username;
};

function emit(sessionId: string, event: SessionEvent): void {
  // A39: a closed session is over. A scripted step still on its way — the end
  // of the turn the close interrupted — neither reaches an app nor moves the
  // row back out of the Archive.
  if (state.closed.has(sessionId)) return;
  const list = state.events.get(sessionId) ?? [];
  stampFirstSeq(list, event);
  list.push(event);
  state.events.set(sessionId, list);
  applyToSummary(sessionId, event);
  const deviceId = findSession(sessionId)?.device_id;
  for (const conn of conns) {
    if (conn.subscriptions.has(sessionId)) {
      // Amendment A5: the gateway knows the device from the socket.
      send(conn.socket, { type: 'session.event', session_id: sessionId, device_id: deviceId, event });
    }
  }
}

function applyToSummary(sessionId: string, event: SessionEvent): void {
  const session = findSession(sessionId);
  if (!session) return;
  session.updated_at = event.ts;
  if (event.kind === 'status') {
    // A47: a turn that ends, or stops to ask, marks the session; working
    // again clears it. The broadcast below carries the mark either way.
    moveState(session, event.state);
    session.state_detail = event.detail ?? null;
  }
  if (event.kind === 'todos') {
    state.todos.set(sessionId, event.items);
    session.todos = {
      total: event.items.length,
      done: event.items.filter((i) => i.status === 'completed').length,
    };
  }
  if (event.kind === 'turn_started') session.turn = { turn_id: event.turn_id, started_at: event.ts };
  if (event.kind === 'turn_completed') {
    session.turn = null;
    if (event.usage) session.usage = event.usage;
  }
  if (event.kind === 'queue') {
    state.queues.set(sessionId, event.pending);
    session.queued = event.pending.length;
  }
  broadcast({ type: 'session.updated', session });
}

/** Play a list of scripted steps against a session. */
function play(sessionId: string, steps: Step[], done?: () => void): void {
  for (const step of steps) {
    setTimeout(() => {
      const ts = Date.now();
      emit(sessionId, step.event(nextSeq(sessionId), ts));
    }, step.after);
  }
  const total = steps.at(-1)?.after ?? 0;
  if (done) setTimeout(done, total + 20);
}

/**
 * A remote turn, followed by whatever the queue collected while it ran. The
 * dequeued message keeps its original request id as its `block_id` (A12).
 */
function playRemote(sessionId: string, text: string, blockId: string): void {
  play(sessionId, turnScript(text, blockId), () => drainQueue(sessionId));
}

function drainQueue(sessionId: string): void {
  const [next, ...rest] = state.queues.get(sessionId) ?? [];
  if (!next) return;
  emit(sessionId, { seq: nextSeq(sessionId), ts: Date.now(), kind: 'queue', pending: rest });
  setTimeout(() => playRemote(sessionId, next.text, next.id), 400);
}

/**
 * A39: `session.archive {archived: true}` closes the session. A real device
 * interrupts the turn, ends what it holds for the agent and only then records
 * the choice, so the mock stops the session's events at once and publishes it
 * archived, unowned and stopped in one go. A working session takes a moment
 * over it, the way ending a process does.
 */
const CLOSE_DELAY_MS = 500;

function closeSession(session: Session, done: () => void): void {
  const working = session.state === 'running' || session.state === 'starting';
  // Marked before the wait rather than after it: nothing the agent says while
  // the close is in progress reaches an app or revives the row.
  state.closed.add(session.session_id);
  const finish = () => {
    session.turn = null;
    session.archived = true;
    session.control = 'none';
    moveState(session, 'stopped');
    // A47: an archived session waits for nobody.
    clearMark(session);
    session.state_detail = null;
    session.updated_at = Date.now();
    broadcast({ type: 'session.updated', session });
    done();
  };
  if (working) setTimeout(finish, CLOSE_DELAY_MS);
  else finish();
}

/** A15: writing to a closed session brings it back to life. */
function reviveSession(session: Session): void {
  const wasClosed = state.closed.delete(session.session_id);
  if (!wasClosed && !session.archived) return;
  session.archived = false;
  if (session.control === 'none') session.control = 'remote';
  broadcast({ type: 'session.updated', session });
}

/** Ends the running turn as `interrupted` and leaves the session idle. */
function interruptTurn(sessionId: string): void {
  const session = findSession(sessionId);
  if (!session?.turn) return;
  emit(sessionId, {
    seq: nextSeq(sessionId),
    ts: Date.now(),
    kind: 'turn_completed',
    turn_id: session.turn.turn_id,
    stop_reason: 'interrupted',
    duration_ms: Date.now() - session.turn.started_at,
  });
  emit(sessionId, { seq: nextSeq(sessionId), ts: Date.now(), kind: 'status', state: 'idle' });
}

/* -------------------------------------------------------------- http api */

function json(res: ServerResponse, status: number, body: unknown, headers: Record<string, string> = {}): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(payload),
    ...headers,
  });
  res.end(payload);
}

const unauthorized = (res: ServerResponse): void =>
  json(res, 401, { ok: false, error: { code: 'unauthorized', message: 'sign in first' } });

const failure = (code: string, message: string) => ({ ok: false, error: { code, message } });

/** A29: long enough for the composer to say "Polishing…", short enough to wait. */
const POLISH_DELAY_MS = 600;

/**
 * A29: what a polish model would have done to a dictation, done with two
 * regular expressions — the fillers go, a stammered word is said once, and the
 * sentence starts with a capital and ends with a stop. The real thing reads the
 * conversation too; this one only has to make the flow visible in the app.
 */
function fakePolish(text: string): string {
  const said = text
    .replace(/\b(?:um|uh|erm|you know)\b[,]?\s*/gi, '')
    .replace(/\b(\w+)(\s+\1\b)+/gi, '$1')
    .replace(/\s{2,}/g, ' ')
    .trim();
  if (said.length === 0) return text.trim();
  const sentence = said[0]!.toUpperCase() + said.slice(1);
  return /[.!?]$/.test(sentence) ? sentence : `${sentence}.`;
}

/** A24: the account a request carries, by cookie or by bearer, or nobody. */
function caller(req: IncomingMessage): Account | undefined {
  const cookie = req.headers.cookie ?? '';
  const token =
    /rc_session=([^;]+)/.exec(cookie)?.[1] ??
    req.headers.authorization?.replace(/^Bearer\s+/i, '');
  const username = token ? state.tokens.get(token) : undefined;
  return username ? getAccount(username) : undefined;
}

/** Signs an account in: a token, the cookie header, and the `LoginResponse`. */
function signIn(res: ServerResponse, account: Account): void {
  const token = randomUUID();
  state.tokens.set(token, account.username);
  account.last_login_at = Date.now();
  json(
    res,
    200,
    { ok: true, token, exp: Date.now() + 30 * 86_400_000, user: userView(account) },
    { 'Set-Cookie': `${COOKIE}=${token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000` },
  );
}

const deviceCount = (username: string): number => devicesOf(username).length;

async function readBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of req) chunks.push(chunk as Buffer);
  if (chunks.length === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>;
  } catch {
    return {};
  }
}

const CODE_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

function pairingCode(): string {
  const group = () =>
    Array.from({ length: 4 }, () => CODE_ALPHABET[Math.floor(Math.random() * CODE_ALPHABET.length)]).join('');
  return `RC-${group()}-${group()}`;
}

/** A23: 26 Crockford characters, as the gateway mints from 16 random bytes. */
function claimToken(): string {
  return Array.from(
    { length: 26 },
    () => CODE_ALPHABET[Math.floor(Math.random() * CODE_ALPHABET.length)],
  ).join('');
}

type ClaimLookup =
  | { status: 'unknown' | 'expired' }
  | { status: 'live'; request: { expires_at: number; code?: string } };

/** Unknown is a 404 and expired a 410, so the two are never the same answer. */
function lookupClaim(token: string): ClaimLookup {
  const request = state.claims.get(token);
  if (!request) return { status: 'unknown' };
  if (request.expires_at <= Date.now()) {
    state.claims.delete(token);
    return { status: 'expired' };
  }
  return { status: 'live', request };
}

function startPairing(code: string, owner: string): void {
  state.pairingOwner.set(code, owner);
  const timers: NodeJS.Timeout[] = [];
  const emitStep = (delay: number, step: 'waiting' | 'enrolled' | 'online' | 'agents', withDevice = false) =>
    timers.push(
      setTimeout(() => {
        broadcast({
          type: 'pairing.progress',
          code,
          step,
          ...(withDevice ? { device: newDevice(code) } : {}),
        });
      }, delay),
    );
  emitStep(300, 'waiting');
  emitStep(2_600, 'enrolled');
  emitStep(4_200, 'online', true);
  emitStep(6_000, 'agents', true);
  timers.push(
    setTimeout(() => {
      const device = newDevice(code);
      state.deviceOwner.set(device.device_id, owner);
      if (!state.devices.some((d) => d.device_id === device.device_id)) state.devices.push(device);
      broadcast({ type: 'device.updated', device });
    }, 6_100),
  );
  state.pairings.set(code, { expires_at: Date.now() + 10 * 60_000, timers });
}

function newDevice(code: string): Device {
  const template = state.devices[0];
  if (!template) throw new Error('no template device');
  return {
    ...template,
    device_id: `dev-${code.slice(3, 7).toLowerCase()}`,
    name: 'new-laptop',
    hostname: 'new-laptop.local',
    latency_ms: 24,
    created_at: Date.now(),
    last_seen: Date.now(),
  };
}

const server = createServer((req, res) => {
  void handleHttp(req, res).catch(() => {
    json(res, 500, { ok: false, error: { code: 'internal', message: 'mock failure' } });
  });
});

async function handleHttp(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = new URL(req.url ?? '/', `http://127.0.0.1:${PORT}`);
  const path = url.pathname;
  const method = req.method ?? 'GET';

  if (path === '/api/health') {
    json(res, 200, {
      ok: true,
      version: GATEWAY_VERSION,
      protocol: 1,
      auth: { mode: 'password', registration_open: isRegistrationOpen() },
    });
    return;
  }

  if (path === '/api/login' && method === 'POST') {
    const body = await readBody(req);
    const username = typeof body.username === 'string' ? body.username.toLowerCase() : '';
    if (username.length === 0) {
      json(res, 400, failure('bad_request', 'username is required'));
      return;
    }
    const account = getAccount(username);
    if (!account || account.password !== body.password) {
      json(res, 401, failure('unauthorized', 'wrong username or password'));
      return;
    }
    if (account.state === 'disabled') {
      json(res, 403, failure('forbidden', 'this account is disabled'));
      return;
    }
    signIn(res, account);
    return;
  }

  // A24: registering makes a member account and signs it in.
  if (path === '/api/register' && method === 'POST') {
    const body = await readBody(req);
    const username = typeof body.username === 'string' ? body.username.toLowerCase() : '';
    const password = typeof body.password === 'string' ? body.password : '';
    if (!isRegistrationOpen()) {
      json(res, 403, failure('forbidden', 'registration is closed'));
      return;
    }
    if (!validUsername(username) || !validPassword(password)) {
      json(res, 400, failure('bad_request', 'username or password outside the rules'));
      return;
    }
    if (getAccount(username)) {
      json(res, 409, failure('conflict', 'that username is taken'));
      return;
    }
    signIn(res, createAccount(username, password, 'member'));
    return;
  }

  if (path === '/install.sh') {
    res.writeHead(200, { 'Content-Type': 'text/x-shellscript' });
    res.end('#!/bin/sh\necho "mock install script"\n');
    return;
  }

  // A23: the host's half of pairing by scanning. The host has no credential
  // yet, so both of these are unauthenticated. The status poll answers at once
  // rather than holding a connection for 25 s the way the gateway does; nothing
  // in the web app polls it, and a developer curling it wants an answer.
  if (path === '/api/pairing/requests' && method === 'POST') {
    const token = claimToken();
    const expires_at = Date.now() + 10 * 60_000;
    state.claims.set(token, { expires_at });
    json(res, 200, { token, expires_at, claim_url: `http://127.0.0.1:5173/pair#${token}` });
    return;
  }

  const claimStatus = /^\/api\/pairing\/requests\/([^/]+)$/.exec(path);
  if (claimStatus && method === 'GET') {
    const token = decodeURIComponent(claimStatus[1] ?? '');
    const lookup = lookupClaim(token);
    if (lookup.status !== 'live') {
      json(res, lookup.status === 'expired' ? 410 : 404, failure('not_found', 'no such request'));
      return;
    }
    if (lookup.request.code === undefined) {
      json(res, 200, { status: 'waiting' });
      return;
    }
    state.claims.delete(token);
    json(res, 200, {
      status: 'claimed',
      code: lookup.request.code,
      expires_at: lookup.request.expires_at,
    });
    return;
  }

  const account = caller(req);
  if (!account) {
    unauthorized(res);
    return;
  }

  const claim = /^\/api\/pairing\/requests\/([^/]+)\/claim$/.exec(path);
  if (claim && method === 'POST') {
    const token = decodeURIComponent(claim[1] ?? '');
    const lookup = lookupClaim(token);
    if (lookup.status !== 'live') {
      json(res, lookup.status === 'expired' ? 410 : 404, failure('not_found', 'no such request'));
      return;
    }
    if (lookup.request.code !== undefined) {
      json(res, 409, failure('conflict', 'this request was already claimed'));
      return;
    }
    const code = pairingCode();
    lookup.request.code = code;
    startPairing(code, account.username);
    json(res, 200, { code, expires_at: Date.now() + 10 * 60_000 });
    return;
  }

  if (path === '/api/logout' && method === 'POST') {
    for (const [token, username] of state.tokens) {
      if (username === account.username) state.tokens.delete(token);
    }
    json(res, 200, { ok: true }, { 'Set-Cookie': `${COOKIE}=; Path=/; Max-Age=0` });
    return;
  }

  if (path === '/api/session') {
    json(res, 200, { ok: true, user: userView(account), exp: Date.now() + 86_400_000 });
    return;
  }

  // A24: the caller's own password. `admin`'s is RC_PASSWORD, so it is refused.
  if (path === '/api/password' && method === 'POST') {
    const body = await readBody(req);
    if (account.role === 'admin') {
      json(res, 403, failure('forbidden', "the admin's password is RC_PASSWORD"));
      return;
    }
    if (body.current_password !== account.password) {
      json(res, 401, failure('unauthorized', 'wrong current password'));
      return;
    }
    const next = typeof body.new_password === 'string' ? body.new_password : '';
    if (!validPassword(next)) {
      json(res, 400, failure('bad_request', 'password outside the rules'));
      return;
    }
    account.password = next;
    json(res, 200, { ok: true });
    return;
  }

  if (path.startsWith('/api/users') || path === '/api/registration') {
    await handleAccountRoutes(req, res, account, path, method);
    return;
  }

  if (path === '/api/config') {
    json(res, 200, {
      public_origin: `http://127.0.0.1:5173`,
      // A44: the provider detects the language, so there is nothing to list.
      stt: { enabled: true, languages: ['auto'] },
      polish: { enabled: true },
      push: { web_enabled: true, apns_enabled: false },
      version: GATEWAY_VERSION,
      client: { version: CLIENT_VERSION, build: CLIENT_BUILD, url: '/dist/rc_client-latest.whl' },
    });
    return;
  }

  // A35 §3.2: the caller's own account preferences. A change goes out as
  // `preferences.updated` to every app socket of the account; a real gateway
  // also sends the `preferences` frame to its devices, which this mock has none
  // of.
  if (path === '/api/preferences' && method === 'GET') {
    json(res, 200, { preferences: preferencesOf(account.username) });
    return;
  }

  if (path === '/api/preferences' && method === 'PATCH') {
    const body = await readBody(req);
    const outcome = patchPreferences(preferencesOf(account.username), body);
    if ('error' in outcome) {
      json(res, 400, failure('bad_request', `${outcome.error} is not one of the values allowed`));
      return;
    }
    const next = outcome.preferences;
    state.preferences.set(account.username, next);
    broadcastTo(account.username, { type: 'preferences.updated', preferences: next });
    json(res, 200, { preferences: next });
    return;
  }

  // A29: the two models a configured provider would offer, and a polish that
  // does what the smallest useful model does — drop the fillers and the
  // stammered repeats, and start the sentence with a capital.
  if (path === '/api/polish/models' && method === 'GET') {
    json(res, 200, {
      models: [
        { id: 'gpt-4.1-mini', label: 'gpt-4.1-mini' },
        { id: 'gpt-4.1', label: 'gpt-4.1' },
      ],
    });
    return;
  }

  if (path === '/api/polish' && method === 'POST') {
    const body = await readBody(req);
    const text = typeof body.text === 'string' ? body.text : '';
    if (text.trim().length === 0) {
      json(res, 400, failure('bad_request', 'empty text'));
      return;
    }
    // Long enough to see "Polishing…" in the composer, short enough to wait for.
    setTimeout(() => json(res, 200, { text: fakePolish(text) }), POLISH_DELAY_MS);
    return;
  }

  if (path === '/api/devices' && method === 'GET') {
    json(res, 200, { devices: devicesOf(account.username) });
    return;
  }

  if (path.startsWith('/api/devices/pairing')) {
    if (method === 'POST') {
      const code = pairingCode();
      startPairing(code, account.username);
      json(res, 200, {
        code,
        expires_at: Date.now() + 10 * 60_000,
        install: {
          macos: `curl -fsSL http://127.0.0.1:5173/install.sh | sh -s -- \\\n  --pair ${code}`,
          linux: `curl -fsSL http://127.0.0.1:5173/install.sh | sh -s -- \\\n  --pair ${code}`,
        },
      });
      return;
    }
    if (method === 'DELETE') {
      const code = decodeURIComponent(path.split('/').pop() ?? '');
      state.pairings.get(code)?.timers.forEach(clearTimeout);
      state.pairings.delete(code);
      json(res, 200, { ok: true });
      return;
    }
  }

  const deviceMatch = /^\/api\/devices\/([^/]+)$/.exec(path);
  if (deviceMatch) {
    const deviceId = decodeURIComponent(deviceMatch[1] ?? '');
    const device = devicesOf(account.username).find((d) => d.device_id === deviceId);
    if (!device) {
      json(res, 404, { ok: false, error: { code: 'not_found', message: 'no such device' } });
      return;
    }
    if (method === 'PATCH') {
      const body = await readBody(req);
      device.name = String(body.name ?? device.name);
      broadcast({ type: 'device.updated', device });
      json(res, 200, { device });
      return;
    }
    if (method === 'DELETE') {
      state.devices = state.devices.filter((d) => d.device_id !== deviceId);
      state.sessions = state.sessions.filter((s) => s.device_id !== deviceId);
      broadcast({ type: 'device.removed', device_id: deviceId });
      state.deviceOwner.delete(deviceId);
      json(res, 200, { ok: true });
      return;
    }
  }

  if (path === '/api/sessions') {
    const deviceId = url.searchParams.get('device_id');
    const archived = url.searchParams.get('archived');
    json(res, 200, {
      sessions: sessionsOf(account.username)
        .filter((s) => (deviceId ? s.device_id === deviceId : true))
        .filter((s) => (archived === null ? true : String(s.archived) === archived)),
    });
    return;
  }

  if (path === '/api/push/web/vapid') {
    json(res, 200, { public_key: 'BJ' + 'x'.repeat(85) });
    return;
  }

  if (path === '/api/push/web/subscribe') {
    json(res, 200, { ok: true });
    return;
  }

  json(res, 404, { ok: false, error: { code: 'not_found', message: `no route for ${path}` } });
}

/* --------------------------------------------------------------- accounts */

/** A24 §3.9: the admin's account routes. Everyone else gets `403`. */
async function handleAccountRoutes(
  req: IncomingMessage,
  res: ServerResponse,
  account: Account,
  path: string,
  method: string,
): Promise<void> {
  if (account.role !== 'admin') {
    json(res, 403, failure('forbidden', 'admin only'));
    return;
  }

  if (path === '/api/registration' && method === 'PATCH') {
    const body = await readBody(req);
    setRegistrationOpen(body.open === true);
    json(res, 200, { open: isRegistrationOpen() });
    return;
  }

  if (path === '/api/users' && method === 'GET') {
    json(res, 200, {
      users: listAccounts().map((a) => recordView(a, deviceCount(a.username))),
      registration_open: isRegistrationOpen(),
    });
    return;
  }

  if (path === '/api/users' && method === 'POST') {
    const body = await readBody(req);
    const username = typeof body.username === 'string' ? body.username.toLowerCase() : '';
    const password = typeof body.password === 'string' ? body.password : '';
    const role: Role = body.role === 'admin' ? 'admin' : 'member';
    if (!validUsername(username) || !validPassword(password)) {
      json(res, 400, failure('bad_request', 'username or password outside the rules'));
      return;
    }
    if (getAccount(username)) {
      json(res, 409, failure('conflict', 'that username is taken'));
      return;
    }
    const created = createAccount(username, password, role);
    json(res, 200, { user: recordView(created, 0) });
    return;
  }

  const target = /^\/api\/users\/([^/]+)$/.exec(path);
  const subject = target ? getAccount(decodeURIComponent(target[1] ?? '')) : undefined;
  if (!target) {
    json(res, 404, failure('not_found', `no route for ${path}`));
    return;
  }
  if (!subject) {
    json(res, 404, failure('not_found', 'no such account'));
    return;
  }

  if (method === 'PATCH') {
    const body = await readBody(req);
    const demoting = body.role !== undefined && body.role !== 'admin';
    const disabling = body.state !== undefined && body.state !== 'active';
    if (subject.username === 'admin' && (demoting || disabling || body.password !== undefined)) {
      json(res, 409, failure('conflict', 'the admin account cannot be changed'));
      return;
    }
    if (body.password !== undefined) {
      if (typeof body.password !== 'string' || !validPassword(body.password)) {
        json(res, 400, failure('bad_request', 'password outside the rules'));
        return;
      }
      subject.password = body.password;
    }
    if (body.role === 'admin' || body.role === 'member') subject.role = body.role as Role;
    if (body.state === 'active' || body.state === 'disabled') {
      subject.state = body.state as State;
      // A24: disabling signs the account out everywhere.
      if (subject.state === 'disabled') signOutEverywhere(subject.username);
    }
    json(res, 200, { user: recordView(subject, deviceCount(subject.username)) });
    return;
  }

  if (method === 'DELETE') {
    if (subject.username === 'admin') {
      json(res, 409, failure('conflict', 'the admin account cannot be deleted'));
      return;
    }
    removeAccount(subject.username);
    json(res, 200, { ok: true });
    return;
  }

  json(res, 404, failure('not_found', `no route for ${path}`));
}

/** Closes every app socket of an account and drops its login tokens. */
function signOutEverywhere(username: string): void {
  for (const [token, owner] of state.tokens) {
    if (owner === username) state.tokens.delete(token);
  }
  for (const conn of conns) {
    if (conn.username === username) conn.socket.close(4401, 'account disabled');
  }
}

/** A24: deleting takes the devices, their sessions and the pairing codes too. */
function removeAccount(username: string): void {
  signOutEverywhere(username);
  for (const device of devicesOf(username)) {
    state.deviceOwner.delete(device.device_id);
    state.sessions = state.sessions.filter((s) => s.device_id !== device.device_id);
  }
  state.devices = state.devices.filter((d) => state.deviceOwner.has(d.device_id));
  for (const [code, owner] of state.pairingOwner) {
    if (owner !== username) continue;
    state.pairings.get(code)?.timers.forEach(clearTimeout);
    state.pairings.delete(code);
    state.pairingOwner.delete(code);
  }
  deleteAccount(username);
}

/* ---------------------------------------------------------------- ws/app */

const appWss = new WebSocketServer({ noServer: true });
const sttWss = new WebSocketServer({ noServer: true });

server.on('upgrade', (req, socket, head) => {
  const url = new URL(req.url ?? '/', `http://127.0.0.1:${PORT}`);
  if (url.pathname === '/ws/app') {
    const account = caller(req);
    if (!account) {
      socket.destroy();
      return;
    }
    appWss.handleUpgrade(req, socket, head, (ws) => onAppSocket(ws, account));
    return;
  }
  if (url.pathname === '/ws/stt') {
    sttWss.handleUpgrade(req, socket, head, (ws) => onSttSocket(ws));
    return;
  }
  socket.destroy();
});

function onAppSocket(socket: WebSocket, account: Account): void {
  const conn: AppConn = {
    socket,
    username: account.username,
    subscriptions: new Set(),
    terminals: new Set(),
  };
  conns.add(conn);

  send(socket, {
    type: 'hello',
    protocol: 1,
    gateway_version: GATEWAY_VERSION,
    user: userView(account),
    devices: devicesOf(account.username),
    sessions: sessionsOf(account.username),
    stt: { enabled: true, languages: ['auto'] },
    polish: { enabled: true },
    // A35: absent here would be a gateway too old for the switch, which is what
    // the app draws disabled; this one holds them.
    preferences: preferencesOf(account.username),
    server_time: Date.now(),
  });

  const ping = setInterval(() => send(socket, { type: 'ping' }), 25_000);

  socket.on('message', (raw) => {
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(String(raw)) as Record<string, unknown>;
    } catch {
      return;
    }
    handleAppFrame(conn, frame);
  });

  socket.on('close', () => {
    clearInterval(ping);
    conns.delete(conn);
    // A38: the gateway's `terminal.detach` — the shell keeps running, with its
    // scrollback, for ten minutes, and any socket of the account may attach.
    for (const terminalId of conn.terminals) detachTerminal(terminalId);
    conn.terminals.clear();
  });
}

const reply = (conn: AppConn, id: unknown, result: unknown): void =>
  send(conn.socket, { type: 'reply', id, ok: true, result });

const replyError = (conn: AppConn, id: unknown, code: string, message: string): void =>
  send(conn.socket, { type: 'reply', id, ok: false, error: { code, message } });

/** Answer a `session.send` and remember the answer, so a Retry repeats it. */
const replySend = (conn: AppConn, id: unknown, sessionId: string, result: unknown): void => {
  state.served.record(sessionId, id, result);
  reply(conn, id, result);
};

/* ------------------------------------------------------------- A38 terminals */

/** §7.3: four shells per device, and ten minutes for a detached one. */
const MAX_TERMINALS = 4;
const DETACH_MS = 10 * 60_000;

const promptFor = (device: Device): string =>
  device.platform === 'macos' ? `me@${device.name} ~ % ` : `ci@${device.name}:~$ `;

/** A column or row count the schema would accept, or null. */
function extent(value: unknown, max: number): number | null {
  if (typeof value !== 'number' || !Number.isInteger(value)) return null;
  return value >= 1 && value <= max ? value : null;
}

const sinkFor = (conn: AppConn, deviceId: string, terminalId: string) => ({
  output: (data: string, seq: number) =>
    send(conn.socket, {
      type: 'terminal.output',
      terminal_id: terminalId,
      device_id: deviceId,
      seq,
      data,
    }),
  exited: (code: number) => {
    send(conn.socket, {
      type: 'terminal.exited',
      terminal_id: terminalId,
      device_id: deviceId,
      code,
    });
    forgetTerminal(terminalId);
  },
});

function forgetTerminal(terminalId: string): void {
  const timer = state.detached.get(terminalId);
  if (timer) clearTimeout(timer);
  state.detached.delete(terminalId);
  state.terminals.delete(terminalId);
  for (const conn of conns) conn.terminals.delete(terminalId);
}

/** The gateway's `terminal.detach`: the shell lives on, with nobody watching. */
function detachTerminal(terminalId: string): void {
  const shell = state.terminals.get(terminalId);
  if (!shell) return;
  shell.detach();
  const timer = setTimeout(() => {
    shell.close(0);
    forgetTerminal(terminalId);
  }, DETACH_MS);
  timer.unref();
  state.detached.set(terminalId, timer);
}

/** The shell this request names, when the caller may have it. */
function terminalOf(conn: AppConn, frame: Record<string, unknown>): FakeShell | null {
  const shell = state.terminals.get(String(frame.terminal_id ?? ''));
  if (!shell || shell.deviceId !== String(frame.device_id ?? '')) return null;
  return conn.terminals.has(String(frame.terminal_id ?? '')) ? shell : null;
}

function handleAppFrame(conn: AppConn, frame: Record<string, unknown>): void {
  const id = frame.id;
  const type = String(frame.type ?? '');
  const sessionId = String(frame.session_id ?? '');
  const deviceId = String(frame.device_id ?? '');

  // A24: another account's session or device is answered exactly like one that
  // does not exist. A session or device this gateway has never heard of falls
  // through to the handler, which words its own `not_found`.
  const foreignSession = findSession(sessionId) !== undefined && !ownsSession(conn.username, sessionId);
  const foreignDevice =
    state.deviceOwner.has(deviceId) && state.deviceOwner.get(deviceId) !== conn.username;
  if (type !== 'pong' && (foreignSession || foreignDevice)) {
    return replyError(conn, id, 'not_found', 'no such session');
  }

  switch (type) {
    case 'pong':
      return;

    case 'session.subscribe': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      conn.subscriptions.add(sessionId);
      const sinceSeq = typeof frame.since_seq === 'number' ? frame.since_seq : undefined;
      const all = state.events.get(sessionId) ?? [];
      // §6.2: the buffer is bounded, and a cursor it no longer covers is told
      // to reload from history instead of being answered with a gap.
      const resync = needsResync(all, sinceSeq);
      const events = replayFor(all, sinceSeq);
      const pending = state.queues.get(sessionId);
      reply(conn, id, {
        session,
        events,
        resync,
        // Amendment A6: the latest queue snapshot, omitted when none was seen.
        ...(pending ? { queue: { pending } } : {}),
      });
      if (sessionId === 'ses-flaky' && (session.state === 'running' || session.state === 'starting')) {
        maybeStartDemoTurn();
      }
      return;
    }

    case 'session.unsubscribe':
      conn.subscriptions.delete(sessionId);
      return;

    // A47 §6.2: the person has the conversation in front of them. Handled
    // here, never by a device; idempotent, and every socket of the account
    // hears of it only when there was a mark to clear.
    case 'session.seen': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      const cleared = clearMark(session);
      reply(conn, id, {});
      if (cleared) broadcast({ type: 'session.updated', session });
      return;
    }

    case 'session.history': {
      const all = state.events.get(sessionId) ?? [];
      const beforeSeq = typeof frame.before_seq === 'number' ? frame.before_seq : Infinity;
      const limit = typeof frame.limit === 'number' ? Math.min(frame.limit, 1000) : 200;
      const final = historyEvents(all, beforeSeq);
      const page = final.slice(Math.max(0, final.length - limit));
      reply(conn, id, { events: page, has_more: page.length < final.length });
      return;
    }

    case 'session.block': {
      const blockId = String(frame.block_id ?? '');
      const all = state.events.get(sessionId) ?? [];
      const latest = [...all].reverse().find((e) => 'block_id' in e && e.block_id === blockId);
      if (!latest) return replyError(conn, id, 'not_found', 'no such block');
      const full: SessionEvent =
        latest.kind === 'tool_call'
          ? {
              ...latest,
              output: `${latest.output ?? ''}\n[full output — ${'-'.repeat(40)}]\n${Array.from({ length: 40 }, (_, i) => `line ${i + 1}`).join('\n')}`,
              output_truncated: false,
            }
          : latest;
      reply(conn, id, { event: full });
      return;
    }

    case 'session.create': {
      const deviceId = String(frame.device_id ?? '');
      const session: Session = {
        session_id: `ses-${randomUUID().slice(0, 8)}`,
        device_id: deviceId,
        agent: String(frame.agent ?? 'claude'),
        title: String(frame.title ?? frame.first_message ?? 'New session').slice(0, 60),
        cwd: String(frame.cwd ?? HOME[deviceId] ?? '/'),
        git: { branch: 'main', dirty: false, ahead: 0, behind: 0, worktree: Boolean(frame.worktree) },
        state: 'idle',
        state_detail: null,
        origin: 'remote',
        control: 'remote',
        model: (frame.model as string) ?? null,
        permission_mode: (frame.permission_mode as string) ?? null,
        effort: (frame.effort as string) ?? null,
        created_at: Date.now(),
        updated_at: Date.now(),
        last_seq: 0,
        archived: false,
        turn: null,
        todos: null,
        usage: null,
        queued: 0,
      };
      state.sessions.push(session);
      state.events.set(session.session_id, []);
      broadcast({ type: 'session.updated', session });
      reply(conn, id, { session });
      if (typeof frame.first_message === 'string' && frame.first_message.length > 0) {
        // No `session.send` behind it, so the device mints the block id here.
        setTimeout(() => {
          const sid = session.session_id;
          play(sid, turnScript(frame.first_message as string), () => drainQueue(sid));
        }, 400);
      }
      return;
    }

    case 'session.send': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      // §8 rule 7 and A12: a Retry reuses the request id, and a device that
      // already took that message answers it again without delivering it
      // twice. This is the whole reason a Retry is safe.
      const already = state.served.answerFor(sessionId, id);
      if (already !== undefined) return reply(conn, id, already);
      // A43: `queue_ts` puts an edited message back in the place it left, and
      // anything but a non-negative integer is refused before anything happens.
      const queueTs = readQueueTs(frame.queue_ts);
      if (queueTs === null) {
        return replyError(conn, id, 'bad_request', 'queue_ts must be a non-negative integer');
      }
      cancelResumeOnSend(sessionId);
      reviveSession(session);
      // A10 §6.3: a shared session accepts every send; the device decides
      // between injecting now and holding until the terminal turn ends.
      if (session.control === 'shared') return sharedSend(conn, id, session, frame, queueTs);
      if (session.control === 'terminal') {
        return replyError(conn, id, 'conflict', 'controlled by terminal; take over first');
      }
      const text = String(frame.text ?? '');
      const mode = String(frame.mode ?? 'auto');
      const running = session.state === 'running' || session.state === 'needs_approval';
      if (running && mode !== 'interrupt') {
        // A12: the queue entry keeps the request id all the way to its
        // `user_message`, so the app can correlate the row it already shows.
        // A43: it is held in `ts` order, with the number of its files.
        const queuedId = String(id);
        const queue = state.queues.get(sessionId) ?? [];
        const entry = {
          id: queuedId,
          text,
          ts: heldTs(queue, queueTs, Date.now()),
          ...filesField(frame.attachments),
        };
        const pending = hold(queue, entry);
        emit(sessionId, { seq: nextSeq(sessionId), ts: Date.now(), kind: 'queue', pending });
        replySend(conn, id, sessionId, { accepted: 'queued', queued_id: queuedId });
        return;
      }
      replySend(conn, id, sessionId, { accepted: 'sent' });
      playRemote(sessionId, text, String(id));
      return;
    }

    // A27: what the session's agent offers now. An agent without the
    // capability answers `unsupported` and the apps draw nothing; A40 gives
    // an attached Claude the one command its terminal can be typed.
    case 'session.commands': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      if (!agentFor(session)?.capabilities.includes('commands')) {
        return replyError(conn, id, 'unsupported', 'this agent has no slash commands');
      }
      reply(conn, id, { commands: commandsFor(session.agent) });
      return;
    }

    // A27: run one. The result is `{}`; the echo and the outcome are events,
    // the echo under this request's own id.
    case 'session.command': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      if (!agentFor(session)?.capabilities.includes('commands')) {
        return replyError(conn, id, 'unsupported', 'this agent has no slash commands');
      }
      if (session.control === 'terminal') {
        return replyError(conn, id, 'conflict', 'controlled by terminal; take over first');
      }
      const name = String(frame.name ?? '');
      if (!commandsFor(session.agent).some((command) => command.name === name)) {
        return replyError(conn, id, 'not_found', `no command named /${name}`);
      }
      // A40: on a session the device types into, a running turn is a busy
      // terminal rather than a busy agent, and the words say so.
      if (typedSession(session, agentFor(session))) {
        if (terminalBusy(session)) return replyError(conn, id, 'conflict', TERMINAL_BUSY);
      } else if (session.state === 'running' || session.state === 'needs_approval') {
        return replyError(conn, id, 'conflict', 'wait for the turn to finish');
      }
      const argument = typeof frame.argument === 'string' ? frame.argument : undefined;
      cancelResumeOnSend(sessionId);
      reply(conn, id, {});
      play(sessionId, commandScript(session.agent, name, argument, String(id)));
      return;
    }

    case 'session.stop': {
      const session = findSession(sessionId);
      // A10 §6.3: an attachment that carries no interrupt leaves Stop to the
      // terminal, whatever the agent's `interrupt` capability says.
      if (session?.control === 'shared' && agentFor(session)?.shared_interrupt !== true) {
        return replyError(conn, id, 'unsupported', 'stop it in the terminal');
      }
      // A42: on a terminal the device types into, Stop is a single Escape, and
      // the CLI would spend it on the prompt it has up rather than on the turn.
      // Idle, the Escape is not worth typing: the reply is `{}` and nothing
      // else happens, because `interruptTurn` has no turn to end.
      if (session && typedSession(session, agentFor(session)) && promptOnScreen(session)) {
        return replyError(conn, id, 'conflict', ANSWER_THE_PROMPT);
      }
      interruptTurn(sessionId);
      reply(conn, id, {});
      return;
    }

    case 'session.approve': {
      const requestId = String(frame.request_id ?? '');
      const optionId = String(frame.option_id ?? '');
      const session = findSession(sessionId);
      const shared = session?.control === 'shared';
      const all = state.events.get(sessionId) ?? [];
      const approval = [...all]
        .reverse()
        .find((e) => e.kind === 'approval' && e.request_id === requestId);
      // A11 clarification 2: the app may only send back an option the block
      // offered. A Claude relay offers Allow and Deny; the Codex daemon offers
      // whatever `availableDecisions` held.
      if (
        approval?.kind === 'approval' &&
        !approval.options.some((option) => option.id === optionId)
      ) {
        return replyError(conn, id, 'bad_request', 'that option was not offered');
      }
      if (approval && approval.kind === 'approval') {
        emit(sessionId, {
          ...approval,
          seq: nextSeq(sessionId),
          ts: Date.now(),
          status: 'resolved',
          decision: { option_id: optionId, by: 'remote' },
        });
      }
      reply(conn, id, {});
      if (sessionId === 'ses-flaky') play(sessionId, afterApproval());
      if (shared && session) {
        const allowed = optionId !== 'deny';
        const steps =
          session.agent === 'codex'
            ? codexSharedAfterApproval(
                sharedNonceOf(sessionId),
                sharedTurnIds.get(sessionId) ?? `shared-${sharedNonceOf(sessionId)}`,
                allowed,
              )
            : sharedAfterApproval(sharedNonceOf(sessionId), allowed);
        play(sessionId, steps, () => flushHeld(sessionId));
      }
      return;
    }

    case 'session.answer': {
      const requestId = String(frame.request_id ?? '');
      const all = state.events.get(sessionId) ?? [];
      const question = [...all]
        .reverse()
        .find((e) => e.kind === 'question' && e.request_id === requestId);
      if (question && question.kind === 'question') {
        // A20: the block resolves naming whoever answered it, and the session
        // stops waiting.
        emit(sessionId, {
          ...question,
          seq: nextSeq(sessionId),
          ts: Date.now(),
          status: 'resolved',
          answers: frame.answers as Record<string, string[] | string>,
          by: 'remote',
        });
        if (findSession(sessionId)?.state === 'needs_input') {
          emit(sessionId, { seq: nextSeq(sessionId), ts: Date.now(), kind: 'status', state: 'idle' });
        }
      }
      reply(conn, id, {});
      if (sessionId === 'ses-flaky') play(sessionId, afterAnswer());
      return;
    }

    case 'session.set': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      // A10/A11/A40 §6.3: on a shared session a setting is the device's only
      // when the attachment carries that very key; the rest stay the
      // terminal's, and an app that asks for one anyway is told so.
      if (lockedKeys(session, agentFor(session), frame).length > 0) {
        return replyError(conn, id, 'unsupported', 'change it in the terminal');
      }
      // A21: a tier the agent does not list is a bad request, and null is the
      // standard speed rather than a missing value.
      if ('speed' in frame) {
        const speed = frame.speed;
        const tiers = agentFor(session)?.speeds ?? [];
        if (speed !== null && !tiers.some((tier) => tier.id === speed)) {
          return replyError(conn, id, 'bad_request', 'unknown speed tier');
        }
      }
      // A40: a setting the device has to type lands when the terminal has
      // taken it, and only into a terminal that is idle. Nothing is queued.
      if (typedSession(session, agentFor(session)) && settingKeys(frame).length > 0) {
        if (terminalBusy(session)) return replyError(conn, id, 'conflict', TERMINAL_BUSY);
        setTimeout(() => applySet(conn, id, sessionId, frame), TYPING_MS);
        return;
      }
      applySet(conn, id, sessionId, frame);
      return;
    }

    case 'session.queue_remove': {
      const queuedId = String(frame.queued_id ?? '');
      const held = state.held.get(sessionId) ?? [];
      const queue = state.queues.get(sessionId) ?? [];
      // A43: what the device answers for a message it no longer holds — it
      // took it first — and why an edit then opens nothing.
      if (!held.some((h) => h.queued_id === queuedId) && !queue.some((q) => q.id === queuedId)) {
        return replyError(conn, id, 'not_found', 'that message is not queued');
      }
      state.held.set(sessionId, held.filter((h) => h.queued_id !== queuedId));
      const pending = queue.filter((q) => q.id !== queuedId);
      emit(sessionId, { seq: nextSeq(sessionId), ts: Date.now(), kind: 'queue', pending });
      reply(conn, id, {});
      return;
    }

    // A35 §6.3: the person moves the resume the device scheduled, or removes
    // it. Both answer with the session, and each step is a row of the timeline.
    case 'session.resume_set': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      if (session.turn !== null) return replyError(conn, id, 'conflict', 'a turn is running');
      if (session.control === 'terminal') {
        return replyError(conn, id, 'conflict', 'controlled by terminal');
      }
      const at = typeof frame.at === 'number' ? frame.at : 0;
      const now = Date.now();
      if (at < now + 60_000 || at > now + 8 * 24 * 3_600_000) {
        return replyError(conn, id, 'bad_request', 'at is outside the allowed window');
      }
      const moved = session.resume != null;
      session.resume = {
        at,
        estimated: false,
        attempts: session.resume?.attempts ?? 0,
        ...(session.resume?.window_minutes !== undefined
          ? { window_minutes: session.resume.window_minutes }
          : {}),
      };
      broadcast({ type: 'session.updated', session });
      emit(sessionId, {
        seq: nextSeq(sessionId),
        ts: now,
        kind: 'resume',
        status: moved ? 'rescheduled' : 'scheduled',
        at,
        estimated: false,
      });
      reply(conn, id, { session });
      return;
    }

    case 'session.resume_cancel': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      if (session.resume != null) {
        session.resume = null;
        broadcast({ type: 'session.updated', session });
        emit(sessionId, {
          seq: nextSeq(sessionId),
          ts: Date.now(),
          kind: 'resume',
          status: 'cancelled',
          reason: 'you cancelled it',
        });
      }
      reply(conn, id, { session });
      return;
    }

    case 'session.takeover': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      // A10 §6.3: there is nothing to take over — the device is already attached.
      if (session.control === 'shared') return replyError(conn, id, 'conflict', 'already attached');
      session.control = 'remote';
      // A47: a terminal turn the takeover ended is a turn that ended.
      moveState(session, 'idle');
      broadcast({ type: 'session.updated', session });
      // A10 §6.5: apps learn a new owner from `meta.control`; `status` follows
      // only because taking over also moves the session to `idle`.
      emit(sessionId, {
        seq: nextSeq(sessionId),
        ts: Date.now(),
        kind: 'meta',
        control: 'remote',
      });
      emit(sessionId, { seq: nextSeq(sessionId), ts: Date.now(), kind: 'status', state: 'idle' });
      emit(sessionId, {
        seq: nextSeq(sessionId),
        ts: Date.now(),
        kind: 'notice',
        level: 'info',
        text: 'You took over this session from the terminal.',
      });
      reply(conn, id, { session });
      return;
    }

    case 'session.archive': {
      const session = findSession(sessionId);
      if (!session) return replyError(conn, id, 'not_found', 'no such session');
      // A39: `archived: true` closes the session; `archived: false` only
      // clears the flag, which is what the device does with it.
      if (!frame.archived) {
        session.archived = false;
        broadcast({ type: 'session.updated', session });
        reply(conn, id, { session });
        return;
      }
      closeSession(session, () => reply(conn, id, { session }));
      return;
    }

    case 'session.delete': {
      state.sessions = state.sessions.filter((s) => s.session_id !== sessionId);
      const removed = findSession(sessionId);
      broadcast({
        type: 'session.removed',
        session_id: sessionId,
        device_id: removed?.device_id ?? '',
      });
      reply(conn, id, {});
      return;
    }

    case 'device.dirs': {
      const deviceId = String(frame.device_id ?? '');
      const home = HOME[deviceId] ?? '/home/user';
      const path = typeof frame.path === 'string' && frame.path.length > 0 ? frame.path : home;
      reply(conn, id, {
        path,
        parent: path === '/' ? null : path.slice(0, path.lastIndexOf('/')) || '/',
        entries: dirEntries(path, home),
        recent: recentDirs,
      });
      return;
    }

    // A37: one directory, inside one the device listed, answered with the new
    // directory's own listing so the picker can stand in it and choose it.
    case 'device.mkdir': {
      const deviceId = String(frame.device_id ?? '');
      const home = HOME[deviceId] ?? '/home/user';
      const made = makeDir(String(frame.path ?? ''), home, String(frame.name ?? ''));
      if ('error' in made) return replyError(conn, id, made.error.code, made.error.message);
      reply(conn, id, { ...made, recent: recentDirs });
      return;
    }

    case 'device.git': {
      const path = String(frame.path ?? '');
      const isRepo = path.includes('remote-control') || path.includes('work');
      reply(
        conn,
        id,
        isRepo
          ? { is_repo: true, branch: 'main', dirty: path.endsWith('web'), ahead: 0, behind: 0 }
          : { is_repo: false },
      );
      return;
    }

    // A33: the one frame that carries the quota windows. The device reads them
    // one network or daemon call at a time, so the reply lands a moment after
    // the page opens and its "Checking…" state is really seen.
    case 'device.agents': {
      const device = state.devices.find((d) => d.device_id === String(frame.device_id ?? ''));
      if (!device) return replyError(conn, id, 'not_found', 'no such device');
      if (!device.online) return replyError(conn, id, 'device_offline', 'the device is offline');
      const fresh = deviceAgents[device.device_id] ?? device.agents;
      setTimeout(() => reply(conn, id, { agents: fresh }), 900);
      return;
    }

    // A22: the device takes the update, restarts, and comes back on the new
    // build a few seconds later, which is what a real `hello` would report.
    case 'device.update': {
      const device = state.devices.find((d) => d.device_id === String(frame.device_id ?? ''));
      if (!device) return replyError(conn, id, 'not_found', 'no such device');
      if (!device.online) return replyError(conn, id, 'device_offline', 'the device is offline');
      const build = String(frame.build ?? '');
      if (device.client_build === build) {
        return replyError(conn, id, 'conflict', 'already on this build');
      }
      reply(conn, id, { accepted: true, from: device.client_build ?? null });
      device.update_state = 'updating';
      device.update_message = null;
      broadcast({ type: 'device.updated', device });
      setTimeout(() => {
        device.client_build = build;
        device.client_version = CLIENT_VERSION;
        device.update_state = 'idle';
        device.update_message = null;
        broadcast({ type: 'device.updated', device });
      }, 6_000);
      return;
    }

    // A38 §7.3: the person's login shell in a pseudo-terminal, streamed to the
    // one connection that asked for it and to nobody else.
    case 'terminal.open': {
      const device = state.devices.find((d) => d.device_id === deviceId);
      if (!device) return replyError(conn, id, 'not_found', 'no such device');
      if (!device.online) return replyError(conn, id, 'device_offline', 'the device is offline');
      if (device.terminal !== true) {
        return replyError(conn, id, 'unsupported', 'this device offers no terminal');
      }
      const cols = extent(frame.cols, 500);
      const rows = extent(frame.rows, 200);
      if (cols === null || rows === null) return replyError(conn, id, 'bad_request', 'bad size');
      const running = [...state.terminals.values()].filter(
        (shell) => shell.deviceId === deviceId && shell.running,
      );
      if (running.length >= MAX_TERMINALS) {
        return replyError(conn, id, 'conflict', 'this device already runs four terminals');
      }
      const terminalId = randomUUID();
      const shell = new FakeShell(deviceId, promptFor(device), cols, rows);
      state.terminals.set(terminalId, shell);
      conn.terminals.add(terminalId);
      shell.attach(sinkFor(conn, deviceId, terminalId));
      reply(conn, id, { terminal_id: terminalId });
      shell.greet();
      return;
    }

    case 'terminal.input': {
      const shell = terminalOf(conn, frame);
      if (!shell) return replyError(conn, id, 'not_found', 'no such terminal');
      const data = String(frame.data ?? '');
      if (Buffer.byteLength(data, 'base64') > 64 * 1024) {
        return replyError(conn, id, 'too_large', 'at most 64 KiB per write');
      }
      reply(conn, id, {});
      shell.input(data);
      return;
    }

    case 'terminal.resize': {
      const shell = terminalOf(conn, frame);
      if (!shell) return replyError(conn, id, 'not_found', 'no such terminal');
      const cols = extent(frame.cols, 500);
      const rows = extent(frame.rows, 200);
      if (cols === null || rows === null) return replyError(conn, id, 'bad_request', 'bad size');
      shell.resize(cols, rows);
      reply(conn, id, {});
      return;
    }

    // §7.3: output moves to this connection, with the screen it was left on.
    case 'terminal.attach': {
      const terminalId = String(frame.terminal_id ?? '');
      const shell = state.terminals.get(terminalId);
      if (!shell || shell.deviceId !== deviceId || !shell.running) {
        return replyError(conn, id, 'not_found', 'no such terminal');
      }
      const timer = state.detached.get(terminalId);
      if (timer) clearTimeout(timer);
      state.detached.delete(terminalId);
      for (const other of conns) other.terminals.delete(terminalId);
      conn.terminals.add(terminalId);
      shell.attach(sinkFor(conn, deviceId, terminalId));
      reply(conn, id, {
        terminal_id: terminalId,
        cols: shell.cols,
        rows: shell.rows,
        scrollback: shell.scrollback,
      });
      return;
    }

    // §7.3: ends the shell, and says so again when it was already gone.
    case 'terminal.close': {
      const shell = state.terminals.get(String(frame.terminal_id ?? ''));
      reply(conn, id, {});
      shell?.close(0);
      return;
    }

    default:
      replyError(conn, id, 'unsupported', `mock has no handler for ${type}`);
  }
}

/**
 * PROTOCOL-FROZEN §8: ascending by `seq`, the latest event per block plus the
 * turn markers, notices, errors and the latest `todos` snapshot. Never streaming
 * deltas, never `status` / `meta` / `queue`.
 */
function historyEvents(all: readonly SessionEvent[], beforeSeq: number): SessionEvent[] {
  const candidates = all
    .filter((e) => e.seq < beforeSeq)
    .filter((e) => !['status', 'meta', 'queue'].includes(e.kind))
    .filter((e) => !('delta' in e && e.delta !== undefined));

  const latestByBlock = new Map<string, SessionEvent>();
  let latestTodos: SessionEvent | undefined;
  const rest: SessionEvent[] = [];
  for (const event of candidates) {
    const blockId = blockIdOf(event);
    if (blockId) latestByBlock.set(blockId, event);
    else if (event.kind === 'todos') latestTodos = event;
    else rest.push(event);
  }

  const out = [...latestByBlock.values(), ...rest];
  if (latestTodos) out.push(latestTodos);
  return out.sort((a, b) => a.seq - b.seq);
}

/* ----------------------------------------------- A40 settings and commands */

/**
 * Apply what `session.set` asked for, answer with the session, and publish the
 * change as `meta` (5.11) so a second app on it sees the change without
 * asking. For a typed session this runs once the terminal has taken it.
 */
function applySet(
  conn: AppConn,
  id: unknown,
  sessionId: string,
  frame: Record<string, unknown>,
): void {
  const session = findSession(sessionId);
  if (!session) return replyError(conn, id, 'not_found', 'no such session');
  const setsSpeed = 'speed' in frame;
  if (setsSpeed) session.speed = frame.speed as string | null;
  if (typeof frame.model === 'string') session.model = frame.model;
  if (typeof frame.permission_mode === 'string') session.permission_mode = frame.permission_mode;
  if (typeof frame.effort === 'string') session.effort = frame.effort;
  if (typeof frame.title === 'string') session.title = frame.title;
  session.updated_at = Date.now();
  broadcast({ type: 'session.updated', session });
  reply(conn, id, { session });
  emit(sessionId, {
    seq: nextSeq(sessionId),
    ts: Date.now(),
    kind: 'meta',
    ...(typeof frame.model === 'string' ? { model: frame.model } : {}),
    ...(typeof frame.permission_mode === 'string'
      ? { permission_mode: frame.permission_mode }
      : {}),
    ...(typeof frame.effort === 'string' ? { effort: frame.effort } : {}),
    ...(setsSpeed ? { speed: session.speed ?? null } : {}),
  });
}

/* --------------------------------------------------- A10/A11 shared sessions */

const sharedNonces = new Map<string, string>();
const sharedTurnIds = new Map<string, string>();
const sharedApprovalShown = new Set<string>();

const sharedNonceOf = (sessionId: string): string => sharedNonces.get(sessionId) ?? '0';

/**
 * `session.send` on a session the device is attached to (A10 §6.3, A11
 * clarification 1). Idle injects at once. On a running turn, `auto` steers
 * when the agent can steer, `interrupt` ends the turn and starts a new one,
 * and anything else is held until the terminal turn finishes.
 */
function sharedSend(
  conn: AppConn,
  id: unknown,
  session: Session,
  frame: Record<string, unknown>,
  queueTs: number | undefined,
): void {
  const sessionId = session.session_id;
  const agent = agentFor(session);
  const attachments = Array.isArray(frame.attachments) ? frame.attachments : [];
  // A11 §4.2: the daemon takes image inputs; the Claude channel does not.
  if (attachments.length > 0 && agent?.shared_attachments !== true) {
    return replyError(conn, id, 'unsupported', 'attachments cannot be delivered to a terminal session');
  }
  const text = String(frame.text ?? '');
  const mode = String(frame.mode ?? 'auto');
  // A12: the request id is the block id the app already rendered under.
  const blockId = String(id);
  const busy =
    session.state === 'running' ||
    session.state === 'needs_approval' ||
    session.state === 'needs_input';

  if (!busy) {
    replySend(conn, id, sessionId, { accepted: 'sent' });
    afterEcho(() => {
      emitUserMessage(sessionId, blockId, text);
      playShared(sessionId, text, false);
    });
    return;
  }

  // A11 clarification 1: `auto` on a running thread steers when the agent can.
  if (mode === 'auto' && agent?.capabilities.includes('steer')) {
    replySend(conn, id, sessionId, { accepted: 'steered' });
    afterEcho(() => {
      emitUserMessage(sessionId, blockId, text);
      playShared(sessionId, text, true);
    });
    return;
  }

  // A11 clarification 1: `interrupt` needs the same attachment as `session.stop`.
  if (mode === 'interrupt') {
    if (agent?.shared_interrupt !== true) {
      return replyError(conn, id, 'unsupported', 'stop it in the terminal');
    }
    interruptTurn(sessionId);
    replySend(conn, id, sessionId, { accepted: 'sent' });
    afterEcho(() => {
      emitUserMessage(sessionId, blockId, text);
      playShared(sessionId, text, false);
    });
    return;
  }

  const queuedId = String(id);
  // A43: held in `ts` order, the order the CLI will be given them in.
  const held = state.held.get(sessionId) ?? [];
  const ts = heldTs(held, queueTs, Date.now());
  state.held.set(sessionId, hold(held, { block_id: blockId, text, queued_id: queuedId, ts }));
  replySend(conn, id, sessionId, { accepted: 'queued', queued_id: queuedId });
  // A19: a held message is a queue entry and nothing else. Its block appears
  // when the CLI takes it, after the output of the turn it waited for.
  afterEcho(() => {
    const entry = { id: queuedId, text, ts, ...filesField(frame.attachments) };
    const pending = hold(state.queues.get(sessionId) ?? [], entry);
    emit(sessionId, { seq: nextSeq(sessionId), ts: Date.now(), kind: 'queue', pending });
  });
}

/** What a device costs to answer: the reply is instant, the echo is not. */
function afterEcho(run: () => void): void {
  setTimeout(run, ECHO_DELAY_MS);
}

/** A19: a message an app sent into a shared session, once the CLI took it. */
function emitUserMessage(sessionId: string, blockId: string, text: string): void {
  emit(sessionId, {
    seq: nextSeq(sessionId),
    ts: Date.now(),
    kind: 'user_message',
    block_id: blockId,
    source: 'remote',
    text,
    delivery: 'delivered',
  });
}

function playShared(sessionId: string, text: string, steered: boolean): void {
  const session = findSession(sessionId);
  const nonce = String(Date.now()).slice(-6);
  sharedNonces.set(sessionId, nonce);
  const withApproval = !sharedApprovalShown.has(sessionId);
  sharedApprovalShown.add(sessionId);
  // Steering joins the live turn; anything else opens a new one.
  const turnId = (steered ? session?.turn?.turn_id : null) ?? `shared-${nonce}`;
  sharedTurnIds.set(sessionId, turnId);
  const steps =
    session?.agent === 'codex'
      ? codexSharedTurn(text, nonce, { turnId, steered, withApproval })
      : sharedTurn(text, nonce, withApproval);
  play(sessionId, steps, withApproval ? undefined : () => flushHeld(sessionId));
}

/**
 * The terminal turn ended: inject everything held, replacing each block with
 * `delivery: "delivered"` under its original `block_id`.
 */
function flushHeld(sessionId: string): void {
  const items = state.held.get(sessionId) ?? [];
  if (items.length === 0) return;
  state.held.set(sessionId, []);
  for (const item of items) emitUserMessage(sessionId, item.block_id, item.text);
  emit(sessionId, { seq: nextSeq(sessionId), ts: Date.now(), kind: 'queue', pending: [] });
  const last = items[items.length - 1];
  if (last) setTimeout(() => playShared(sessionId, last.text, false), 500);
}

let demoStarted = false;

function maybeStartDemoTurn(): void {
  if (demoStarted) return;
  demoStarted = true;
  const prompt = 'test_refresh_flow fails ~1 in 5 on CI, never locally. Find the race and fix it.';
  // A device-minted block id, the way a pre-A12 device or a terminal sends one.
  setTimeout(() => play('ses-flaky', turnScript(prompt), () => drainQueue('ses-flaky')), 600);
}

/* ---------------------------------------------------------------- ws/stt */

const PARTIALS = [
  'also add a retry',
  'also add a retry to the token refresh path',
  'also add a retry to the token refresh path and re-run the suite',
];

/**
 * A44: the provider detects the language, so the socket reads no query at all;
 * a `language` an older app still sends is ignored, as the gateway ignores it.
 */
function onSttSocket(socket: WebSocket): void {
  let index = 0;
  let receivedAudio = false;
  const timer = setInterval(() => {
    if (!receivedAudio) return;
    const text = PARTIALS[Math.min(index, PARTIALS.length - 1)];
    index += 1;
    send(socket, { type: 'stt.partial', text });
  }, 2_000);

  socket.on('message', (raw, isBinary) => {
    if (isBinary) {
      receivedAudio = true;
      return;
    }
    let frame: { type?: string };
    try {
      frame = JSON.parse(String(raw)) as { type?: string };
    } catch {
      return;
    }
    if (frame.type === 'stt.stop') {
      send(socket, {
        // A29: spoken the way speech arrives — a filler and two stammers — so
        // what the polish model does to it can be seen in the composer.
        type: 'stt.final',
        text: 'um so also add a retry to the the token refresh path and re-run the suite on on the CI runner too',
        language: 'en',
      });
      clearInterval(timer);
      setTimeout(() => socket.close(), 50);
    }
    if (frame.type === 'stt.cancel') {
      clearInterval(timer);
      socket.close();
    }
  });

  socket.on('close', () => clearInterval(timer));
}

server.listen(PORT, '127.0.0.1', () => {
  console.log(`mock gateway listening on http://127.0.0.1:${PORT}`);
  console.log(`  admin / ${PASSWORD} — two devices, five sessions, the Users screen`);
  console.log(`  ${MEMBER_USERNAME} / ${MEMBER_PASSWORD} — a member: no devices, no Users screen`);
});
