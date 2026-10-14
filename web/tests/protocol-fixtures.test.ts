/**
 * Decodes every canonical fixture from `protocol/fixtures/` and asserts the
 * shapes this app depends on. A structural failure here means the web client
 * and the frozen contract have drifted apart.
 */
import { describe, expect, it } from 'vitest';
import { PROTOCOL_VERSION } from '../src/protocol/types';
import { readPreferences, unsetKeys } from '../src/stores/preferenceFields';
import { countUnseen, keyOf } from '../src/stores/sessions';
import type {
  AgentInfo,
  Command,
  Device,
  Preferences,
  Session,
  SessionEvent,
  Usage,
} from '../src/protocol/types';
import type {
  PolishModelsResponse,
  PolishRequest,
  PolishResponse,
  User,
  UserRecord,
} from '../src/protocol/types';
import type { PreferencesPatch, PreferencesResponse } from '../src/lib/api';
import type {
  HelloFrame,
  PushFrame,
  Reply,
  RequestParams,
  SessionResult,
  SubscribeResult,
  TerminalAttachResult,
  TerminalOpenResult,
} from '../src/protocol/frames';
import { resumeRowText } from '../src/features/chat/resume';
import { en } from '../src/strings';
import { toolCategory } from '../src/features/chat/blocks/toolCategory';
import {
  commandSections,
  filterCommands,
  matchCommand,
} from '../src/features/chat/commands';
import { CONTEXT_LIMIT, CONTEXT_TEXT_LIMIT } from '../src/features/voice/polish';
import { base64Size } from '../src/lib/base64';
import { fixturesAvailable, listFixtures, readFixture } from './fixtures';

const EVENT_KINDS = new Set([
  'user_message',
  'assistant_text',
  'thinking',
  'tool_call',
  'todos',
  'approval',
  'question',
  'turn_started',
  'turn_completed',
  'status',
  'meta',
  'queue',
  'notice',
  'error',
  // A35: what the device did about a session the usage limit stopped.
  'resume',
]);

/** A35 (5.15): the five steps a `resume` event reports. */
const RESUME_STATUSES = new Set(['scheduled', 'rescheduled', 'fired', 'cancelled', 'dropped']);

/**
 * §3.7, with the three A35 kinds the service worker also renders, and A47's
 * `badge`, which changes the app icon's count and shows nothing.
 */
const PUSH_KINDS = new Set([
  'needs_approval',
  'needs_input',
  'turn_completed',
  'error',
  'limit_reached',
  'resumed',
  'resume_dropped',
  'badge',
]);

const TOOL_KINDS = new Set([
  'shell',
  'read',
  'edit',
  'write',
  'search',
  'web',
  'mcp',
  'subagent',
  'todo',
  'other',
]);

/** The one `Trigger` of `schema/events.json`, behind both `source` and `trigger`. */
const TRIGGERS = new Set(['remote', 'terminal', 'queue', 'agent', 'resume']);

/** A27: `Command.name` of PROTOCOL.md §4.11 — lower case, never with the slash. */
const COMMAND_NAME = /^[a-z0-9][a-z0-9_:.-]*$/;

/** A24: the username rule of PROTOCOL.md §3.1, and the two closed lists. */
const USERNAME = /^[a-z0-9][a-z0-9._-]{2,31}$/;
const ROLES = new Set(['admin', 'member']);
const STATES = new Set(['active', 'disabled']);

function assertUser(user: User): void {
  expect(user.username).toMatch(USERNAME);
  expect(ROLES.has(user.role)).toBe(true);
}

function assertUserRecord(record: UserRecord): void {
  assertUser(record);
  expect(STATES.has(record.state)).toBe(true);
  expect(typeof record.created_at).toBe('number');
  expect(record.last_login_at === null || typeof record.last_login_at === 'number').toBe(true);
  expect(record.devices).toBeGreaterThanOrEqual(0);
}

function assertUsage(usage: Usage): void {
  // Amendment A2: only the three token counts are required.
  expect(typeof usage.input_tokens).toBe('number');
  expect(typeof usage.output_tokens).toBe('number');
  expect(typeof usage.total_tokens).toBe('number');
  for (const key of ['context_used', 'context_window', 'cost_usd'] as const) {
    const value = usage[key];
    expect(value === undefined || value === null || typeof value === 'number').toBe(true);
  }
}

function assertAgent(agent: AgentInfo): void {
  expect(typeof agent.agent).toBe('string');
  expect(typeof agent.available).toBe('boolean');
  expect(Array.isArray(agent.models)).toBe(true);
  expect(Array.isArray(agent.permission_modes)).toBe(true);
  expect(Array.isArray(agent.efforts)).toBe(true);
  expect(Array.isArray(agent.capabilities)).toBe(true);
  // A21: speed tiers are optional, and a list of the same labelled ids.
  expect(agent.speeds === undefined || Array.isArray(agent.speeds)).toBe(true);
  for (const choice of [
    ...agent.models,
    ...agent.permission_modes,
    ...agent.efforts,
    ...(agent.speeds ?? []),
  ]) {
    expect(typeof choice.id).toBe('string');
    expect(typeof choice.label).toBe('string');
  }
  // A10/A11: the three attachment booleans are optional and never anything else.
  for (const key of ['shared_interrupt', 'shared_settings', 'shared_attachments'] as const) {
    const value = agent[key];
    expect(value === undefined || typeof value === 'boolean').toBe(true);
  }
  // A40 §4.2: the subset is one of the four settings names, it never appears
  // without `shared_settings`, and it is never the empty list — an agent that
  // carries no setting says so with the boolean.
  const keys = agent.shared_settings_keys;
  if (keys !== undefined) {
    expect(agent.shared_settings).toBe(true);
    expect(keys.length).toBeGreaterThan(0);
    for (const key of keys) {
      expect(['model', 'permission_mode', 'effort', 'speed']).toContain(key);
    }
  }
}

function assertDevice(device: Device): void {
  expect(typeof device.device_id).toBe('string');
  expect(['macos', 'linux']).toContain(device.platform);
  expect(typeof device.online).toBe('boolean');
  expect(device.latency_ms === null || typeof device.latency_ms === 'number').toBe(true);
  device.agents.forEach(assertAgent);
}

function assertSession(session: Session): void {
  expect(typeof session.session_id).toBe('string');
  expect(typeof session.device_id).toBe('string');
  expect([
    'starting',
    'idle',
    'running',
    'needs_approval',
    'needs_input',
    'error',
    'stopped',
    'readonly',
  ]).toContain(session.state);
  // Amendment A10 adds `shared`: attached to a live terminal session.
  expect(['remote', 'terminal', 'shared', 'none']).toContain(session.control);
  expect(['remote', 'terminal']).toContain(session.origin);
  expect(typeof session.last_seq).toBe('number');
  // A21: absent or null is the standard speed.
  expect(
    session.speed === undefined || session.speed === null || typeof session.speed === 'string',
  ).toBe(true);
  if (session.usage) assertUsage(session.usage);
  if (session.turn) expect(typeof session.turn.turn_id).toBe('string');
  // A35: the one resume a session can have pending, or null/absent for none.
  if (session.resume) {
    expect(typeof session.resume.at).toBe('number');
    expect(typeof session.resume.estimated).toBe('boolean');
    expect(session.resume.attempts).toBeGreaterThanOrEqual(0);
  }
  // A47: the gateway's mark, a boolean when present; absent is false.
  expect(session.unseen === undefined || typeof session.unseen === 'boolean').toBe(true);
}

function assertEvent(event: SessionEvent): void {
  expect(EVENT_KINDS).toContain(event.kind);
  expect(typeof event.seq).toBe('number');
  expect(typeof event.ts).toBe('number');

  switch (event.kind) {
    case 'tool_call':
      expect(typeof event.tool).toBe('string');
      expect(typeof event.title).toBe('string');
      expect(['running', 'succeeded', 'failed', 'cancelled']).toContain(event.status);
      // Amendment A1: the category lives in `tool_kind`, never in `kind`.
      expect(event.tool_kind).toBeDefined();
      expect(TOOL_KINDS).toContain(event.tool_kind);
      expect(toolCategory(event.tool, event.tool_kind)).toBe(event.tool_kind);
      if (event.diff) {
        expect(typeof event.diff.path).toBe('string');
        expect(typeof event.diff.additions).toBe('number');
        expect(typeof event.diff.deletions).toBe('number');
      }
      break;
    case 'approval':
      expect(event.options.length).toBeGreaterThan(0);
      expect(event.options.some((o) => o.style === 'primary')).toBe(true);
      expect(event.options.some((o) => o.style === 'danger')).toBe(true);
      expect(['pending', 'resolved', 'expired']).toContain(event.status);
      break;
    case 'question':
      expect(event.questions.length).toBeGreaterThan(0);
      expect(['pending', 'resolved', 'expired']).toContain(event.status);
      for (const question of event.questions) {
        expect(typeof question.multi).toBe('boolean');
        expect(typeof question.allow_text).toBe('boolean');
      }
      // Amendment A20: a resolved question may name who answered it, and only
      // the two sides that can — never a policy.
      if (event.by !== undefined) {
        expect(['remote', 'terminal']).toContain(event.by);
        expect(event.status).not.toBe('pending');
      }
      break;
    case 'user_message':
      // A30 added `agent`: words the CLI filed as a user turn that no person
      // typed, which the app draws muted and captioned, never as the person's.
      expect(TRIGGERS).toContain(event.source);
      // A10: delivery is present on shared sessions only. A19 took `pending`
      // away: a message the device still holds is a queue entry, not a block.
      if (event.delivery !== undefined) {
        expect(['delivered', 'absorbed']).toContain(event.delivery);
      }
      // Amendment A3: inbound attachments describe size, never bytes.
      for (const attachment of event.attachments ?? []) {
        expect(typeof attachment.size).toBe('number');
        expect(attachment).not.toHaveProperty('data_base64');
      }
      break;
    case 'assistant_text':
    case 'thinking':
      expect(typeof event.done).toBe('boolean');
      expect(event.delta !== undefined || event.text !== undefined).toBe(true);
      break;
    case 'turn_started':
      expect(TRIGGERS).toContain(event.trigger);
      break;
    case 'turn_completed':
      expect(['completed', 'interrupted', 'error']).toContain(event.stop_reason);
      if (event.usage) assertUsage(event.usage);
      // A35: a turn the vendor's usage limit ended is an `error` stop that says
      // which window was hit and when it resets, or that it named no time.
      if (event.limit) {
        expect(event.stop_reason).toBe('error');
        expect(
          event.limit.resets_at === null || typeof event.limit.resets_at === 'number',
        ).toBe(true);
        if (event.limit.window_minutes !== undefined) {
          expect(event.limit.window_minutes).toBeGreaterThan(0);
        }
      }
      break;
    case 'resume':
      expect(RESUME_STATUSES).toContain(event.status);
      // `at` belongs to the two steps that name a time; `reason` to the two
      // that say why nothing will happen.
      if (event.status === 'scheduled' || event.status === 'rescheduled') {
        expect(typeof event.at).toBe('number');
      }
      if (event.reason !== undefined) expect(event.reason.length).toBeGreaterThan(0);
      break;
    case 'todos':
      for (const todo of event.items) {
        expect(['pending', 'in_progress', 'completed']).toContain(todo.status);
      }
      break;
    case 'queue':
      // A43: in delivery order, which is `ts` order with no two alike, and a
      // count of files only on an entry that holds some.
      event.pending.forEach((entry, index) => {
        expect(typeof entry.text).toBe('string');
        if (index > 0) expect(entry.ts).toBeGreaterThan(event.pending[index - 1]!.ts);
        if (entry.attachments !== undefined) {
          expect(Number.isInteger(entry.attachments)).toBe(true);
          expect(entry.attachments).toBeGreaterThanOrEqual(1);
        }
      });
      break;
    default:
      break;
  }
}

describe.runIf(fixturesAvailable())('protocol fixtures', () => {
  it('parses every fixture file as JSON', () => {
    const files = listFixtures();
    expect(files.length).toBeGreaterThan(40);
    for (const file of files) {
      expect(() => readFixture(file.name), file.name).not.toThrow();
    }
  });

  it('decodes the hello frame into devices and sessions', () => {
    const hello = readFixture<HelloFrame>('app/hello.json');
    expect(hello.type).toBe('hello');
    expect(hello.protocol).toBe(PROTOCOL_VERSION);
    expect(typeof hello.gateway_version).toBe('string');
    expect(typeof hello.user.username).toBe('string');
    expect(typeof hello.stt.enabled).toBe('boolean');
    expect(hello.stt.languages).toEqual(['auto']);
    hello.devices.forEach(assertDevice);
    hello.sessions.forEach(assertSession);
  });

  it('decodes every event fixture', () => {
    const files = listFixtures('events');
    expect(files.length).toBeGreaterThan(20);
    for (const file of files) {
      assertEvent(readFixture<SessionEvent>(file.name));
    }
  });

  it('decodes the message another agent put into the conversation (A30)', () => {
    const event = readFixture<SessionEvent>('events/user_message.agent.json');
    if (event.kind !== 'user_message') throw new Error('not a user_message fixture');
    expect(event.source).toBe('agent');
    // The device sends what a reader wants — who reported and what they said —
    // so neither the envelope nor a system reminder ever reaches a bubble.
    expect(event.text).not.toContain('<teammate-message');
    expect(event.text).not.toContain('<task-notification');
    expect(event.text).not.toContain('<system-reminder');
    expect(event.text.trim().length).toBeGreaterThan(0);
  });

  it('decodes the standalone object fixtures', () => {
    // Amendment A10 added `objects/`: sessions and agents on their own.
    const files = listFixtures('objects');
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const object = readFixture<Record<string, unknown>>(file.name);
      if ('session_id' in object) assertSession(object as unknown as Session);
      else if ('agent' in object) assertAgent(object as unknown as AgentInfo);
      else throw new Error(`unknown object fixture ${file.name}`);
    }
  });

  it('decodes the subscribe reply, including the replay tail', () => {
    const reply = readFixture<Reply<SubscribeResult>>('replay/subscribe.reply.json');
    expect(reply.ok).toBe(true);
    if (!reply.ok) return;
    assertSession(reply.result.session);
    expect(typeof reply.result.resync).toBe('boolean');
    reply.result.events.forEach(assertEvent);
  });

  it('decodes the outbound send frame with base64 attachments', () => {
    const send = readFixture<{
      type: string;
      attachments?: { name: string; mime: string; data_base64: string }[];
      mode: string;
    }>('app/session.send.json');
    expect(send.type).toBe('session.send');
    expect(['auto', 'queue', 'interrupt']).toContain(send.mode);
    for (const attachment of send.attachments ?? []) {
      expect(typeof attachment.data_base64).toBe('string');
      expect(attachment).not.toHaveProperty('size');
    }
  });

  it('decodes an edited queued message going back to its place (A43)', () => {
    const queue = readFixture<SessionEvent>('events/queue.json');
    const withFiles = readFixture<SessionEvent>('events/queue.attachments.json');
    if (queue.kind !== 'queue' || withFiles.kind !== 'queue') throw new Error('not a queue');
    // The count rides on the entry that holds files, and on no other.
    expect(withFiles.pending.map((entry) => entry.attachments)).toEqual([undefined, 2]);

    const requeue = readFixture<RequestParams<'session.send'> & { type: string; id: string }>(
      'app/session.send.requeue.json',
    );
    expect(requeue.type).toBe('session.send');
    expect(requeue.mode).toBe('queue');
    // The first entry of the snapshot, sent back under its own ts.
    expect(requeue.queue_ts).toBe(queue.pending[0]?.ts);
    expect(requeue).not.toHaveProperty('attachments');
  });

  it('decodes the agent list and the speed tier it advertises (A21)', () => {
    const reply = readFixture<Reply<{ agents: AgentInfo[] }>>('app/reply.device.agents.json');
    expect(reply.ok).toBe(true);
    if (!reply.ok) return;
    reply.result.agents.forEach(assertAgent);

    const codex = reply.result.agents.find((agent) => agent.agent === 'codex');
    expect(codex?.speeds).toEqual([{ id: 'priority', label: 'Fast' }]);
    // Claude has no faster tier, so the apps draw no control for it.
    expect(reply.result.agents.find((agent) => agent.agent === 'claude')?.speeds).toBeUndefined();

    const set = readFixture<{ type: string; session_id: string; speed?: string | null }>(
      'app/session.set.json',
    );
    expect(set.type).toBe('session.set');
    expect(codex?.speeds?.some((tier) => tier.id === set.speed)).toBe(true);
  });

  it('decodes the slash-command frames of A27', () => {
    const ask = readFixture<{ type: string; session_id: string }>('app/session.commands.json');
    expect(ask.type).toBe('session.commands');
    expect(typeof ask.session_id).toBe('string');

    const reply = readFixture<Reply<{ commands: Command[] }>>('app/reply.session.commands.json');
    expect(reply.ok).toBe(true);
    if (!reply.ok) return;
    expect(reply.result.commands.length).toBeGreaterThan(0);
    for (const command of reply.result.commands) {
      // §4.11: the name is what the user types after the slash, never with it.
      expect(command.name).toMatch(COMMAND_NAME);
      expect(typeof command.description).toBe('string');
      expect(command.argument === undefined || command.argument.length > 0).toBe(true);
      expect(command.group === undefined || command.group.length > 0).toBe(true);
    }
    // The worked list carries more than one group, which is what sections it.
    expect(commandSections(reply.result.commands).length).toBeGreaterThan(1);
    expect(filterCommands(reply.result.commands, 'comp').map((c) => c.name)).toEqual(['compact']);

    const run = readFixture<{ type: string; session_id: string; name: string; argument?: string }>(
      'app/session.command.json',
    );
    expect(run.type).toBe('session.command');
    expect(run.name).toMatch(COMMAND_NAME);
    // The name goes back exactly as it was listed.
    expect(reply.result.commands.some((command) => command.name === run.name)).toBe(true);
    expect(matchCommand(reply.result.commands, `/${run.name} ${run.argument ?? ''}`)).toMatchObject({
      command: { name: run.name },
      ...(run.argument ? { argument: run.argument } : {}),
    });

    // What a terminal would have printed comes back as a `tool_call` block.
    const printed = readFixture<SessionEvent>('events/tool_call.command.json');
    if (printed.kind !== 'tool_call') throw new Error('not a tool_call fixture');
    expect(printed.tool_kind).toBe('other');
    expect(printed.tool).toBe(printed.title);
    expect(printed.tool.startsWith('/')).toBe(true);
  });

  it('decodes an error reply into a code the UI knows', () => {
    const reply = readFixture<Reply>('app/reply.error.json');
    expect(reply.ok).toBe(false);
    if (reply.ok) return;
    expect(typeof reply.error.code).toBe('string');
    expect(typeof reply.error.message).toBe('string');
  });

  it('decodes the HTTP responses the app calls on boot', () => {
    const health = readFixture<{ ok: boolean; protocol: number; version: string }>(
      'http/health.response.json',
    );
    expect(health.protocol).toBe(PROTOCOL_VERSION);

    const config = readFixture<{
      public_origin: string;
      stt: { enabled: boolean; languages: string[] };
      push: { web_enabled: boolean; apns_enabled: boolean };
      client?: { version: string; build: string; url: string };
    }>('http/config.response.json');
    expect(typeof config.public_origin).toBe('string');
    // A44: the gateway's provider detects the language, so there is no list.
    expect(config.stt.languages).toEqual(['auto']);
    expect(typeof config.push.web_enabled).toBe('boolean');
    // A22: the wheel this gateway serves, which every device row is read against.
    expect(config.client?.build).toMatch(/^[0-9a-f]{64}$/);
    expect(typeof config.client?.version).toBe('string');
    expect(config.client?.url).toContain('/dist/');

    const devices = readFixture<{ devices: Device[] }>('http/devices.list.response.json');
    devices.devices.forEach(assertDevice);

    const sessions = readFixture<{ sessions: Session[] }>('http/sessions.list.response.json');
    sessions.sessions.forEach(assertSession);

    const pairing = readFixture<{
      code: string;
      expires_at: number;
      install: { macos: string; linux: string };
    }>('http/devices.pairing.response.json');
    expect(pairing.code).toMatch(/^RC-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$/);
    expect(pairing.install.macos).toContain(pairing.code);
    expect(pairing.install.linux).toContain(pairing.code);
  });

  it('decodes the account bodies of A24', () => {
    const health = readFixture<{ auth: { mode: string; registration_open: boolean } }>(
      'http/health.response.json',
    );
    // The login screen offers registration only when this is true.
    expect(typeof health.auth.registration_open).toBe('boolean');

    const login = readFixture<{ user: User }>('http/login.response.json');
    const request = readFixture<{ username: string; password: string }>('http/login.request.json');
    expect(request.username).toMatch(USERNAME);
    assertUser(login.user);
    assertUser(readFixture<{ user: User }>('http/auth.session.response.json').user);
    assertUser(readFixture<HelloFrame>('app/hello.json').user);

    const register = readFixture<{ username: string; password: string }>(
      'http/register.request.json',
    );
    expect(register.username).toMatch(USERNAME);
    expect(register.password.length).toBeGreaterThanOrEqual(8);

    const password = readFixture<{ current_password: string; new_password: string }>(
      'http/password.request.json',
    );
    expect(password.new_password.length).toBeGreaterThanOrEqual(8);

    const list = readFixture<{ users: UserRecord[]; registration_open: boolean }>(
      'http/users.list.response.json',
    );
    expect(typeof list.registration_open).toBe('boolean');
    list.users.forEach(assertUserRecord);
    assertUserRecord(readFixture<{ user: UserRecord }>('http/users.response.json').user);

    const create = readFixture<{ username: string; password: string; role?: string }>(
      'http/users.create.request.json',
    );
    expect(create.username).toMatch(USERNAME);
    expect(create.role === undefined || ROLES.has(create.role)).toBe(true);

    const patch = readFixture<{ state?: string; role?: string; password?: string }>(
      'http/users.patch.request.json',
    );
    expect(Object.keys(patch).length).toBeGreaterThan(0);
    if (patch.state !== undefined) expect(STATES.has(patch.state)).toBe(true);

    expect(readFixture<{ open: boolean }>('http/registration.patch.request.json').open).toBe(true);
    expect(typeof readFixture<{ open: boolean }>('http/registration.response.json').open).toBe(
      'boolean',
    );
  });

  it('decodes the device update request and its reply (A22)', () => {
    const request = readFixture<{ type: string; device_id: string; build: string }>(
      'app/device.update.json',
    );
    expect(request.type).toBe('device.update');
    expect(request.build).toMatch(/^[0-9a-f]{64}$/);

    const reply = readFixture<Reply<{ accepted: boolean; from?: string | null }>>(
      'app/reply.device.update.json',
    );
    expect(reply.ok).toBe(true);
    if (!reply.ok) return;
    expect(reply.result.accepted).toBe(true);

    // The build a device reports is what the row compares with the gateway's.
    const updated = readFixture<{ device: Device }>('app/device.updated.json');
    assertDevice(updated.device);
    expect(updated.device.client_build).toMatch(/^[0-9a-f]{64}$/);
  });

  it('decodes the pairing-by-scanning bodies (A23)', () => {
    const request = readFixture<{ token: string; expires_at: number; claim_url: string }>(
      'http/devices.pairing.request.response.json',
    );
    expect(request.token).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/);
    // The web app reads the token out of the fragment of exactly this link.
    expect(request.claim_url).toBe(`${new URL(request.claim_url).origin}/pair#${request.token}`);

    const status = readFixture<{ status: string; code?: string }>(
      'http/devices.pairing.request.status.response.json',
    );
    expect(['waiting', 'claimed']).toContain(status.status);

    const claim = readFixture<{ code: string; expires_at: number }>(
      'http/devices.pairing.claim.response.json',
    );
    expect(claim.code).toMatch(/^RC-[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$/);
    expect(typeof claim.expires_at).toBe('number');
  });

  it('decodes the push payload the service worker renders', () => {
    const payload = readFixture<{
      rc: { v: number; kind: string; device_id: string; session_id: string; device_name: string };
    }>('http/push.payload.json');
    expect(payload.rc.v).toBe(1);
    expect(PUSH_KINDS).toContain(payload.rc.kind);
    expect(typeof payload.rc.device_id).toBe('string');
    expect(typeof payload.rc.session_id).toBe('string');
  });

  /**
   * A47 — the mark on `Session`, the request that clears it, and the count
   * every push carries for the app's icon, decoded the way the app reads them.
   */
  it('decodes the mark, session.seen and the count of A47', () => {
    const hello = readFixture<HelloFrame>('app/hello.json');
    const marked = hello.sessions.filter((session) => session.unseen === true);
    expect(marked).toHaveLength(1);
    expect(countUnseen(Object.fromEntries(hello.sessions.map((s) => [keyOf(s), s])))).toBe(1);

    const seen = readFixture<RequestParams<'session.seen'> & { type: string; id: string }>(
      'app/session.seen.json',
    );
    expect(seen.type).toBe('session.seen');
    expect(typeof seen.id).toBe('string');
    expect(Object.keys(seen).sort()).toEqual(['id', 'session_id', 'type']);

    const push = readFixture<{ rc: { kind: string; badge?: number } }>('http/push.payload.json');
    expect(Number.isInteger(push.rc.badge)).toBe(true);

    const badge = readFixture<{ rc: { kind: string; title: string; badge?: number } }>(
      'http/push.payload.badge.json',
    );
    expect(badge.rc.kind).toBe('badge');
    // It shows nothing, so it says nothing.
    expect(badge.rc.title).toBe('');
    expect(badge.rc.badge).toBeGreaterThanOrEqual(0);
  });

  /**
   * A35 — everything the round added, decoded the way the app reads it: the
   * account's preferences, the two resume requests and the reply that carries
   * the session back, the timeline's new events, and the push the gateway sends
   * when the device schedules a resume.
   */
  it('decodes the preferences bodies and frames of A35', () => {
    const response = readFixture<PreferencesResponse>('http/preferences.response.json');
    expect(typeof response.preferences.resume_after_limit).toBe('boolean');

    const request = readFixture<PreferencesPatch>('http/preferences.patch.request.json');
    expect(typeof request.resume_after_limit).toBe('boolean');

    const updated = readFixture<{ type: string; preferences: Preferences }>(
      'app/preferences.updated.json',
    );
    expect(updated.type).toBe('preferences.updated');
    expect(typeof updated.preferences.resume_after_limit).toBe('boolean');

    // `hello` carries the same object; a gateway older than A35 sends none.
    const hello = readFixture<HelloFrame>('app/hello.json');
    expect(typeof hello.preferences?.resume_after_limit).toBe('boolean');
  });

  /**
   * A41 — the Settings screen's own values in that same object, every one of
   * them optional: what the fixtures carry is what the settings store reads,
   * and what they leave out is what this app writes up once. A44 left
   * `stt_language` to the iPhone, so it is neither read nor written up here.
   */
  it('reads the A41 Settings preferences off the account, except the dictation language', () => {
    const response = readFixture<PreferencesResponse>('http/preferences.response.json');
    expect(response.preferences.stt_language).toBe('zh');
    expect(readPreferences(response.preferences)).toEqual({
      language: 'zh-Hans',
      polishEnabled: true,
      polishModel: 'gpt-5.4-mini',
      polishStrength: 'moderate',
      timelineDetail: 'detailed',
    });

    const updated = readFixture<{ preferences: Preferences }>('app/preferences.updated.json');
    expect(readPreferences(updated.preferences).polishStrength).toBe('moderate');

    // `hello`'s object is a partial one: three fields nobody has set yet, two
    // of them this app's to write up.
    const hello = readFixture<HelloFrame>('app/hello.json');
    const preferences = hello.preferences as Preferences;
    expect(preferences.stt_language).toBeUndefined();
    expect(readPreferences(preferences)).toEqual({
      language: 'en',
      polishEnabled: false,
      timelineDetail: 'simple',
    });
    expect(unsetKeys(preferences)).toEqual(['polishModel', 'polishStrength']);
  });

  it('decodes the resume requests, the session they answer with, and its rows', () => {
    const set = readFixture<{ type: string; session_id: string; at: number }>(
      'app/session.resume_set.json',
    );
    expect(set.type).toBe('session.resume_set');
    expect(typeof set.at).toBe('number');

    const cancel = readFixture<{ type: string; session_id: string }>(
      'app/session.resume_cancel.json',
    );
    expect(cancel.type).toBe('session.resume_cancel');

    const reply = readFixture<Reply<SessionResult>>('app/reply.session.resume_set.json');
    expect(reply.ok).toBe(true);
    if (!reply.ok) return;
    assertSession(reply.result.session);
    // The reply carries the time the request asked for, which is what the
    // notice above the transcript then reads.
    expect(reply.result.session.resume?.at).toBe(set.at);

    const pending = readFixture<Session>('objects/session.resume-pending.json');
    assertSession(pending);
    expect(pending.resume?.estimated).toBe(false);
    expect(pending.resume?.attempts).toBe(0);

    const scheduled = readFixture<SessionEvent>('events/resume.json');
    if (scheduled.kind !== 'resume') throw new Error('not a resume fixture');
    expect(scheduled.status).toBe('scheduled');
    expect(resumeRowText(scheduled, scheduled.at ?? 0)).toContain(en.chat.resumeScheduled);

    const dropped = readFixture<SessionEvent>('events/resume.dropped.json');
    if (dropped.kind !== 'resume') throw new Error('not a resume fixture');
    expect(dropped.status).toBe('dropped');
    // The device's one line of why joins the app's own word for what happened.
    expect(resumeRowText(dropped)).toBe(`${en.chat.resumeDropped} · ${dropped.reason}`);
    // The moment of resuming is not a row: the prompt in the bubble is.
    expect(resumeRowText({ ...scheduled, status: 'fired' })).toBeNull();

    const ended = readFixture<SessionEvent>('events/turn_completed.limit.json');
    if (ended.kind !== 'turn_completed') throw new Error('not a turn_completed fixture');
    expect(ended.stop_reason).toBe('error');
    expect(ended.limit?.window_minutes).toBe(300);

    const started = readFixture<SessionEvent>('events/turn_started.resume.json');
    if (started.kind !== 'turn_started') throw new Error('not a turn_started fixture');
    expect(started.trigger).toBe('resume');

    const prompt = readFixture<SessionEvent>('events/user_message.resume.json');
    if (prompt.kind !== 'user_message') throw new Error('not a user_message fixture');
    expect(prompt.source).toBe('resume');
    // §7.2: one fixed sentence, which also tells the agent about its subagents.
    expect(prompt.text).toContain('The usage limit has reset.');
    expect(prompt.text).toContain('subagents');

    const push = readFixture<{ rc: { kind: string; title: string } }>(
      'http/push.payload.limit.json',
    );
    expect(push.rc.kind).toBe('limit_reached');
    // §3.7: the title is generic and never carries the time.
    expect(push.rc.title).toContain('paused by the usage limit');
    expect(push.rc.title).not.toMatch(/\d/);
  });

  it('decodes the dictation polish bodies of A29', () => {
    const models = readFixture<PolishModelsResponse>('http/polish.models.response.json');
    expect(models.models.length).toBeGreaterThan(0);
    for (const model of models.models) {
      expect(typeof model.id).toBe('string');
      expect(typeof model.label).toBe('string');
    }

    const request = readFixture<PolishRequest>('http/polish.request.json');
    expect(request.text.length).toBeGreaterThan(0);
    // The model comes from the list the gateway serves, and the strength is one
    // of the two §3.5 fixes.
    expect(models.models.some((model) => model.id === request.model)).toBe(true);
    expect(['moderate', 'strong']).toContain(request.strength);
    expect(request.context.length).toBeLessThanOrEqual(CONTEXT_LIMIT);
    for (const item of request.context) {
      expect(['user', 'assistant']).toContain(item.role);
      expect(item.text.length).toBeLessThanOrEqual(CONTEXT_TEXT_LIMIT);
    }

    const response = readFixture<PolishResponse>('http/polish.response.json');
    expect(typeof response.text).toBe('string');
    // The answer is the same request said cleanly, never an empty string.
    expect(response.text.trim().length).toBeGreaterThan(0);

    // `hello` and `GET /api/config` both say whether the gateway can do this.
    expect(readFixture<HelloFrame>('app/hello.json').polish?.enabled).toBe(true);
    expect(
      readFixture<{ polish?: { enabled: boolean } }>('http/config.response.json').polish?.enabled,
    ).toBe(true);
  });

  /**
   * A38 §7.3 — the five requests the terminal page sends, the two replies it
   * reads, and the two frames the gateway pushes to it. The bytes stay base64
   * the whole way: the emulator is the only thing that decodes them.
   */
  it('decodes the terminal frames of A38', () => {
    const open = readFixture<RequestParams<'terminal.open'> & { type: string }>(
      'app/terminal.open.json',
    );
    expect(open.type).toBe('terminal.open');
    expect(open.cols).toBeGreaterThan(0);
    expect(open.cols).toBeLessThanOrEqual(500);
    expect(open.rows).toBeGreaterThan(0);
    expect(open.rows).toBeLessThanOrEqual(200);

    const opened = readFixture<Reply<TerminalOpenResult>>('app/reply.terminal.open.json');
    expect(opened.ok).toBe(true);
    if (!opened.ok) return;
    const terminalId = opened.result.terminal_id;

    const input = readFixture<RequestParams<'terminal.input'> & { type: string }>(
      'app/terminal.input.json',
    );
    expect(input.type).toBe('terminal.input');
    expect(input.terminal_id).toBe(terminalId);
    // §7.3: at most 64 KiB decoded, and never anything but base64.
    expect(input.data).toMatch(/^[A-Za-z0-9+/]*={0,2}$/);
    expect(base64Size(input.data)).toBeLessThanOrEqual(64 * 1024);

    const resize = readFixture<RequestParams<'terminal.resize'> & { type: string }>(
      'app/terminal.resize.json',
    );
    expect(resize.type).toBe('terminal.resize');
    expect(resize.cols).toBeLessThanOrEqual(500);
    expect(resize.rows).toBeLessThanOrEqual(200);

    const attach = readFixture<RequestParams<'terminal.attach'> & { type: string }>(
      'app/terminal.attach.json',
    );
    expect(attach.type).toBe('terminal.attach');
    expect(attach.terminal_id).toBe(terminalId);

    const attached = readFixture<Reply<TerminalAttachResult>>('app/reply.terminal.attach.json');
    expect(attached.ok).toBe(true);
    if (!attached.ok) return;
    expect(attached.result.terminal_id).toBe(terminalId);
    // The screen as it was left, which the page writes before it resumes.
    expect(attached.result.scrollback).toMatch(/^[A-Za-z0-9+/]*={0,2}$/);
    expect(base64Size(attached.result.scrollback)).toBeLessThanOrEqual(64 * 1024);

    const close = readFixture<RequestParams<'terminal.close'> & { type: string }>(
      'app/terminal.close.json',
    );
    expect(close.type).toBe('terminal.close');
    expect(close.terminal_id).toBe(terminalId);

    // The two push frames carry `device_id` and never the gateway's `to`.
    const output = readFixture<PushFrame>('app/terminal.output.json');
    if (output.type !== 'terminal.output') throw new Error('not a terminal.output fixture');
    expect(output.terminal_id).toBe(terminalId);
    expect(output.device_id).toBe(open.device_id);
    expect(output.seq).toBeGreaterThanOrEqual(1);
    expect(base64Size(output.data)).toBeLessThanOrEqual(16 * 1024);
    expect(output).not.toHaveProperty('to');

    const exited = readFixture<PushFrame>('app/terminal.exited.json');
    if (exited.type !== 'terminal.exited') throw new Error('not a terminal.exited fixture');
    expect(exited.terminal_id).toBe(terminalId);
    expect(typeof exited.code).toBe('number');
    expect(exited).not.toHaveProperty('to');
  });

  it('decodes the STT socket frames', () => {
    const partial = readFixture<{ type: string; text: string }>('stt/stt.partial.json');
    expect(partial.type).toBe('stt.partial');
    const final = readFixture<{ type: string; text: string; language: string }>('stt/stt.final.json');
    expect(final.type).toBe('stt.final');
    expect(typeof final.language).toBe('string');
    expect(readFixture<{ type: string }>('stt/stt.stop.json').type).toBe('stt.stop');
    expect(readFixture<{ type: string }>('stt/stt.cancel.json').type).toBe('stt.cancel');
  });
});
